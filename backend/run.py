"""Start Dara locally. FastAPI is preferred; stdlib runner keeps the app usable offline."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    host = os.environ.get("FINANCE_HOST", "127.0.0.1")
    port = int(os.environ.get("FINANCE_PORT", "8765"))
    if importlib.util.find_spec("fastapi") and importlib.util.find_spec("uvicorn"):
        import uvicorn
        uvicorn.run("backend.main:app", host=host, port=port, reload=False)
    else:
        from backend.fallback import serve
        serve(host,port)


if __name__ == "__main__":
    main()
