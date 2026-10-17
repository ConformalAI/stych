"""
Ad-hoc driver: exercise stych's REAL Postgres connector + analysis/cleaning tools
against a live Postgres table, capture all logs + structured output to disk.

Run from backend/:  .venv/bin/python tools/run_pg_tests.py
Self-limits to 250s via SIGALRM.
"""

import json
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# --- stych code under test ---
from datasources.postgres import (
    list_postgres_tables,
    load_from_postgres,
    test_postgres_connection,
)
from tools.data_tools import DataAnalysisTools, DataCleaningTools

CONN_URL = "postgresql+psycopg2://stych:stych123@localhost:5433/demo"
SCHEMA = "public"
TABLE = "orders"
TIME_LIMIT_S = 250

RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
OUT_DIR = Path(__file__).resolve().parent.parent / "tests" / "test_runs" / RUN_ID
OUT_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = OUT_DIR / "run.log"
RESULT_PATH = OUT_DIR / "results.json"

# --- capture stych's own logging (the tools log via `logging`) + ours ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("pg_test_driver")


class TimeLimit(Exception):
    pass


def _on_alarm(signum, frame):
    raise TimeLimit(f"exceeded {TIME_LIMIT_S}s budget")


signal.signal(signal.SIGALRM, _on_alarm)
signal.alarm(TIME_LIMIT_S)


def _jsonable(obj):
    """Make tool outputs JSON-serializable; drop big DataFrames to a shape summary."""
    if isinstance(obj, pd.DataFrame):
        return {"__dataframe__": True, "shape": list(obj.shape), "columns": list(obj.columns)}
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return None if np.isnan(obj) else float(obj)
    if isinstance(obj, np.ndarray):
        return _jsonable(obj.tolist())
    return obj


results = {"run_id": RUN_ID, "table": f"{SCHEMA}.{TABLE}", "started": RUN_ID, "stages": {}}


def stage(name, fn):
    t0 = time.time()
    log.info("──────── STAGE: %s ────────", name)
    try:
        out = fn()
        dt = round(time.time() - t0, 3)
        results["stages"][name] = {"ok": True, "seconds": dt, "output": _jsonable(out)}
        log.info("STAGE %s OK in %.3fs", name, dt)
        return out
    except Exception as e:  # noqa: BLE001 - we want to record any failure
        dt = round(time.time() - t0, 3)
        results["stages"][name] = {"ok": False, "seconds": dt, "error": repr(e)}
        log.exception("STAGE %s FAILED in %.3fs", name, dt)
        return None


def main():
    log.info("Output dir: %s", OUT_DIR)

    # 1. Connector: connectivity + table listing + preview load
    stage("connect", lambda: dict(zip(("ok", "msg"), test_postgres_connection(CONN_URL))))
    stage("list_tables", lambda: list_postgres_tables(schema=SCHEMA, connection_url=CONN_URL))

    loaded = stage(
        "load_preview",
        lambda: load_from_postgres(connection_url=CONN_URL, schema=SCHEMA, table=TABLE),
    )
    if loaded is None:
        log.error("Load failed; aborting downstream stages.")
        return
    df, label = loaded
    log.info("Loaded %s rows x %s cols from %s", df.shape[0], df.shape[1], label)

    # Coerce the text-typed numeric columns so analysis/outlier tools have real numerics.
    df_typed = df.copy()
    for c in ("quantity", "unit_price"):
        if c in df_typed.columns:
            df_typed[c] = pd.to_numeric(df_typed[c], errors="coerce")

    # 2. ANALYSIS suite (stych DataAnalysisTools)
    A = DataAnalysisTools
    stage("analysis.describe_statistics", lambda: A.describe_statistics(df_typed))
    stage("analysis.missing_values_report", lambda: A.missing_values_report(df_typed))
    stage("analysis.correlation_analysis", lambda: A.correlation_analysis(df_typed))
    stage("analysis.distribution_revenue", lambda: A.distribution_analysis(df_typed, "revenue"))
    stage("analysis.distribution_status", lambda: A.distribution_analysis(df_typed, "status"))

    # 3. CLEANING suite (stych DataCleaningTools)
    C = DataCleaningTools
    dedup = stage("cleaning.remove_duplicates", lambda: C.remove_duplicates(df_typed))
    base = dedup["data"] if dedup and "data" in dedup else df_typed
    filled = stage(
        "cleaning.handle_missing_values", lambda: C.handle_missing_values(base, strategy="auto")
    )
    stage("cleaning.detect_outliers_revenue", lambda: C.detect_outliers(df_typed, "revenue"))
    stage(
        "cleaning.detect_outliers_unit_price",
        lambda: C.detect_outliers(df_typed, "unit_price", method="zscore"),
    )

    # 4. Persist the cleaned frame as a real artifact
    if filled and "data" in filled:
        cleaned = filled["data"]
        csv_path = OUT_DIR / "orders_cleaned.csv"
        cleaned.to_csv(csv_path, index=False)
        results["cleaned_csv"] = str(csv_path)
        log.info("Wrote cleaned dataset -> %s (%s rows)", csv_path, len(cleaned))


if __name__ == "__main__":
    code = 0
    try:
        main()
        results["status"] = "completed"
    except TimeLimit as e:
        results["status"] = f"time_limit: {e}"
        log.error("TIME LIMIT: %s", e)
        code = 2
    finally:
        signal.alarm(0)
        n_ok = sum(1 for s in results["stages"].values() if s.get("ok"))
        n_total = len(results["stages"])
        results["summary"] = {"stages_ok": n_ok, "stages_total": n_total}
        RESULT_PATH.write_text(json.dumps(results, indent=2, default=str))
        log.info("=== DONE: %s/%s stages OK -> %s", n_ok, n_total, RESULT_PATH)
    sys.exit(code)
