"""The parts of import_csv.py that keep the product popup consistent with a
freshly rebuilt products table. Nothing here touches the real database."""
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import import_csv  # noqa: E402


def test_drop_similarity_tables_removes_only_the_popup_tables():
    conn = sqlite3.connect(":memory:")
    for table in ("products", "restock_events", *import_csv.SIMILARITY_TABLES):
        conn.execute(f"CREATE TABLE {table} (x)")
    import_csv.drop_similarity_tables(conn)
    left = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert left == {"products", "restock_events"}
    import_csv.drop_similarity_tables(conn)          # idempotent when already gone


def test_similarity_python_honours_explicit_setting(monkeypatch):
    monkeypatch.setenv("SIMILARITY_PYTHON", "")
    assert import_csv.similarity_python() is None      # empty = skip the refresh
    monkeypatch.setenv("SIMILARITY_PYTHON", "/opt/ml/bin/python")
    assert import_csv.similarity_python() == "/opt/ml/bin/python"   # taken on trust, no probe


def test_similarity_python_rejects_interpreters_missing_the_stack(monkeypatch):
    monkeypatch.delenv("SIMILARITY_PYTHON", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    assert import_csv.similarity_python() is None


def test_similarity_python_accepts_an_interpreter_with_both_modules(monkeypatch):
    monkeypatch.delenv("SIMILARITY_PYTHON", raising=False)
    probed = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: probed.append(cmd[-1]) or SimpleNamespace(returncode=0))
    assert import_csv.similarity_python() == sys.executable
    assert probed == ["import faiss", "import sentence_transformers"]


def test_refresh_similarity_reports_but_never_raises(monkeypatch):
    calls = []
    monkeypatch.setattr(import_csv, "similarity_python", lambda: "/opt/ml/bin/python")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: calls.append(cmd) or SimpleNamespace(returncode=2))
    assert import_csv.refresh_similarity(Path("/tmp/all_dispensaries.csv")) is False
    assert calls[0][:3] == ["/opt/ml/bin/python", str(import_csv.PROJECT_ROOT / "build_similarity.py"), "all"]
    assert calls[0][calls[0].index("--db") + 1] == str(import_csv.DB_PATH)
    assert calls[0][calls[0].index("--dutchie-csv") + 1] == "/tmp/all_dispensaries.csv"

    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: SimpleNamespace(returncode=0))
    assert import_csv.refresh_similarity(Path("/tmp/all_dispensaries.csv")) is True


def test_refresh_similarity_skips_when_no_interpreter(monkeypatch):
    monkeypatch.setattr(import_csv, "similarity_python", lambda: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    assert import_csv.refresh_similarity(Path("x.csv")) is False


def test_first_import_restock_frame_has_the_real_columns():
    """A fresh database has no earlier snapshot; the empty result must still
    carry the columns or to_sql cannot create restock_events."""
    conn = sqlite3.connect(":memory:")
    import_csv.ensure_snapshot_schema(conn)
    events = import_csv.compute_restocks(conn, "2026-09-18", None)
    assert events.empty and list(events.columns) == import_csv.RESTOCK_COLUMNS
    events.to_sql("restock_events", conn, if_exists="replace", index=False)   # must not raise
    assert conn.execute("SELECT count(*) FROM restock_events").fetchone() == (0,)
