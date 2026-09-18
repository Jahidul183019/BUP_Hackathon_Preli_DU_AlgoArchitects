from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .interpreter import interpret_notes
from .optimizer import optimize_energy
from .schemas import OptimizeRequest, OptimizeResponse

app = FastAPI(title="GridWise", version="0.1.0")


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=400,
        content={"detail": [
            {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
            for error in exc.errors()
        ]},
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/optimize-energy", response_model=OptimizeResponse, responses={400: {"description": "Invalid request"}})
def optimize(request: OptimizeRequest):
    directives = interpret_notes(request)
    return optimize_energy(request, directives)
