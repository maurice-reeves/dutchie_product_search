"""Host and scrape metrics for the private dashboard.

`sample()` is called from a background task on a fixed interval and appends
to a ring buffer; the dashboard reads the latest sample as "right now" and
the whole buffer for the last-hour sparklines. Network throughput is a delta
between consecutive samples, which is why sampling has to be periodic rather
than on request — two requests a millisecond apart would report nonsense.

Everything comes from psutil so the same code runs on the Mac now and on a
Linux box later. CPU temperature is the one exception: macOS offers no
unprivileged way to read it, so `temp_c` is None here and populated from
/sys on Linux (Raspberry Pi included).
"""
import os
import platform
import sqlite3
import subprocess
import time
from collections import deque
from pathlib import Path

import psutil

SAMPLE_INTERVAL_S = 15
HISTORY_LEN = 3600 // SAMPLE_INTERVAL_S      # one hour

_BOOT = psutil.boot_time()
_history: deque = deque(maxlen=HISTORY_LEN)
_net_prev = (time.time(), psutil.net_io_counters())

_THERMAL_ZONE = Path("/sys/class/thermal/thermal_zone0/temp")


def prime() -> None:
    """Take the first CPU reading; psutil reports 0.0 until it has a baseline."""
    psutil.cpu_percent(interval=None)


def _temperature():
    """(celsius, throttled) on Linux; (None, None) where it can't be read."""
    if not _THERMAL_ZONE.exists():
        return None, None
    try:
        temp = round(int(_THERMAL_ZONE.read_text().strip()) / 1000, 1)
    except (OSError, ValueError):
        return None, None
    throttled = None
    try:
        # Raspberry Pi only: non-zero means under-voltage or thermal throttling
        # has happened since boot.
        out = subprocess.run(
            ["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=2
        )
        if out.returncode == 0:
            throttled = out.stdout.strip() != "throttled=0x0"
    except (OSError, subprocess.SubprocessError):
        pass
    return temp, throttled


def sample() -> dict:
    global _net_prev
    now = time.time()
    net = psutil.net_io_counters()
    prev_t, prev_net = _net_prev
    dt = max(now - prev_t, 1e-6)
    _net_prev = (now, net)

    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage("/")
    temp_c, throttled = _temperature()
    s = {
        "ts": now,
        "uptime_s": now - _BOOT,
        "cpu_pct": psutil.cpu_percent(interval=None),
        "cores": psutil.cpu_count(),
        "mem_used_mb": (vm.total - vm.available) // 2**20,
        "mem_total_mb": vm.total // 2**20,
        "swap_used_mb": swap.used // 2**20,
        "swap_total_mb": swap.total // 2**20,
        "disk_free_gb": round(disk.free / 2**30, 1),
        "disk_total_gb": round(disk.total / 2**30, 1),
        "disk_pct": disk.percent,
        "load": [round(x, 2) for x in os.getloadavg()],
        "procs": len(psutil.pids()),
        "net_down_kbs": round((net.bytes_recv - prev_net.bytes_recv) / dt / 1024, 1),
        "net_up_kbs": round((net.bytes_sent - prev_net.bytes_sent) / dt / 1024, 1),
        "net_total_down_mb": net.bytes_recv // 2**20,
        "net_total_up_mb": net.bytes_sent // 2**20,
        "temp_c": temp_c,
        "throttled": throttled,
        "host": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
    }
    _history.append(s)
    return s


def latest() -> dict:
    return _history[-1] if _history else sample()


def history() -> list:
    return list(_history)


# --- scrape / import state ----------------------------------------------------

def scrape_status(db_path: Path, scrape_dir: Path) -> dict:
    """What the search index holds and whether the daily scrape is mid-run.

    `scrape_dir` is where scrape_denver_dispensaries.py writes its CSV, logs/
    and checkpoints/ (the repo's parent directory by default). A checkpoint
    directory for today with a manifest in it means a run is in flight or
    died part-way; the script deletes the manifest only after the CSV is
    safely on disk.
    """
    out: dict = {"last_import": None, "dispensaries": None,
                 "run": None, "latest_log": None}

    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT source_csv, row_count, imported_at FROM import_meta LIMIT 1"
            ).fetchone()
            if row:
                out["last_import"] = {
                    "csv": Path(row[0]).name, "rows": row[1], "at": row[2],
                }
            out["dispensaries"] = conn.execute(
                "SELECT COUNT(DISTINCT dispensary_display) FROM products"
            ).fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        pass

    today = time.strftime("%Y%m%d")
    manifest = scrape_dir / "checkpoints" / f"denver-{today}" / "manifest.jsonl"
    if manifest.exists():
        try:
            done = sum(1 for line in manifest.open() if line.strip())
        except OSError:
            done = None
        out["run"] = {
            "in_progress": True,
            "dispensaries_done": done,
            "since": manifest.stat().st_mtime,
        }

    logs = sorted((scrape_dir / "logs").glob("dutchie_scraper_*.log"))
    if logs:
        log = logs[-1]
        last_line = ""
        try:
            with log.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(fh.tell() - 4096, 0))
                lines = fh.read().decode("utf-8", "replace").splitlines()
                last_line = next((l for l in reversed(lines) if l.strip()), "")
        except OSError:
            pass
        out["latest_log"] = {
            "name": log.name, "mtime": log.stat().st_mtime, "last_line": last_line,
        }
    return out
