"""FastAPI entry point. Financial rules and persistence live in service.py/database.py."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .database import ROOT, init_database
from .service import ApiError, ResponseData, dispatch

FRONTEND = ROOT / "frontend"
app = FastAPI(title="Dara Personal Finance", version="1.0.0", docs_url="/api/docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


@app.on_event("startup")
def startup():
    init_database()


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(FRONTEND / "index.html")


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE"])
async def api(path: str, request: Request):
    params = dict(request.query_params)
    body = {}
    if request.method in ("POST", "PATCH", "PUT"):
        raw = await request.body()
        if raw:
            try:
                body = await request.json()
            except Exception:
                return JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)
    try:
        result = dispatch(request.method, "/api/" + path, params, body)
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
