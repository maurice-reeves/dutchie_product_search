"""Private status dashboard: host metrics, scrape state and live visitors.

Mounted by app.py. Everything under /dash and /api/dash is behind HTTP Basic
auth — the browser prompts once and remembers it, which is enough for a
single owner behind the Cloudflare tunnel's TLS. The credentials come from
the environment (or a `.dashboard_password` file next to app.py, which is
gitignored):

    DASHBOARD_PASSWORD   required — with no password the dashboard is simply
                         absent: every route 404s, so it can't be exposed by
                         forgetting to configure it
    DASHBOARD_USER       optional, default "admin"
    DASHBOARD_SCRAPE_DIR optional — where the scraper writes its CSV, logs/
                         and checkpoints/; defaults to the parent of this
                         project, matching scrape_denver_dispensaries.py

The one public route is POST /api/heartbeat, which the search page calls so
the dashboard can count visitors. It accepts an opaque id and stores nothing
else.
"""
import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from . import metrics, visitors

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "products.db"
PAGE = Path(__file__).resolve().parent / "status.html"
_PASSWORD_FILE = PROJECT_ROOT / ".dashboard_password"


def _credentials():
    user = os.environ.get("DASHBOARD_USER", "admin")
    password = os.environ.get("DASHBOARD_PASSWORD")
    if not password and _PASSWORD_FILE.exists():
        password = _PASSWORD_FILE.read_text().strip()
    return user, password


def _scrape_dir() -> Path:
    return Path(os.environ.get("DASHBOARD_SCRAPE_DIR", PROJECT_ROOT.parent))


_basic = HTTPBasic(auto_error=False)


def require_owner(credentials: HTTPBasicCredentials | None = Depends(_basic)):
    user, password = _credentials()
    if not password:
        # Unconfigured: pretend the dashboard doesn't exist rather than
        # advertising a login prompt.
        raise HTTPException(status_code=404)
    ok = (
        credentials is not None
        and secrets.compare_digest(credentials.username.encode(), user.encode())
        and secrets.compare_digest(credentials.password.encode(), password.encode())
    )
    if not ok:
        raise HTTPException(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="dashboard"'},
        )


_PRIVATE_HEADERS = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex"}

router = APIRouter()


@router.get("/dash", dependencies=[Depends(require_owner)], include_in_schema=False)
def dashboard_page():
    return FileResponse(PAGE, headers=_PRIVATE_HEADERS)


@router.get("/api/dash/status", dependencies=[Depends(require_owner)], include_in_schema=False)
def dashboard_status():
    return JSONResponse(
        {
            "now": metrics.latest(),
            "history": metrics.history(),
            "interval_s": metrics.SAMPLE_INTERVAL_S,
            "visitors": visitors.snapshot(),
            "scrape": metrics.scrape_status(DB_PATH, _scrape_dir()),
        },
        headers=_PRIVATE_HEADERS,
    )


class Heartbeat(BaseModel):
    client_id: str = Field(min_length=1, max_length=visitors.MAX_ID_LEN)
    first: bool = False


@router.post("/api/heartbeat", include_in_schema=False)
def heartbeat(beat: Heartbeat):
    visitors.heartbeat(beat.client_id, beat.first)
    return {"ok": True}


@asynccontextmanager
async def lifespan(app):
    """Start the metrics sampler with the app and stop it on shutdown."""
    metrics.prime()
    visitors.load(PROJECT_ROOT / "data" / "visitors.json")

    async def sampler():
        while True:
            await asyncio.sleep(metrics.SAMPLE_INTERVAL_S)
            metrics.sample()

    task = asyncio.create_task(sampler())
    try:
        yield
    finally:
        task.cancel()
