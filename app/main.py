import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations
from threading import BoundedSemaphore

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from .final_validator import final_validator
from .llm_interpreter import interpret_notes, validate_directives
from .schedule_optimizer import OptimizationError, optimize_schedule
from .schemas import OptimizeRequest, OptimizeResponse

logger = logging.getLogger(__name__)
REQUEST_TIMEOUT_SECONDS = 28.0
MAX_WORKERS = 8
SLOT_WAIT_SECONDS = 10.0
_workers = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="gridwise")
_slots = BoundedSemaphore(MAX_WORKERS)


class RequestBoundary:
    """Deadline covers parsing, pipeline, response validation and serialization.

    Buffer JSON so a partial 200 cannot precede an error. Catch failures inside
    the server-error layer so tracebacks never escape to the ASGI server.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        messages = []

        async def safe_send(message):
            try:
                await send(message)
            except Exception:
                # A disconnected client cannot receive an error response.
                logger.error("response_delivery_failed")

        async def collect(message):
            messages.append(message)

        try:
            await asyncio.wait_for(self.app(scope, receive, collect), timeout=REQUEST_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.error("request_timeout")
            return await JSONResponse(status_code=504, content={"detail": "Request timed out"})(scope, receive, safe_send)
        except Exception:
            logger.error("request_failed")
            return await JSONResponse(status_code=500, content={"detail": "Unable to process request"})(scope, receive, safe_send)
        for message in messages:
            await safe_send(message)


app = FastAPI(title="GridWise", version="0.2.0", debug=False)
app.add_middleware(RequestBoundary)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    # Even error locations can contain caller-supplied field names/secrets.
    return JSONResponse(status_code=400, content={"detail": "Invalid request"})


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": "Request could not be processed"})


@app.get("/")
def read_root():
    return {"status": "online"}
@app.get("/health")
async def health():
    return {"status": "ok"}


def _dropped(index: int) -> dict:
    return {
        "note_index": index,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": "Conflicting constraints made schedule infeasible; this directive was not applied.",
    }


def _largest_feasible_subset(hours: list[dict], battery: dict, directives: list[dict]) -> tuple[list[dict], dict]:
    active = [i for i, d in enumerate(directives) if d["directive_type"] != "no_op"]
    for keep_count in range(len(active) - 1, -1, -1):
        for kept in combinations(active, keep_count):
            candidate = [d if i in kept or d["directive_type"] == "no_op" else _dropped(i)
                         for i, d in enumerate(directives)]
            try:
                return candidate, optimize_schedule(hours, battery, candidate)
            except OptimizationError:
                continue
    raise RuntimeError("Pipeline failed")


def _run_pipeline(request: OptimizeRequest) -> dict:
    # Request schema validation has already completed before this worker starts.
    try:
        hours = [row.model_dump() for row in request.hours]
        battery = request.battery.model_dump()
        interpreted = interpret_notes(request.operator_notes, capacity_kwh=battery["capacity_kwh"])
        directives = validate_directives(
            interpreted, note_count=len(request.operator_notes), capacity_kwh=battery["capacity_kwh"]
        )
        try:
            result = optimize_schedule(hours, battery, directives)
        except OptimizationError:
            # Keep the largest subset of directives that still yields a valid
            # schedule; only the conflicting notes are downgraded to no_op.
            logger.warning("Directives caused infeasible schedule; searching feasible subset")
            directives, result = _largest_feasible_subset(hours, battery, directives)
        errors = final_validator(result["hourly_plan"], hours, battery, directives)
        if errors:
            # Validator text may contain untrusted input; log only an event code.
            logger.error("final_validation_failed")
            raise RuntimeError("Pipeline failed")
        response = OptimizeResponse(
            scenario_id=request.scenario_id,
            directive_interpretation=directives,
            **result,
            plan_summary="Minimum-cost 24-hour schedule satisfying validated operator directives and battery neutrality.",
        )
        return response.model_dump()
    finally:
        # Timed-out threads retain their slots until they actually stop. This
        # bounds abandoned work and prevents an unbounded executor queue.
        _slots.release()


@app.post("/optimize-energy", response_model=OptimizeResponse, responses={
    400: {"description": "Invalid request"}, 500: {"description": "Processing failed"},
    503: {"description": "Service busy"}, 504: {"description": "Request timed out"},
})
async def optimize(request: OptimizeRequest):
    # Wait briefly for a worker slot instead of failing immediately under bursts.
    deadline = time.monotonic() + SLOT_WAIT_SECONDS
    while not _slots.acquire(blocking=False):
        if time.monotonic() >= deadline:
            return JSONResponse(status_code=503, content={"detail": "Service busy; retry later"})
        await asyncio.sleep(0.05)
    try:
        future = asyncio.get_running_loop().run_in_executor(_workers, _run_pipeline, request)
    except Exception:
        _slots.release()
        raise
    # Keep queued/running work alive to ensure its finally block releases the
    # slot. Retrieve late exceptions so asyncio never emits an unhandled traceback.
    future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    return await asyncio.shield(future)
