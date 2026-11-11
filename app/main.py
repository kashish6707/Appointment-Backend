import asyncio
import logging
from contextlib import asynccontextmanager

import socketio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import notifications, realtime
from app.db import engine
from app.routers import auth, dashboard, doctor, payments, profile, public, queue, schedule, staff
from app.schemas import FIELD_MSGS, METHOD_MSG

log = logging.getLogger("clinically")
# Show the app's own info lines (e.g. messages to patients while NOTIFY_CHANNEL=log) next to
# uvicorn's. Uvicorn only sets up its own loggers.
if not log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)
    log.propagate = False


async def _retry_messages_forever(every_seconds: int = 60) -> None:
    """Send messages left over: provider failures waiting to retry, or a crash before sending."""
    while True:
        await asyncio.sleep(every_seconds)
        try:
            await asyncio.to_thread(notifications.deliver_due)
        except Exception:
            log.exception("Retrying messages failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    stop_realtime = realtime.start()
    retry = asyncio.create_task(_retry_messages_forever())
    yield
    retry.cancel()
    stop_realtime()


# No CORS middleware on purpose: the browser only ever talks to the Next.js server,
# which forwards /api/* here unchanged.
api = FastAPI(title="Clinically API", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
api.include_router(auth.router)
api.include_router(doctor.router)
api.include_router(dashboard.router)
api.include_router(public.router)
api.include_router(payments.router)
api.include_router(queue.router)
api.include_router(staff.router)
api.include_router(schedule.router)
api.include_router(profile.router)


def error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"message": message}, status_code=status)


@api.middleware("http")
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


@api.exception_handler(RequestValidationError)
async def on_validation_error(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    if errors and errors[0]["loc"][0] == "path":  # e.g. a malformed id in the URL
        return error(404, "We couldn't find that.")
    return error(400, _validation_message(errors))


@api.exception_handler(StarletteHTTPException)
async def on_http_error(request: Request, exc: StarletteHTTPException):
    response = error(exc.status_code, str(exc.detail))
    extra = getattr(exc, "extra", None)  # e.g. LimitReached: code, limit, booked
    if extra:
        response = JSONResponse({"message": str(exc.detail), **extra}, status_code=exc.status_code)
    for key, value in (exc.headers or {}).items():
        response.headers[key] = value
    return response


@api.exception_handler(Exception)
async def on_unexpected_error(request: Request, exc: Exception):
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return error(500, "Something went wrong on our side. Try again in a moment.")


@api.get("/api/health")
def health():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        log.exception("Health check failed")
        return error(503, "The database is unavailable.")
    return {"status": "ok"}


# What uvicorn runs: Socket.IO handles /api/socket.io/ (live updates), FastAPI everything else.
app = socketio.ASGIApp(realtime.sio, other_asgi_app=api, socketio_path="api/socket.io")
