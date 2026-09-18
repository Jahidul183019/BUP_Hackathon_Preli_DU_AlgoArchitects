import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from .final_validator import final_validator
from .llm_interpreter import DirectiveFallback, interpret_notes, validate_directives
from .schedule_optimizer import optimize_schedule
from .schemas import OptimizeRequest, OptimizeResponse

logger = logging.getLogger(__name__)
REQUEST_TIMEOUT_SECONDS = 28.0
_workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="gridwise")
_slots = BoundedSemaphore(4)


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


@app.get("/health")
async def health():
    return {"status": "ok"}


def _run_pipeline(request: OptimizeRequest) -> dict:
    # Request schema validation has already completed before this worker starts.
    try:
        hours = [row.model_dump() for row in request.hours]
        battery = request.battery.model_dump()
        interpreted = interpret_notes(request.operator_notes, capacity_kwh=battery["capacity_kwh"])
        directives = validate_directives(
            interpreted, note_count=len(request.operator_notes), capacity_kwh=battery["capacity_kwh"]
        )
        if any(isinstance(entry, DirectiveFallback) for entry in directives):
            logger.error("interpretation_failed")
            raise RuntimeError("Pipeline failed")
        result = optimize_schedule(hours, battery, directives)
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
    if not _slots.acquire(blocking=False):
        return JSONResponse(status_code=503, content={"detail": "Service busy; retry later"})
    try:
        future = asyncio.get_running_loop().run_in_executor(_workers, _run_pipeline, request)
    except Exception:
        _slots.release()
        raise
    # Keep queued/running work alive to ensure its finally block releases the
    # slot. Retrieve late exceptions so asyncio never emits an unhandled traceback.
    future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    return await asyncio.shield(future)
