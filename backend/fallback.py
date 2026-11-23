"""Dependency-free local REST/asset server used when FastAPI is not installed."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .database import ROOT, init_database
from .service import ApiError, ResponseData, dispatch

FRONTEND = ROOT / "frontend"


class Handler(BaseHTTPRequestHandler):
    server_version = "DaraFinance/1.0"

    def _send(self, status, body, content_type="application/json; charset=utf-8", filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if filename:
            safe = filename.replace('"', "")
            self.send_header("Content-Disposition", f'attachment; filename="{safe}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self):
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/api/"):
            length = int(self.headers.get("Content-Length", "0"))
            if length > 25 * 1024 * 1024:
                self._send(413, b'{"error":"Request is too large."}')
                return
            body = {}
            if self.command in ("POST", "PATCH", "PUT") and length:
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except Exception:
                    self._send(400, b'{"error":"Request body must be valid JSON."}')
                    return
            params = {key: values[-1] for key, values in parse_qs(parsed.query, keep_blank_values=True).items()}
            try:
                result = dispatch(self.command, parsed.path, params, body)
            except ApiError as exc:
                payload = {"error": exc.message}
                if exc.details is not None:
                    payload["details"] = exc.details
                self._send(exc.status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                return
            if isinstance(result, ResponseData):
                self._send(result.status, result.body, result.content_type, result.filename)
            else:
                self._send(200, json.dumps(result, ensure_ascii=False, default=str).encode("utf-8"))
            return
        if parsed.path == "/":
            target = FRONTEND / "index.html"
        elif parsed.path.startswith("/static/"):
            target = (FRONTEND / parsed.path.removeprefix("/static/")).resolve()
            if not target.is_relative_to(FRONTEND.resolve()):
                self._send(403, b"Forbidden", "text/plain; charset=utf-8")
                return
        else:
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        if not target.is_file():
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        ext = target.suffix.lower()
        mime = {".html":"text/html; charset=utf-8", ".css":"text/css; charset=utf-8", ".js":"text/javascript; charset=utf-8", ".svg":"image/svg+xml", ".webmanifest":"application/manifest+json"}.get(ext,"application/octet-stream")
        self._send(200, target.read_bytes(), mime)

    def do_GET(self): self._handle()
    def do_POST(self): self._handle()
    def do_PATCH(self): self._handle()
    def do_PUT(self): self._handle()
    def do_DELETE(self): self._handle()

    def log_message(self, fmt, *args):
        print("[dara] " + fmt % args)


def serve(host="127.0.0.1", port=8765):
    init_database()
    server = ThreadingHTTPServer((host,port),Handler)
    print(f"Dara is running at http://{host}:{port}")
    server.serve_forever()
