"""
Focused end-to-end test for the **analysis agent on a Postgres table**, prompt → output.

Seeds a small *messy* table in the demo DB (text-typed numeric columns + sentinel junk,
mirroring the user's `dirty_cafe_sales`), then drives DataAnalysisAgent.process_query directly
(no orchestrator) with a complex multi-part analytical prompt. Captures the full ReAct trace and
asserts the fix: the agent now runs *multiple* run_sql steps and recovers from any SQL error
within the loop instead of failing on the first query.

Run from backend/:  .venv/bin/python -m tools.run_sql_analysis_e2e
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()  # .env must load before stych imports read env

import pandas as pd  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from agents.analysis.agent import DataAnalysisAgent  # noqa: E402
from agents.progress import ProgressEmitter  # noqa: E402
from agents.sql_agent.agent import SQLAgent  # noqa: E402
from agents.state import AgentState, DataContext  # noqa: E402
from llm.infer import LLMRouter  # noqa: E402

# ── config ──────────────────────────────────────────────────────────────
# psycopg2 URL for the agent path; plain URL for the SQLAlchemy seed/verify path.
CONN_URL = os.getenv("E2E_CONN_URL", "postgresql+psycopg2://stych:stych123@localhost:5433/demo")
SCHEMA, TABLE = "public", "dirty_cafe_sales_test"
PER_QUERY_TIMEOUT_S = int(os.getenv("PER_QUERY_TIMEOUT_S", "300"))

PROMPT = (
    "Give me a comprehensive analysis of this cafe sales table. Include:\n"
    "1. Summary statistics for the numeric fields (quantity, price_per_unit, total_spent).\n"
    "2. Total and average revenue (use total_spent).\n"
    "3. The top items by total revenue.\n"
    "4. A breakdown of revenue and transaction counts by payment_method and by location.\n"
    "Note that some numeric columns may be stored as text with junk values."
)

RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
OUT_DIR = Path(__file__).resolve().parent.parent / "tests" / "test_runs" / f"sqlanalysis-{RUN_ID}"
OUT_DIR.mkdir(parents=True, exist_ok=True)
TRACE_PATH = OUT_DIR / "trace.jsonl"
RESULT_PATH = OUT_DIR / "results.json"

# A messy cafe-sales sample: every column TEXT, numeric columns peppered with sentinels
# (ERROR / UNKNOWN / blank) and the values stored as strings.
_ROWS = [
    # transaction_id, item, quantity, price_per_unit, total_spent, payment_method, location, date
    ("T1", "Coffee", "2", "2.0", "4.0", "Cash", "In-store", "2023-01-01"),
    ("T2", "Cake", "1", "3.0", "3.0", "Credit Card", "Takeaway", "2023-01-02"),
    ("T3", "Tea", "ERROR", "1.5", "UNKNOWN", "Cash", "In-store", "2023-01-02"),
    ("T4", "Coffee", "3", "2.0", "6.0", "Digital Wallet", "In-store", "2023-01-03"),
    ("T5", "Sandwich", "2", "4.0", "8.0", "Credit Card", "Takeaway", "2023-01-03"),
    ("T6", "Juice", "1", "UNKNOWN", "3.0", "Cash", "", "2023-01-04"),
    ("T7", "Cake", "2", "3.0", "6.0", "", "In-store", "2023-01-04"),
    ("T8", "Coffee", "1", "2.0", "2.0", "Digital Wallet", "Takeaway", "2023-01-05"),
    ("T9", "Cookie", "4", "1.0", "4.0", "Cash", "In-store", "ERROR"),
    ("T10", "Tea", "2", "1.5", "3.0", "Credit Card", "Takeaway", "2023-01-06"),
    ("T11", "Smoothie", "1", "5.0", "", "Digital Wallet", "In-store", "2023-01-06"),
    ("T12", "Coffee", "2", "2.0", "4.0", "Cash", "Takeaway", "2023-01-07"),
    ("T13", "Sandwich", "1", "4.0", "4.0", "Credit Card", "In-store", "2023-01-07"),
    ("T14", "Cake", "3", "3.0", "9.0", "Cash", "Takeaway", "2023-01-08"),
    ("T15", "Juice", "2", "3.0", "6.0", "Digital Wallet", "In-store", "2023-01-08"),
    ("T16", "Coffee", "1", "2.0", "2.0", "Cash", "In-store", "2023-01-09"),
    ("T17", "Cookie", "2", "1.0", "2.0", "Credit Card", "Takeaway", "2023-01-09"),
    ("T18", "Tea", "ERROR", "1.5", "1.5", "Cash", "In-store", "2023-01-10"),
    ("T19", "Smoothie", "2", "5.0", "10.0", "Digital Wallet", "Takeaway", "2023-01-10"),
    ("T20", "Coffee", "3", "2.0", "6.0", "Credit Card", "In-store", "2023-01-11"),
]
_COLS = (
    "transaction_id, item, quantity, price_per_unit, total_spent, "
    "payment_method, location, transaction_date"
)


def _seed_engine():
    return create_engine(CONN_URL, connect_args={"connect_timeout": 10})


def seed_table() -> None:
    ddl = f"""
        DROP TABLE IF EXISTS "{SCHEMA}"."{TABLE}";
        CREATE TABLE "{SCHEMA}"."{TABLE}" (
            transaction_id   TEXT,
            item             TEXT,
            quantity         TEXT,
            price_per_unit   TEXT,
            total_spent      TEXT,
            payment_method   TEXT,
            location         TEXT,
            transaction_date TEXT
        );
    """
    with _seed_engine().begin() as conn:
        for stmt in [s for s in ddl.split(";") if s.strip()]:
            conn.execute(text(stmt))
        conn.execute(
            text(
                f'INSERT INTO "{SCHEMA}"."{TABLE}" ({_COLS}) VALUES '
                "(:tid,:item,:qty,:ppu,:tot,:pay,:loc,:dt)"
            ),
            [
                {
                    "tid": r[0],
                    "item": r[1],
                    "qty": r[2],
                    "ppu": r[3],
                    "tot": r[4],
                    "pay": r[5],
                    "loc": r[6],
                    "dt": r[7],
                }
                for r in _ROWS
            ],
        )
    print(f"Seeded {SCHEMA}.{TABLE} with {len(_ROWS)} messy rows", flush=True)


def drop_table() -> None:
    with _seed_engine().begin() as conn:
        conn.execute(text(f'DROP TABLE IF EXISTS "{SCHEMA}"."{TABLE}"'))
    print(f"Dropped {SCHEMA}.{TABLE}", flush=True)


def load_config_bounded() -> dict:
    cfg_dir = Path(__file__).resolve().parent.parent / "config"
    config = yaml.safe_load((cfg_dir / "config.yaml").read_text()) or {}
    routing = yaml.safe_load((cfg_dir / "routing_config.yaml").read_text()) or {}

    def merge(a, b):
        for k, v in b.items():
            if isinstance(v, dict) and isinstance(a.get(k), dict):
                merge(a[k], v)
            else:
                a[k] = v
        return a

    merge(config, routing)

    def sub(o):
        if isinstance(o, dict):
            return {k: sub(v) for k, v in o.items()}
        if isinstance(o, list):
            return [sub(x) for x in o]
        if isinstance(o, str) and o.startswith("${") and o.endswith("}"):
            inner = o[2:-1]
            i = inner.find(":")
            var, default = (inner, "") if i < 0 else (inner[:i], inner[i + 1 :])
            return os.getenv(var, default)
        return o

    config = sub(config)
    config.setdefault("llm", {})["max_tokens"] = int(os.getenv("MAX_TOKENS", "2048"))
    config["llm"]["temperature"] = 0
    return config


async def drain(queue: asyncio.Queue, sink: list):
    while True:
        ev = await queue.get()
        if ev is None:
            return
        sink.append(ev)
        if ev.get("type") == "part.updated":
            p = ev.get("part", {})
            if p.get("agent") == "analysis":
                print(
                    f"  step#{p.get('step')} {p.get('phase')} {p.get('action')} :: "
                    f"{(p.get('observation') or p.get('thought') or '')[:140]}",
                    flush=True,
                )


def build_state(config: dict) -> AgentState:
    # Small preview frame (SQL working_dataframe). The agent reads the REAL schema from
    # Postgres; this is only used for the framework's row/column bookkeeping.
    preview = pd.DataFrame(_ROWS[:10], columns=[c.strip() for c in _COLS.split(",")])
    dc = DataContext(
        file_id=f"postgres:{SCHEMA}.{TABLE}",
        filename=f"PostgreSQL: {SCHEMA}.{TABLE}",
        raw_dataframe=preview.copy(deep=True),
        working_dataframe=preview.copy(deep=True),
        uploaded_at=datetime.now(),
        metadata={
            "source": "postgres",
            "connection_url": CONN_URL,
            "schema": SCHEMA,
            "table": TABLE,
            "custom_query": None,
            "max_rows": 1_000_000,
        },
    )
    state = AgentState(session_id=f"sqlanalysis-{RUN_ID}")
    state.metadata.update({"user_id": "driver", "is_anonymous": True})
    state.set_data_context(dc)
    return state


def analyze_trace(parts: list, response: str) -> dict:
    # emitter.parts dedupes start/end by part id, so each step survives once (final phase).
    # Count run_sql steps regardless of phase.
    analysis_parts = [p for p in parts if p.get("agent") == "analysis"]
    run_sql_parts = [p for p in analysis_parts if p.get("action") == "run_sql"]
    sql_errors = [p for p in run_sql_parts if "ERROR" in str(p.get("observation", "")).upper()]
    recovered = bool(sql_errors) and len(run_sql_parts) > len(sql_errors)
    return {
        "run_sql_calls": len(run_sql_parts),
        "sql_errors_seen": len(sql_errors),
        "recovered_from_error": recovered,
        # The fix: more than one SQL query (decomposition), not the old single-shot.
        "multi_query": len(run_sql_parts) >= 2,
        "response_len": len(response or ""),
    }


async def main() -> dict:
    config = load_config_bounded()
    llm_router = LLMRouter(config.get("llm", {}))
    sql_agent = SQLAgent(llm_router)
    analysis_agent = DataAnalysisAgent(llm_router, sql_agent=sql_agent, config=config)
    print(
        f"sql_react_max_steps={config.get('analysis', {}).get('sql_react_max_steps')} "
        f"provider={config['llm'].get('provider')}",
        flush=True,
    )

    state = build_state(config)
    state.add_message("user", PROMPT)

    queue: asyncio.Queue = asyncio.Queue(maxsize=4000)
    emitter = ProgressEmitter(queue)
    events: list = []
    drainer = asyncio.create_task(drain(queue, events))

    print("════════ ANALYSIS PROMPT:\n" + PROMPT, flush=True)
    t0 = time.time()
    result = await asyncio.wait_for(
        analysis_agent.process_query(state=state, message=PROMPT, progress_emitter=emitter),
        timeout=PER_QUERY_TIMEOUT_S,
    )
    await emitter.finish()
    try:
        await asyncio.wait_for(drainer, timeout=5)
    except asyncio.TimeoutError:
        drainer.cancel()

    response = result.get("response", "")
    metrics = analyze_trace(emitter.parts, response)
    print("\n════════ RESPONSE:\n" + (response or "")[:2000], flush=True)
    print("\n════════ METRICS:", json.dumps(metrics, indent=2), flush=True)

    with open(TRACE_PATH, "w") as f:
        for ev in events:
            f.write(json.dumps(ev, default=str) + "\n")

    return {
        "run_id": RUN_ID,
        "table": f"{SCHEMA}.{TABLE}",
        "prompt": PROMPT,
        "status": result.get("status"),
        "seconds": round(time.time() - t0, 2),
        "metrics": metrics,
        "response": response,
        "parts": emitter.parts,
    }


if __name__ == "__main__":
    code = 0
    out = {"run_id": RUN_ID}
    try:
        seed_table()
        out = asyncio.run(main())
        m = out["metrics"]
        # Pass gate: the agent decomposed into multiple SQL queries, ended in a completed
        # status, produced a substantive answer, and recovered from any SQL error it hit.
        passed = (
            m["multi_query"]
            and out["status"] == "completed"
            and m["response_len"] > 200
            and (m["sql_errors_seen"] == 0 or m["recovered_from_error"])
        )
        out["passed"] = passed
        code = 0 if passed else 3
    except Exception as e:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        out["error"] = repr(e)
        out["passed"] = False
        code = 1
    finally:
        try:
            drop_table()
        except Exception as e:  # noqa: BLE001
            print(f"teardown drop failed: {e!r}", flush=True)
        RESULT_PATH.write_text(json.dumps(out, indent=2, default=str))
        print(f"\n=== passed={out.get('passed')} -> {RESULT_PATH}", flush=True)
    sys.exit(code)
