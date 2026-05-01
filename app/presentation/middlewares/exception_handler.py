import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.common import error_codes
from app.common.exceptions import FriendlyException

logger = structlog.get_logger(__name__)


def setup_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(FriendlyException)
    async def friendly_exception_handler(request: Request, exc: FriendlyException) -> JSONResponse:
        logger.warning(
            "friendly_exception",
            path=request.url.path,
            code=exc.code,
            message=exc.message,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "message": exc.message, "data": exc.data},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        logger.warning("request_validation_error", path=request.url.path, errors=exc.errors())
        return JSONResponse(
            status_code=422,
            content={
                "code": error_codes.VALIDATION_ERROR,
                "message": "Validation error",
                "data": {"errors": exc.errors()},
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_exception", path=request.url.path, exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={
                "code": error_codes.INTERNAL_ERROR,
                "message": "Internal server error",
                "data": None,
            },
        )
