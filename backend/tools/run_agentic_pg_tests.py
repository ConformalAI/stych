"""
Agentic driver: run stych's REAL multi-agent pipeline (OrchestratorAgent + ReAct
sub-agents) over a live Postgres table, capturing the full agent trace + logs.

Token-bounded on purpose:
  - tiny table (demo.public.orders, 26 rows)
  - max 2 short queries (1 analysis, 1 cleaning) — override with N_QUERIES
  - react step cap lowered to 5, max_tokens 1024, temperature 0

Hard 200s total budget via SIGALRM + a per-query asyncio timeout.

Run from backend/:  .venv/bin/python -m tools.run_agentic_pg_tests
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()  # .env must load before stych imports read env

import pandas as pd  # noqa: E402

from agents.orchestrator.agent import OrchestratorAgent  # noqa: E402
from agents.progress import ProgressEmitter  # noqa: E402
from agents.state import AgentState, DataContext  # noqa: E402
from datasources.postgres import load_from_postgres  # noqa: E402
from llm.infer import LLMRouter  # noqa: E402

# ── config ──────────────────────────────────────────────────────────────
CONN_URL = "postgresql+psycopg2://stych:stych123@localhost:5433/demo"
SCHEMA, TABLE = "public", "orders"
TOTAL_BUDGET_S = int(os.getenv("TOTAL_BUDGET_S", "900"))
PER_QUERY_TIMEOUT_S = int(os.getenv("PER_QUERY_TIMEOUT_S", "240"))
# Max auto-approval rounds per query. Write-back needs ≥2 (create_copy_table +
# sql_materialize_copy). A higher cap also surfaces the permission-loop bug: if a
# query keeps re-asking past this, we flag it instead of looping forever.
MAX_RESUME_ROUNDS = int(os.getenv("MAX_RESUME_ROUNDS", "8"))

# Five prompts exercising analysis, cleaning, and the Postgres tool surface
# (sql_read, create_copy_table, sql_materialize_copy write-back, and a verify read).
QUERIES = [
    ("auto", "How many orders are there, and which order status is most common?"),
    ("cleaning", "Give me a short data-quality report: missing values and duplicates only."),
    (
        "auto",
        "Using SQL against the database, list each distinct order status and how many "
        "orders have it.",
    ),
    (
        "cleaning",
        "Clean the orders table thoroughly: remove duplicate orders, drop rows missing "
        "critical values, fix the misspelled/inconsistent status values (e.g. 'shiped' and "
        "the casing of 'DELIVERED' should become 'Shipped'/'Delivered'), and standardize "
        "country names ('United States' -> 'USA'). Then persist the fully cleaned result "
        "back to the Postgres database as a copy table.",
    ),
    (
        "auto",
        "Read the persisted copy table back from the database and show me its distinct "
        "status and country values so I can confirm the cleaning was applied.",
    ),
]
N_QUERIES = int(os.getenv("N_QUERIES", str(len(QUERIES))))
QUERIES = QUERIES[:N_QUERIES]

RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
OUT_DIR = Path(__file__).resolve().parent.parent / "tests" / "test_runs" / f"agentic-{RUN_ID}"
OUT_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = OUT_DIR / "run.log"
RESULT_PATH = OUT_DIR / "results.json"
TRACE_PATH = OUT_DIR / "trace.jsonl"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("agentic_driver")


class TimeLimit(Exception):
    pass


def _on_alarm(signum, frame):
    raise TimeLimit(f"exceeded {TOTAL_BUDGET_S}s total budget")


signal.signal(signal.SIGALRM, _on_alarm)
signal.alarm(TOTAL_BUDGET_S)


def load_config_bounded() -> dict:
    """Replicate main.load_config (yaml merge + ${ENV} substitution), then clamp
    cost knobs so an agentic run can't run away on tokens."""
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

    # ── cost clamps ──
    # Looser than the original 2-query smoke test: the write-back query needs room to
    # inspect → create_copy_table → materialize → finalize across multiple tool calls.
    config.setdefault("llm", {})
    config["llm"]["max_tokens"] = int(os.getenv("MAX_TOKENS", "2048"))
    config["llm"]["temperature"] = 0
    config.setdefault("agents", {})["max_iterations"] = int(os.getenv("MAX_ITERATIONS", "10"))
    config.setdefault("routing", {}).setdefault("orchestrate_react", {})["react_max_steps"] = int(
        os.getenv("REACT_MAX_STEPS", "14")
    )
    return config


async def drain(queue: asyncio.Queue, sink: list):
    """Consume the ProgressEmitter queue, recording every trace event."""
    while True:
        ev = await queue.get()
        if ev is None:  # finish() sentinel
            return
        sink.append(ev)
        et = ev.get("type")
        if et == "thinking":
            log.info("  ↳ [%s] %s", ev.get("agent"), ev.get("message"))
        elif et == "part.updated":
            p = ev.get("part", {})
            log.info(
                "  ↳ step#%s %s/%s %s",
                p.get("step"),
                p.get("agent", "?"),
                p.get("phase", p.get("kind", "")),
                (p.get("thought") or p.get("action") or p.get("observation") or "")[:160],
            )


def build_data_context(df, label):
    now = datetime.now()
    return DataContext(
        file_id=f"postgres:{SCHEMA}.{TABLE}",
        filename=f"PostgreSQL: {label}",
        raw_dataframe=df.copy(deep=True),
        working_dataframe=df.copy(deep=True),
        uploaded_at=now,
        metadata={
            "user": "driver",
            "source": "postgres",
            "source_label": label,
            "connection_url": CONN_URL,
            "schema": SCHEMA,
            "table": TABLE,
            "custom_query": None,
            "max_rows": 1_000_000,
        },
    )


def _build_approval(action_prompt: dict) -> dict:
    """Auto-approve a pending HITL permission/question prompt (mirrors the UI's
    'Yes, proceed' button -> UserActionRequest{prompt_id, kind, value})."""
    ap = action_prompt or {}
    return {
        "prompt_id": ap.get("prompt_id", ""),
        "kind": ap.get("kind", "cleaning_react_permission"),
        "value": "accept",
    }


async def run_query(orch, state, df_dc, task_type, message, idx):
    state.data_context = df_dc

    queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
    emitter = ProgressEmitter(queue)
    events: list = []
    drainer = asyncio.create_task(drain(queue, events))

    log.info("════════ QUERY %s [%s]: %s", idx, task_type, message)
    t0 = time.time()
    rec = {"idx": idx, "task_type": task_type, "message": message, "rounds": []}
    try:
        # Drive the turn, auto-approving any HITL pause and resuming the SAME session
        # until the agent finishes (or we hit the resume cap, which flags a perm loop).
        # Faithful to the SSE route: record the user message on the session before execute.
        state.add_message("user", message)
        cur_message, cur_user_action, result = message, None, None
        for round_i in range(MAX_RESUME_ROUNDS + 1):
            result = await asyncio.wait_for(
                orch.execute(
                    task_type=task_type,
                    message=cur_message,
                    state=state,
                    session_id=state.session_id,
                    progress_emitter=emitter,
                    user_action=cur_user_action,
                ),
                timeout=PER_QUERY_TIMEOUT_S,
            )
            status = result.get("status")
            ap = result.get("action_prompt")
            rec["rounds"].append(
                {
                    "round": round_i,
                    "status": status,
                    "prompt_kind": (ap or {}).get("kind"),
                    "prompt_id": (ap or {}).get("prompt_id"),
                    "question": (ap or {}).get("question"),
                }
            )
            log.info(
                "  query %s round %s -> status=%s prompt=%r",
                idx,
                round_i,
                status,
                (ap or {}).get("question", "")[:120],
            )
            if status != "awaiting_user_action":
                break
            if not ap:
                rec["warn"] = "awaiting_user_action but no action_prompt; cannot auto-resume"
                log.error("QUERY %s: %s", idx, rec["warn"])
                break
            cur_user_action = _build_approval(ap)
            cur_message = "__user_action__"
        else:
            rec["warn"] = (
                f"still awaiting approval after {MAX_RESUME_ROUNDS} resume rounds "
                "(possible permission loop)"
            )
            log.error("QUERY %s: %s", idx, rec["warn"])

        rec["ok"] = result is not None and "warn" not in rec
        rec["status"] = result.get("status") if result else None
        rec["agents_used"] = result.get("agents_used") if result else None
        rec["response"] = result.get("response") if result else None
        rec["approval_rounds"] = max(0, len(rec["rounds"]) - 1)
        log.info(
            "QUERY %s DONE status=%s agents=%s approvals=%s in %.1fs",
            idx,
            rec["status"],
            rec["agents_used"],
            rec["approval_rounds"],
            time.time() - t0,
        )
        log.info("RESPONSE:\n%s", (rec["response"] or "")[:1500])
    except asyncio.TimeoutError:
        rec["ok"] = False
        rec["error"] = f"per-query timeout {PER_QUERY_TIMEOUT_S}s"
        log.error("QUERY %s TIMED OUT after %ss", idx, PER_QUERY_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        rec["ok"] = False
        rec["error"] = repr(e)
        log.exception("QUERY %s FAILED", idx)
    finally:
        rec["seconds"] = round(time.time() - t0, 2)
        await emitter.finish()
        try:
            await asyncio.wait_for(drainer, timeout=5)
        except asyncio.TimeoutError:
            drainer.cancel()
        rec["trace_event_count"] = len(events)
        rec["parts"] = emitter.parts
        with open(TRACE_PATH, "a") as f:
            for ev in events:
                f.write(json.dumps({"query": idx, **ev}, default=str) + "\n")
    return rec


def _direct_engine():
    """A plain SQLAlchemy engine for out-of-band DB setup/verification (not the agent path)."""
    from sqlalchemy import create_engine

    return create_engine(CONN_URL)


def drop_copy_table() -> None:
    """Remove any leftover copy table so the write-back query exercises create + materialize."""
    from sqlalchemy import text

    try:
        with _direct_engine().begin() as conn:
            conn.execute(text(f'DROP TABLE IF EXISTS "{SCHEMA}"."copy_{TABLE}"'))
        log.info("Dropped pre-existing %s.copy_%s (clean write-back baseline)", SCHEMA, TABLE)
    except Exception as e:  # noqa: BLE001
        log.warning("Could not drop copy table (continuing): %r", e)


def verify_copy_table() -> dict:
    """Confirm the write-back landed: copy table exists, is populated, and cleaning applied."""
    from sqlalchemy import text

    out: dict = {"copy_table": f"{SCHEMA}.copy_{TABLE}"}
    try:
        with _direct_engine().connect() as conn:
            reg = conn.execute(
                text("SELECT to_regclass(:t)"), {"t": f"{SCHEMA}.copy_{TABLE}"}
            ).scalar()
            out["exists"] = reg is not None
            if not out["exists"]:
                return out
            tbl = f'"{SCHEMA}"."copy_{TABLE}"'
            out["row_count"] = conn.execute(text(f"SELECT count(*) FROM {tbl}")).scalar()
            out["distinct_status"] = sorted(
                r[0] for r in conn.execute(text(f"SELECT DISTINCT status FROM {tbl}")) if r[0]
            )
            out["distinct_country"] = sorted(
                r[0] for r in conn.execute(text(f"SELECT DISTINCT country FROM {tbl}")) if r[0]
            )
        out["populated"] = (out.get("row_count") or 0) > 0
        out["status_typo_fixed"] = (
            "shiped" not in out["distinct_status"] and "DELIVERED" not in out["distinct_status"]
        )
        out["country_standardized"] = "United States" not in out["distinct_country"]
        # Strict gate: the write-back genuinely landed the cleaning (not just a stale/empty
        # copy or an incidental dedup). This is what the earlier lenient check missed.
        out["passed"] = bool(
            out["populated"] and out["status_typo_fixed"] and out["country_standardized"]
        )
    except Exception as e:  # noqa: BLE001
        out["error"] = repr(e)
        out["passed"] = False
    return out


async def main():
    log.info("Output dir: %s", OUT_DIR)
    config = load_config_bounded()
    log.info(
        "Cost clamps -> max_tokens=%s react_max_steps=%s max_iterations=%s",
        config["llm"]["max_tokens"],
        config["routing"]["orchestrate_react"]["react_max_steps"],
        config["agents"]["max_iterations"],
    )

    llm_router = LLMRouter(config.get("llm", {}))
    log.info(
        "LLM provider=%s model=%s",
        config["llm"].get("provider"),
        config["llm"].get("litellm", {}).get("default_model"),
    )

    # quick health probe so we fail fast & informatively if the endpoint is down
    try:
        health = await asyncio.wait_for(llm_router.health_check(), timeout=20)
        ok = [k for k, v in health.items() if v.get("status") == "healthy"]
        log.info("LLM health: healthy=%s detail=%s", ok, health)
    except Exception as e:  # noqa: BLE001
        log.warning("LLM health check error (continuing): %r", e)

    orch = OrchestratorAgent(llm_router, config=config)
    log.info("Orchestrator initialized")

    df, label = load_from_postgres(connection_url=CONN_URL, schema=SCHEMA, table=TABLE)
    for c in ("quantity", "unit_price"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    log.info("Loaded %s x %s from %s", df.shape[0], df.shape[1], label)
    dc = build_data_context(df, label)

    # Clean baseline so the write-back query runs create_copy_table + materialize from scratch.
    drop_copy_table()

    # One shared session/state across all prompts (mirrors a real multi-turn conversation).
    state = AgentState(session_id=f"driver-{RUN_ID}")
    state.metadata.update({"user_id": "driver", "is_anonymous": True})

    records = []
    for i, (tt, msg) in enumerate(QUERIES, 1):
        records.append(await run_query(orch, state, dc, tt, msg, i))

    verification = verify_copy_table()
    log.info("Copy-table verification: %s", verification)
    return {"queries": records, "copy_table_verification": verification}


if __name__ == "__main__":
    out = {"run_id": RUN_ID, "table": f"{SCHEMA}.{TABLE}", "queries": []}
    code = 0
    try:
        res = asyncio.run(main())
        out["queries"] = res["queries"]
        out["copy_table_verification"] = res["copy_table_verification"]
        out["status"] = "completed"
    except TimeLimit as e:
        out["status"] = f"time_limit: {e}"
        log.error("TIME LIMIT: %s", e)
        code = 2
    except Exception as e:  # noqa: BLE001
        out["status"] = f"error: {e!r}"
        log.exception("FATAL")
        code = 1
    finally:
        signal.alarm(0)
        n_ok = sum(1 for q in out["queries"] if q.get("ok"))
        out["summary"] = {"queries_ok": n_ok, "queries_total": len(out["queries"])}
        RESULT_PATH.write_text(json.dumps(out, indent=2, default=str))
        log.info("=== DONE %s/%s queries OK -> %s", n_ok, len(out["queries"]), RESULT_PATH)
    sys.exit(code)
