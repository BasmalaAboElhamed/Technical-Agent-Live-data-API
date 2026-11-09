from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import secrets
import threading
import time
from typing import Dict, List, Optional, Tuple

from fastapi import Depends, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import Settings
from app.services.stock_service import StockService


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details=None):
        self.status_code, self.code, self.message, self.details = status_code, code, message, details


def get_service(request: Request) -> StockService:
    return request.app.state.service


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


class RateLimiter:
    """Thread-safe sliding-window rate limiter per identity (API key or IP)."""

    def __init__(self, limit_per_minute: int = 120):
        self.limit = limit_per_minute
        self._lock = threading.Lock()
        self._requests: Dict[str, List[float]] = defaultdict(list)

    def is_allowed(self, identity: str) -> Tuple[bool, int]:
        now = time.monotonic()
        cutoff = now - 60.0
        with self._lock:
            timestamps = self._requests[identity]
            self._requests[identity] = [t for t in timestamps if t > cutoff]
            if len(self._requests[identity]) >= self.limit:
                oldest = self._requests[identity][0]
                retry_after = max(int(60.0 - (now - oldest)), 1)
                return False, retry_after
            self._requests[identity].append(now)
            return True, 0


def require_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key", description="Private API Key")
) -> Optional[str]:
    s: Settings = request.app.state.settings
    if not s.api_keys:
        return x_api_key  # No keys configured: local open mode
    if not x_api_key:
        raise ApiError(401, "unauthorized", "Missing X-API-Key header.")
    matched = any(secrets.compare_digest(x_api_key, k) for k in s.api_keys)
    if not matched:
        raise ApiError(401, "unauthorized", "Invalid X-API-Key.")
    return x_api_key


def check_rate_limit(
    request: Request,
    api_key: Optional[str] = Depends(require_api_key),
) -> None:
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        return
    identity = api_key or (request.client.host if request.client else "unknown")
    allowed, retry_after = limiter.is_allowed(identity)
    if not allowed:
        raise ApiError(429, "rate_limited", "Too many requests. Please slow down.", {"retry_after": retry_after})


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_bytes: int):
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > self.max_bytes:
                    return JSONResponse(
                        status_code=413,
                        content={"error": {"code": "payload_too_large", "message": f"Request body exceeds {self.max_bytes} bytes limit."}}
                    )
            except ValueError:
                pass
        return await call_next(request)


@dataclass
class Pagination:
    limit: int
    offset: int


def pagination(
    request: Request,
    limit: Optional[int] = Query(None, ge=1, description="Max records (default and maximum are configurable)."),
    offset: int = Query(0, ge=0, description="Records to skip."),
) -> Pagination:
    s: Settings = request.app.state.settings
    if limit is None:
        limit = s.default_limit
    if limit > s.max_limit:
        raise ApiError(422, "limit_too_large", f"limit must be <= {s.max_limit}")
    return Pagination(limit, offset)


def err(status: int, code: str, message: str, details=None, headers=None) -> JSONResponse:
    body = {"error": {"code": code, "message": message}}
    if details is not None:
        body["error"]["details"] = details
    return JSONResponse(status_code=status, content=body, headers=headers)


def install_error_handlers(app) -> None:
    @app.exception_handler(ApiError)
    async def _api(_: Request, exc: ApiError):
        headers = {}
        if exc.status_code == 429 and exc.details and "retry_after" in exc.details:
            headers["Retry-After"] = str(exc.details["retry_after"])
        return err(exc.status_code, exc.code, exc.message, exc.details, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        details = [{"loc": list(e["loc"]), "message": e["msg"], "type": e["type"]} for e in exc.errors()]
        return err(422, "validation_error", "Invalid request parameters.", details)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed", 413: "payload_too_large"}.get(exc.status_code, "http_error")
        return err(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        import logging
        logging.getLogger(__name__).exception("unhandled error")
        return err(500, "internal_error", "Internal server error.")

