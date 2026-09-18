"""The combined importer: how the two sources are merged, first-seen dates,
the restock flag and the similarity hook. Nothing here touches the real
database or the network."""
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import import_csv  # noqa: E402
import import_products as ip  # noqa: E402


# ---- store matching ---------------------------------------------------------

@pytest.mark.parametrize("dutchie, jane, same", [
    ("Tgs Wewatta", "The Green Solution - Wewatta", True),
    ("Tgs Edgewater", "The Green Solution - Edgewater (20th)", True),      # extra detail on one side is fine
    ("Medicine Man Aurora", "Medicine Man - Aurora", True),
    ("Every Day Weed Capitol Hill", "EDW - Capitol Hill - Denver ", True),
    ("Star Buds Lakeside", "SB - Lakeside (MED)", True),
    ("Tgs Malley", "The Green Solution - Northglenn", False),              # same brand, different place
    ("Tgs Wewatta", "LivWell Wewatta", False),                              # same place, different brand
])
def test_same_store_by_brand_and_location(dutchie, jane, same):
    assert ip.same_store(ip.store_key(dutchie), ip.store_key(jane)) is same


def test_store_key_ignores_stores_that_are_not_vireo_brands():
    assert ip.store_key("Alternative Medicine Capitol Hill") is None    # "medicine" is not "medicine man"
    assert ip.store_key("Native Roots Edgewater") is None


def frame(rows):
    return pd.DataFrame(rows, columns=["dispensary_display", "name", "created_at", "updated_at"])


def test_combine_replaces_only_the_dutchie_stores_jane_also_has():
    dutchie = frame([("Tgs Wewatta", "a", None, None), ("Tgs Wewatta", "b", None, None),
                     ("Tgs Malley", "c", None, None), ("Native Roots Edgewater", "d", None, None)])
    jane = frame([("The Green Solution - Wewatta", "e", None, None),
                  ("The Green Solution - Edgewater (20th)", "f", None, None)])
    out, lines, dropped = ip.combine(dutchie, jane)
    assert dropped == 2
    assert set(out["dispensary_display"]) == {"Tgs Malley", "Native Roots Edgewater",
                                              "The Green Solution - Wewatta", "The Green Solution - Edgewater (20th)"}
    assert lines == ["  Tgs Malley (Dutchie) kept: no matching Jane store",
                     "  Tgs Wewatta (Dutchie) -> replaced by The Green Solution - Wewatta (Jane)"]


def test_combine_without_jane_is_a_no_op():
    dutchie = frame([("Tgs Wewatta", "a", None, None)])
    out, lines, dropped = ip.combine(dutchie, None)
    assert len(out) == 1 and lines == [] and dropped == 0


def test_combine_writes_naive_utc_dates():
    dutchie = frame([("X", "a", "2026-09-01T10:00:00+00:00", None)])
    jane = frame([("LivWell Uptown", "b", None, None)])
    out, _, _ = ip.combine(dutchie, jane)
    assert str(out.loc[0, "created_at"]) == "2026-09-01 10:00:00"
    assert pd.isna(out.loc[1, "created_at"])


# ---- first seen -------------------------------------------------------------

def test_first_seen_keeps_the_earliest_date_and_fills_missing_created_at():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE products (product_id, dispensary_slug, created_at)")
    conn.executemany("INSERT INTO products VALUES (?, ?, ?)",
                     [("1", "livwell-uptown", None), ("2", "tgs-wewatta", "2025-09-22 16:14:38"), ("1", "livwell-uptown", None)])
    assert ip.record_first_seen(conn, "2026-09-17") == 2          # two distinct (product, store) pairs
    assert ip.record_first_seen(conn, "2026-09-18") == 0          # nothing new the next day
    rows = dict(conn.execute("SELECT product_id, created_at FROM products").fetchall())
    assert rows["1"] == "2026-09-17 00:00:00"                     # filled from first sighting, not today
    assert rows["2"] == "2025-09-22 16:14:38"                     # the source's own date is kept


# ---- vireo csv freshness ----------------------------------------------------

def test_choose_vireo_rejects_a_stale_csv(tmp_path, capsys):
    dutchie = tmp_path / "all_dispensaries20260918_064312.csv"
    fresh = tmp_path / "vireo_products20260918_071219.csv"
    stale = tmp_path / "vireo_products20260910_071219.csv"
    assert ip.choose_vireo(fresh, dutchie) == fresh
    assert ip.choose_vireo(stale, dutchie) is None
    assert ip.choose_vireo(None, dutchie) is None
    assert "8 days older" in capsys.readouterr().out


# ---- restock flag -----------------------------------------------------------

def products_frame():
    return pd.DataFrame({
        "product_id": ["1", "2"], "dispensary_slug": ["a", "b"], "name": ["x", "y"], "brand_name": ["", ""],
        "dispensary_display": ["A", "B"], "price": [1.0, 2.0], "product_type": ["Flower", "Edible"],
        "created_at": [None, None], "scrape_date": ["2026-09-18", "2026-09-18"],
    })


def test_restock_tracking_off_drops_stale_events(tmp_path, monkeypatch):
    db = tmp_path / "p.db"
    monkeypatch.setattr(ip, "DB_PATH", db)
    monkeypatch.setattr(ip, "RESTOCK_TRACKING", False)
    conn = sqlite3.connect(db); conn.execute("CREATE TABLE restock_events (x)"); conn.commit(); conn.close()
    lines = ip.write_database(products_frame(), [Path("d.csv")])
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "restock_events" not in tables and {"products", "first_seen", "import_meta"} <= tables
    assert any("Restock tracking off" in l for l in lines)
    assert conn.execute("SELECT count(*) FROM products WHERE created_at = '2026-09-18 00:00:00'").fetchone() == (2,)


def test_restock_tracking_on_calls_the_parked_code(tmp_path, monkeypatch):
    monkeypatch.setattr(ip, "DB_PATH", tmp_path / "p.db")
    monkeypatch.setattr(ip, "RESTOCK_TRACKING", True)
    monkeypatch.setattr(import_csv, "record_restocks", lambda conn, out: ["restocks recorded"])
    assert "restocks recorded" in ip.write_database(products_frame(), [Path("d.csv")])


def test_first_import_restock_frame_has_the_real_columns():
    """A fresh database has no earlier snapshot; the empty result must still
    carry the columns or to_sql cannot create restock_events."""
    conn = sqlite3.connect(":memory:")
    import_csv.ensure_snapshot_schema(conn)
    events = import_csv.compute_restocks(conn, "2026-09-18", None)
    assert events.empty and list(events.columns) == import_csv.RESTOCK_COLUMNS
    events.to_sql("restock_events", conn, if_exists="replace", index=False)   # must not raise


# ---- similarity hook --------------------------------------------------------

def test_drop_similarity_tables_removes_only_the_popup_tables():
    conn = sqlite3.connect(":memory:")
    for table in ("products", "first_seen", *ip.SIMILARITY_TABLES):
        conn.execute(f"CREATE TABLE {table} (x)")
    ip.drop_similarity_tables(conn)
    left = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert left == {"products", "first_seen"}
    ip.drop_similarity_tables(conn)          # idempotent when already gone


def test_similarity_python_honours_explicit_setting(monkeypatch):
    monkeypatch.setenv("SIMILARITY_PYTHON", "")
    assert ip.similarity_python() is None      # empty = skip the refresh
    monkeypatch.setenv("SIMILARITY_PYTHON", "/opt/ml/bin/python")
    assert ip.similarity_python() == "/opt/ml/bin/python"   # taken on trust, no probe


def test_similarity_python_probes_both_modules(monkeypatch):
    monkeypatch.delenv("SIMILARITY_PYTHON", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    assert ip.similarity_python() is None
    probed = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: probed.append(cmd[-1]) or SimpleNamespace(returncode=0))
    assert ip.similarity_python() == sys.executable
    assert probed == ["import faiss", "import sentence_transformers"]


def test_refresh_similarity_passes_both_csvs_and_never_raises(monkeypatch):
    calls = []
    monkeypatch.setattr(ip, "similarity_python", lambda: "/opt/ml/bin/python")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: calls.append(cmd) or SimpleNamespace(returncode=2))
    assert ip.refresh_similarity(Path("/tmp/d.csv"), Path("/tmp/v.csv")) is False
    cmd = calls[0]
    assert cmd[:3] == ["/opt/ml/bin/python", str(ip.PROJECT_ROOT / "build_similarity.py"), "all"]
    assert cmd[cmd.index("--dutchie-csv") + 1] == "/tmp/d.csv" and cmd[cmd.index("--vireo-csv") + 1] == "/tmp/v.csv"
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: calls.append(cmd) or SimpleNamespace(returncode=0))
    assert ip.refresh_similarity(Path("/tmp/d.csv"), None) is True
    assert "--vireo-csv" not in calls[1]


def test_refresh_similarity_skips_when_no_interpreter(monkeypatch):
    monkeypatch.setattr(ip, "similarity_python", lambda: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    assert ip.refresh_similarity(Path("x.csv"), None) is False
