"""Authenticated multi-user server entry point."""
from __future__ import annotations

import json
import os

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import auth
from .database import ROOT
from .service import ApiError, ResponseData, dispatch

FRONTEND = ROOT / "frontend"
app = FastAPI(title="Dara Finance Server", version="1.0.0", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.on_event("startup")
def startup():
    auth.initialize_auth()


def _token(request: Request) -> str | None:
    return request.cookies.get(auth.SESSION_COOKIE)


def _session(request: Request, *, write: bool = False) -> dict | JSONResponse:
    session = auth.resolve_session(_token(request))
    if session is None:
        return JSONResponse({"error": "Authentication required."}, status_code=401)
    if write:
        origin = request.headers.get("origin")
        host = request.headers.get("host", "")
        if origin and origin.rstrip("/") != f"{request.url.scheme}://{host}".rstrip("/"):
            return JSONResponse({"error": "Request origin is not allowed."}, status_code=403)
        if not auth.verify_csrf(session, request.headers.get("x-csrf-token")):
            return JSONResponse({"error": "Security token is missing or expired. Reload the page and try again."}, status_code=403)
    return session


async def _body(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        raise auth.AuthError(400, "Request body must be valid JSON.") from None
    if not isinstance(payload, dict):
        raise auth.AuthError(400, "Request body must be a JSON object.")
    return payload


def _error(exc: auth.AuthError) -> JSONResponse:
    return JSONResponse({"error": exc.message}, status_code=exc.status)


def _set_cookie(response: JSONResponse, token: str, expires: int) -> JSONResponse:
    import time
    response.set_cookie(auth.SESSION_COOKIE, token, max_age=max(0, expires-int(time.time())),
                        httponly=True, secure=auth.cookie_secure(), samesite="strict", path="/")
    return response


@app.get("/", include_in_schema=False)
def index(request: Request):
    if auth.resolve_session(_token(request)):
        return FileResponse(FRONTEND / "index.html")
    return RedirectResponse("/login", status_code=303)


@app.get("/login", include_in_schema=False)
def login_page(request: Request):
    if auth.resolve_session(_token(request)):
        return RedirectResponse("/", status_code=303)
    return FileResponse(FRONTEND / "login.html")


@app.get("/api/auth/config")
def auth_config():
    return {"signup_enabled": auth.allow_signup()}


@app.post("/api/auth/register")
async def register(request: Request):
    try:
        body = await _body(request)
        user, token, expires = await run_in_threadpool(auth.register, body.get("username"), body.get("password"), request.client.host if request.client else "unknown")
        response = JSONResponse({"user": user, "csrf_token": auth.csrf_value(token)}, status_code=201)
        return _set_cookie(response, token, expires)
    except auth.AuthError as exc:
        return _error(exc)
    except Exception as exc:
        from sqlite3 import IntegrityError
        if isinstance(exc, IntegrityError):
            return JSONResponse({"error": "That username is already in use."}, status_code=409)
        raise


@app.post("/api/auth/login")
async def sign_in(request: Request):
    try:
        body = await _body(request)
        user, token, expires = await run_in_threadpool(auth.login, body.get("username"), body.get("password"), request.client.host if request.client else "unknown")
        response = JSONResponse({"user": user, "csrf_token": auth.csrf_value(token)})
        return _set_cookie(response, token, expires)
    except auth.AuthError as exc:
        return _error(exc)


@app.get("/api/auth/me")
def current_user(request: Request):
    session = auth.resolve_session(_token(request))
    if session is None:
        return JSONResponse({"error": "Authentication required."}, status_code=401)
    return {"user": session["user"], "csrf_token": session["csrf_token"]}


@app.post("/api/auth/logout")
def sign_out(request: Request):
    session = _session(request, write=True)
    if isinstance(session, JSONResponse):
        return session
    auth.logout(_token(request))
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.SESSION_COOKIE, path="/", httponly=True, secure=auth.cookie_secure(), samesite="strict")
    return response


@app.post("/api/auth/password")
async def update_password(request: Request):
    session = _session(request, write=True)
    if isinstance(session, JSONResponse):
        return session
    try:
        body = await _body(request)
        await run_in_threadpool(auth.change_password, session["user"]["id"], _token(request) or "", body.get("current_password"),
                                body.get("new_password"), request.client.host if request.client else "unknown")
        return {"ok": True}
    except auth.AuthError as exc:
        return _error(exc)


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE"])
async def api(path: str, request: Request):
    is_write = request.method in {"POST", "PATCH", "PUT", "DELETE"}
    session = _session(request, write=is_write)
    if isinstance(session, JSONResponse):
        return session
    params = dict(request.query_params)
    body = {}
    if request.method in {"POST", "PATCH", "PUT"}:
        try:
            body = await _body(request)
        except auth.AuthError as exc:
            return _error(exc)
    try:
        with auth.tenant_scope(session):
            result = await run_in_threadpool(dispatch, request.method, "/api/" + path, params, body)
    except ApiError as exc:
        payload = {"error": exc.message}
        if exc.details is not None:
            payload["details"] = exc.details
        return JSONResponse(payload, status_code=exc.status)
    if isinstance(result, ResponseData):
        headers = {}
        if result.filename:
            headers["Content-Disposition"] = f'attachment; filename="{result.filename}"'
        return Response(result.body, status_code=result.status, media_type=result.content_type, headers=headers)
    return JSONResponse(result)
