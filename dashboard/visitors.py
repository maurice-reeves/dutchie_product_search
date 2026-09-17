"""Live visitor count for the dashboard.

The public search page posts a heartbeat every few seconds carrying a random
id it made up for its own tab. "Visitors now" is how many distinct ids were
heard from in the last TTL seconds; nothing about the visitor is stored, only
the id and a timestamp. Page views and the peak are the only numbers worth
keeping across restarts, so those go to a small JSON file.

Everything lives in one process. Run uvicorn with a single worker (the
default) or the counts will be split across processes.
"""
import json
import os
import time
from pathlib import Path

TTL_S = 30                 # a tab that hasn't pinged in this long has gone
MAX_ID_LEN = 64

_seen: dict = {}           # client_id -> last heartbeat time
_views = 0
_peak = 0
_state_path: Path | None = None


def load(state_path: Path) -> None:
    global _views, _peak, _state_path
    _state_path = state_path
    try:
        data = json.loads(state_path.read_text())
        _views = int(data.get("views", 0))
        _peak = int(data.get("peak", 0))
    except (OSError, ValueError):
        pass


def _save() -> None:
    if _state_path is None:
        return
    tmp = _state_path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps({"views": _views, "peak": _peak}))
        os.replace(tmp, _state_path)
    except OSError:
        pass


def _prune(now: float) -> None:
    cutoff = now - TTL_S
    for key in [k for k, t in _seen.items() if t < cutoff]:
        del _seen[key]


def heartbeat(client_id: str, first: bool) -> None:
    """Record a ping. `first` is True once per page load and counts a view."""
    global _views, _peak
    if not client_id or len(client_id) > MAX_ID_LEN:
        return
    now = time.time()
    _seen[client_id] = now
    _prune(now)
    changed = False
    if first:
        _views += 1
        changed = True
    if len(_seen) > _peak:
        _peak = len(_seen)
        changed = True
    if changed:
        _save()


def snapshot() -> dict:
    _prune(time.time())
    return {"now": len(_seen), "peak": _peak, "views": _views}
