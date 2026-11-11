import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.db import engine
from app.routers import auth, doctor
from app.schemas import FIELD_MSGS, METHOD_MSG

log = logging.getLogger("clinically")

# No CORS middleware on purpose: the browser only ever talks to the Next.js server,
# which forwards /api/* here unchanged.
app = FastAPI(title="Clinically API", docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(auth.router)
app.include_router(doctor.router)


def error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"message": message}, status_code=status)


@app.middleware("http")
async def require_json_posts(request: Request, call_next):
    # With SameSite=Lax this stops other sites from posting HTML forms to the API.
    if request.method == "POST":
        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            return error(415, "Send the request as JSON (Content-Type: application/json).")
    return await call_next(request)


def _validation_message(errors: list[dict]) -> str:
    if not errors:
        return "The request is invalid."
    err = errors[0]
    kind = err["type"]
    path = [p for p in err["loc"] if isinstance(p, str)]
    field = path[-1] if len(path) > 1 else None  # path[0] is "body"

    if kind == "json_invalid":
        return "The request body isn't valid JSON."
    if kind in ("union_tag_not_found", "union_tag_invalid"):
        return METHOD_MSG
    if kind == "extra_forbidden":
        return f"Unexpected field: {field}."
    if kind == "value_error":
        return str(err.get("ctx", {}).get("error") or err["msg"].removeprefix("Value error, "))
    if field in FIELD_MSGS:
        return FIELD_MSGS[field]
    if kind == "missing":
        return "The request body is missing."
    return "Send the request body as a JSON object."


@app.exception_handler(RequestValidationError)
async def on_validation_error(request: Request, exc: RequestValidationError):
    return error(400, _validation_message(exc.errors()))


@app.exception_handler(StarletteHTTPException)
async def on_http_error(request: Request, exc: StarletteHTTPException):
    response = error(exc.status_code, str(exc.detail))
    for key, value in (exc.headers or {}).items():
        response.headers[key] = value
    return response


@app.exception_handler(Exception)
async def on_unexpected_error(request: Request, exc: Exception):
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return error(500, "Something went wrong on our side. Try again in a moment.")


@app.get("/api/health")
def health():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        log.exception("Health check failed")
        return error(503, "The database is unavailable.")
    return {"status": "ok"}
