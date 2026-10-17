"""
Reproduction driver for the copy-table write-back reliability bug.

Symptom (manual): the agent claims a full multi-step clean (nulls, dupes, casing,
country standardization) but the persisted copy table is unchanged except an
incidental dedup. Root hypothesis: cleaning that is not funneled through the temp
table + a trivial `SELECT * FROM {temp}` materialize silently fails to reach the copy.

This runs ONE broad "clean everything then write back" prompt REPEAT times against a
freshly-dropped copy table, then checks the persisted copy against the source and
cross-checks the agent's textual claims against DB reality. Non-determinism means a
single pass can look fine; repeating exposes the failure.

Run:  .venv/bin/python -m tools.run_writeback_repro
Env:  REPEAT (default 3), PER_QUERY_TIMEOUT_S, MAX_RESUME_ROUNDS
"""

import asyncio
import json
import os
import time

import pandas as pd  # noqa: E402
from sqlalchemy import text  # noqa: E402

from agents.orchestrator.agent import OrchestratorAgent  # noqa: E402
from agents.progress import ProgressEmitter  # noqa: E402
from agents.state import AgentState  # noqa: E402
from datasources.postgres import load_from_postgres  # noqa: E402
from llm.infer import LLMRouter  # noqa: E402
from tools.run_agentic_pg_tests import (  # noqa: E402  (module bootstraps dotenv/logging)
    CONN_URL,
    MAX_RESUME_ROUNDS,
    PER_QUERY_TIMEOUT_S,
    SCHEMA,
    TABLE,
    _build_approval,
    _direct_engine,
    build_data_context,
    drain,
    drop_copy_table,
    load_config_bounded,
    log,
)

REPEAT = int(os.getenv("REPEAT", "3"))

BROAD_PROMPT = (
    "Clean the orders table thoroughly: remove duplicate orders, drop rows that are "
    "missing critical values, fix the inconsistent/misspelled status values (e.g. "
    "'shiped' -> 'Shipped', 'DELIVERED' -> 'Delivered'), and standardize country names "
    "('United States' -> 'USA'). Then persist the fully cleaned result back to the "
    "PostgreSQL database as a copy table."
)


def source_snapshot() -> dict:
    """Distinct dirty values + row count in the untouched source table."""
    with _direct_engine().connect() as conn:
        tbl = f'"{SCHEMA}"."{TABLE}"'
        return {
            "rows": conn.execute(text(f"SELECT count(*) FROM {tbl}")).scalar(),
            "status": sorted(
                r[0] for r in conn.execute(text(f"SELECT DISTINCT status FROM {tbl}")) if r[0]
            ),
            "country": sorted(
                r[0] for r in conn.execute(text(f"SELECT DISTINCT country FROM {tbl}")) if r[0]
            ),
        }


def copy_snapshot() -> dict:
    with _direct_engine().connect() as conn:
        reg = conn.execute(text("SELECT to_regclass(:t)"), {"t": f"{SCHEMA}.copy_{TABLE}"}).scalar()
        if reg is None:
            return {"exists": False}
        tbl = f'"{SCHEMA}"."copy_{TABLE}"'
        return {
            "exists": True,
            "rows": conn.execute(text(f"SELECT count(*) FROM {tbl}")).scalar(),
            "status": sorted(
                r[0] for r in conn.execute(text(f"SELECT DISTINCT status FROM {tbl}")) if r[0]
            ),
            "country": sorted(
                r[0] for r in conn.execute(text(f"SELECT DISTINCT country FROM {tbl}")) if r[0]
            ),
        }


def classify_path(parts: list) -> dict:
    """Inspect the tool actions to see HOW the agent tried to write the copy."""
    info = {
        "sql_write_temp_count": 0,
        "run_script_count": 0,
        "materialize_select": None,
        "materialize_source_target": None,
        "used_sql_write_copy": False,
    }
    for p in parts:
        act = p.get("action")
        inp = p.get("action_input") or p.get("input") or {}
        if isinstance(inp, str):
            try:
                inp = json.loads(inp)
            except Exception:
                inp = {}
        if act == "sql_write_temp":
            info["sql_write_temp_count"] += 1
        elif act == "run_script":
            info["run_script_count"] += 1
        elif act == "sql_materialize_copy":
            if inp.get("select_sql"):
                info["materialize_select"] = inp.get("select_sql")[:300]
                info["materialize_source_target"] = inp.get("source_target", "temp")
        elif act == "sql_write_copy":
            info["used_sql_write_copy"] = True
    return info


async def run_once(orch, dc, idx: int) -> dict:
    drop_copy_table()
    state = AgentState(session_id=f"repro-{idx}-{int(time.time())}")
    state.metadata.update({"user_id": "repro", "is_anonymous": True})
    state.data_context = dc

    queue: asyncio.Queue = asyncio.Queue(maxsize=4000)
    emitter = ProgressEmitter(queue)
    events: list = []
    drainer = asyncio.create_task(drain(queue, events))

    rec = {"idx": idx, "approval_rounds": 0, "permission_prompts": []}
    # Faithful to the SSE route: the user message is recorded on the session before execute.
    state.add_message("user", BROAD_PROMPT)
    cur_msg, cur_action, result = BROAD_PROMPT, None, None
    try:
        for round_i in range(MAX_RESUME_ROUNDS + 1):
            result = await asyncio.wait_for(
                orch.execute(
                    task_type="cleaning",
                    message=cur_msg,
                    state=state,
                    session_id=state.session_id,
                    progress_emitter=emitter,
                    user_action=cur_action,
                ),
                timeout=PER_QUERY_TIMEOUT_S,
            )
            ap = result.get("action_prompt")
            if result.get("status") != "awaiting_user_action":
                break
            if not ap:
                rec["warn"] = "awaiting_user_action with no action_prompt"
                break
            rec["permission_prompts"].append((ap or {}).get("question"))
            cur_action = _build_approval(ap)
            cur_msg = "__user_action__"
        rec["approval_rounds"] = len(rec["permission_prompts"])
        rec["claim"] = (result or {}).get("response", "")
    except Exception as e:  # noqa: BLE001
        rec["error"] = repr(e)
        log.exception("repro %s failed", idx)
    finally:
        await emitter.finish()
        try:
            await asyncio.wait_for(drainer, timeout=5)
        except asyncio.TimeoutError:
            drainer.cancel()
    rec["path"] = classify_path(emitter.parts)
    rec["copy"] = copy_snapshot()
    return rec


def grade(rec: dict, src: dict) -> dict:
    copy = rec.get("copy", {})
    claim = (rec.get("claim") or "").lower()
    # The agent almost always CLAIMS it persisted a cleaned copy for this prompt.
    claimed_persistence = any(
        w in claim for w in ("persist", "copy table", "copy_orders", "saved to", "database as")
    )
    checks = {"claimed_persistence": claimed_persistence}

    if not copy.get("exists"):
        checks["copy_exists"] = False
        # Agent said it persisted, but there is no copy table at all.
        checks["false_success_claim"] = claimed_persistence
        rec["checks"] = checks
        rec["passed"] = False
        return rec

    checks["copy_exists"] = True
    checks["status_typo_fixed"] = (
        "shiped" not in copy["status"] and "DELIVERED" not in copy["status"]
    )
    checks["country_standardized"] = "United States" not in copy["country"]
    # The copy must actually differ from source on the dirty dimensions it claimed to fix.
    checks["copy_differs_from_source"] = (
        copy["status"] != src["status"] or copy["country"] != src["country"]
    )
    # False success = claimed persistence but copy is effectively unchanged vs source
    # (the "same except one row removed" symptom) or still dirty.
    checks["false_success_claim"] = claimed_persistence and not (
        checks["status_typo_fixed"] and checks["country_standardized"]
    )
    rec["checks"] = checks
    rec["passed"] = (
        checks["status_typo_fixed"]
        and checks["country_standardized"]
        and checks["copy_differs_from_source"]
    )
    return rec


async def main():
    config = load_config_bounded()
    llm_router = LLMRouter(config.get("llm", {}))
    orch = OrchestratorAgent(llm_router, config=config)
    df, label = load_from_postgres(connection_url=CONN_URL, schema=SCHEMA, table=TABLE)
    for c in ("quantity", "unit_price"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    dc = build_data_context(df, label)
    src = source_snapshot()
    log.info("SOURCE: %s", src)

    results = []
    for i in range(1, REPEAT + 1):
        log.info("════════ REPRO ITERATION %s/%s", i, REPEAT)
        rec = grade(await run_once(orch, dc, i), src)
        results.append(rec)
        log.info(
            "ITER %s passed=%s rounds=%s path=%s copy.status=%s copy.country=%s",
            i,
            rec.get("passed"),
            rec.get("approval_rounds"),
            rec.get("path"),
            rec.get("copy", {}).get("status"),
            rec.get("copy", {}).get("country"),
        )
    false_success = sum(1 for r in results if r.get("checks", {}).get("false_success_claim"))
    return {
        "source": src,
        "iterations": results,
        "passed": sum(1 for r in results if r.get("passed")),
        "false_success": false_success,
        "total": len(results),
    }


if __name__ == "__main__":
    out = asyncio.run(main())
    print("\n================ REPRO SUMMARY ================")
    print(json.dumps(out, indent=2, default=str))
    print(f"\nPASS {out['passed']}/{out['total']} iterations produced a fully-cleaned copy table")
    print(
        f"FALSE-SUCCESS claims (claimed persist but copy missing/unchanged): {out['false_success']}"
    )
