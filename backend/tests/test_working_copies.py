"""
Working-copy lifecycle tests: the metadata registry, SQLAgent branch/switch/
drop/list, verify-on-touch (a restored ref is checked, not wiped), opportunistic
GC, the ``copy:<name>`` adapter variant, and restore keeping temp refs.

No live database — a fake runner records DDL. These pin the lifecycle model:
one session holds many named copies branching from the source or from each
other; the active copy is what agents and the preview operate on.
"""

from datetime import datetime

import pandas as pd
import pytest

from agents.sql_agent import working_copies as wc
from agents.sql_agent.agent import SQLAgent
from agents.sql_agent.runner import dataset_table_prefix
from agents.state import AgentState, DataContext, _restore_sensitive_meta
from datasources import adapters as table_adapters
from tools.sql_copies import execute_working_copy_tool

URL = "postgresql://localhost/db"


class _FakeRunner:
    temp_schema = "stych_temp"
    credential_key = URL

    def __init__(self):
        self.tables = set()  # physical stych_temp table names
        self.dropped = []
        self.created_from_source = []

    def quote_relation(self, schema, table):
        return f'"{schema}"."{table}"'

    def create_temp_table_from_source(self, session_id, table_name=None, **kwargs):
        self.tables.add(table_name)
        self.created_from_source.append(table_name)
        return ("stych_temp", table_name)

    def duplicate_temp_table(self, source_table, new_table):
        assert source_table in self.tables, f"branch source {source_table} missing"
        self.tables.add(new_table)

    def drop_temp_table(self, table):
        self.tables.discard(table)
        self.dropped.append(table)

    def table_exists(self, schema, table):
        return table in self.tables

    def list_temp_tables_with_prefix(self, prefix):
        return [t for t in self.tables if t.startswith(prefix)]

    def get_row_count(self, schema, table):
        if table not in self.tables:
            raise RuntimeError("missing table")
        return 42


def _state(session_id="sess-wc-1"):
    raw = pd.DataFrame({"a": [1]})
    st = AgentState(session_id=session_id)
    st.set_data_context(
        DataContext(
            file_id="f",
            filename="orders",
            raw_dataframe=raw.copy(deep=True),
            working_dataframe=raw.copy(deep=True),
            uploaded_at=datetime.now(),
            metadata={
                "source": "postgres",
                "connection_url": URL,
                "schema": "public",
                "table": "orders",
            },
        )
    )
    return st


def _agent(fake=None):
    agent = SQLAgent(llm_router=None)
    fake = fake or _FakeRunner()
    # _runner_for is the seam the agent actually uses; stubbing it keeps these
    # lifecycle tests off a live database.
    agent._runner = lambda url: fake
    agent._runner_for = lambda state: fake
    return agent, fake


# ---------------------------------------------------------------- registry


def test_registry_register_switch_remove_mirrors_legacy_keys():
    meta = {}
    wc.register_copy(meta, name="working", table="t1")
    assert meta["temp_table"] == "t1" and meta["temp_schema"] == "stych_temp"
    wc.register_copy(meta, name="v2", table="t2", parent="working")
    assert meta["active_copy"] == "v2" and meta["temp_table"] == "t2"
    wc.set_active(meta, "working")
    assert meta["temp_table"] == "t1"
    wc.remove_copy(meta, "working")  # active removed → most recent remaining
    assert meta["active_copy"] == "v2" and meta["temp_table"] == "t2"
    wc.remove_copy(meta, "v2")
    assert "temp_table" not in meta and "active_copy" not in meta


def test_bind_scratch_schema_repairs_case_sensitive_legacy_mirror():
    meta = {}
    wc.register_copy(meta, name="working", table="t1")
    assert meta["temp_schema"] == "stych_temp"

    assert wc.bind_scratch_schema(meta, "STYCH_TEMP") is True
    assert meta["scratch_schema"] == "STYCH_TEMP"
    assert meta["temp_schema"] == "STYCH_TEMP"
    assert wc.bind_scratch_schema(meta, "STYCH_TEMP") is False


def test_registry_rejects_bad_names_duplicates_and_limit():
    meta = {}
    with pytest.raises(wc.WorkingCopyError):
        wc.validate_name("has spaces")
    with pytest.raises(wc.WorkingCopyError):
        wc.validate_name("source")
    wc.register_copy(meta, name="working", table="t1")
    with pytest.raises(wc.WorkingCopyError):
        wc.register_copy(meta, name="working", table="t2")
    for i in range(wc.MAX_COPIES - 1):
        wc.register_copy(meta, name=f"c{i}", table=f"tt{i}")
    with pytest.raises(wc.WorkingCopyError):
        wc.register_copy(meta, name="overflow", table="tx")


def test_adopt_legacy_promotes_pre_registry_session():
    meta = {"temp_schema": "stych_temp", "temp_table": "old_t"}
    wc.adopt_legacy(meta)
    assert wc.active_copy(meta)["table"] == "old_t"
    assert wc.active_copy(meta)["name"] == "working"


# ---------------------------------------------------------------- SQLAgent


async def test_ensure_creates_and_registers_first_copy():
    agent, fake = _agent()
    state = _state()
    schema, table = await agent.ensure_temp_table(state)
    assert schema == "stych_temp" and table in fake.tables
    meta = state.data_context.metadata
    assert wc.active_copy(meta)["table"] == table
    # Idempotent: second call returns the same table without creating another.
    assert (await agent.ensure_temp_table(state))[1] == table
    # Two physical tables: the pinned snapshot of the source, and the working
    # copy cloned from it so both carry identical row ids.
    assert len(fake.tables) == 2
    pin = wc.pinned_base(meta)
    assert pin is not None and pin["table"] != table
    assert wc.active_copy(meta)["name"] != wc.PINNED_BASE_NAME


async def test_read_target_stays_live_until_a_working_copy_exists():
    agent, fake = _agent()
    state = _state()

    target = await agent.resolve_read_target(state)

    assert target == {
        "ref": '"public"."orders"',
        "schema": "public",
        "table": "orders",
        "working_copy": False,
    }
    assert fake.created_from_source == []


async def test_attached_read_binds_placeholder_without_creating_copy():
    agent, fake = _agent()
    state = _state()
    seen = []

    async def _run_frames(_state, sql, *, ensure_working_copy=True):
        seen.append((sql, ensure_working_copy))
        return [pd.DataFrame({"n": [1]})]

    agent.run_sql_frames = _run_frames
    await agent.run_attached_sql_frames(state, "SELECT COUNT(*) n FROM {table}")

    assert seen == [('SELECT COUNT(*) n FROM "public"."orders"', False)]
    assert fake.created_from_source == []


async def test_restored_ref_is_verified_not_wiped():
    # A session restored from snapshot keeps its registry; the physical table
    # still exists, so the same table (with all its modifications) is used.
    agent, fake = _agent()
    fake.tables.add("t_restored")
    state = _state()
    wc.register_copy(state.data_context.metadata, name="working", table="t_restored")
    _, table = await agent.ensure_temp_table(state)
    assert table == "t_restored"
    assert fake.created_from_source == []  # nothing rebuilt


async def test_missing_physical_table_is_recreated():
    # The registry references a table that is gone (dropped externally) —
    # recreate from source instead of returning a dead ref (the old bricked state).
    agent, fake = _agent()
    state = _state()
    wc.register_copy(state.data_context.metadata, name="working", table="t_gone")
    _, table = await agent.ensure_temp_table(state)
    assert table != "t_gone" and table in fake.tables
    assert wc.active_copy(state.data_context.metadata)["table"] == table


async def test_branch_from_copy_switch_and_drop():
    agent, fake = _agent()
    state = _state()
    await agent.ensure_temp_table(state)
    meta = state.data_context.metadata
    first = wc.active_copy(meta)

    entry = await agent.create_working_copy(state, name="v2", parent=first["name"])
    assert entry["parent"] == first["name"]
    assert wc.active_copy(meta)["name"] == "v2"
    assert len(fake.tables) == 3  # pinned base + first copy + v2

    await agent.switch_working_copy(state, first["name"])
    assert meta["temp_table"] == first["table"]

    await agent.drop_working_copy(state, "v2")
    assert entry["table"] in fake.dropped
    assert [c["name"] for c in wc.list_copies(meta)] == [wc.PINNED_BASE_NAME, first["name"]]


async def test_gc_drops_only_unregistered_prefixed_tables():
    agent, fake = _agent()
    state = _state()
    prefix_table = f"{dataset_table_prefix(state.session_id, 'f')}orphan1"
    fake.tables.add(prefix_table)
    fake.tables.add("stych_other_session_t")  # different session — untouched
    await agent.ensure_temp_table(state)
    assert prefix_table in fake.dropped
    assert "stych_other_session_t" in fake.tables
    active_table = wc.active_copy(state.data_context.metadata)["table"]
    assert active_table in fake.tables


async def test_gc_never_drops_a_sibling_datasets_working_copies():
    """Two attachments may share one scratch schema and session.

    A fresh SQLAgent instance must not classify the other attachment's base or
    working table as an orphan merely because its registry is context-local.
    """
    fake = _FakeRunner()
    first = _state(session_id="shared-session")
    second = _state(session_id="shared-session")
    second.data_context.file_id = "f-2"
    second.data_context.filename = "customers"
    second.data_context.metadata["table"] = "customers"

    first_agent, _ = _agent(fake)
    await first_agent.ensure_temp_table(first)
    first_tables = set(fake.tables)

    second_agent, _ = _agent(fake)
    await second_agent.ensure_temp_table(second)

    assert first_tables <= fake.tables
    assert not (first_tables & set(fake.dropped))
    assert all(
        table.startswith(dataset_table_prefix("shared-session", "f")) for table in first_tables
    )


async def test_list_working_copies_reports_rows_and_active():
    agent, _ = _agent()
    state = _state()
    await agent.ensure_temp_table(state)
    await agent.create_working_copy(state, name="v2", parent="working")
    copies = await agent.list_working_copies(state)
    # The pinned snapshot is listed alongside the writable copies so it is
    # visible in the workspace rather than hidden scratch.
    assert {c["name"] for c in copies} == {wc.PINNED_BASE_NAME, "working", "v2"}
    assert [c["name"] for c in copies if c["active"]] == ["v2"]
    assert all(c["rows"] == 42 for c in copies)


# ---------------------------------------------------------------- agent tools


async def test_tool_create_switch_list_drop_roundtrip():
    agent, _ = _agent()
    state = _state()
    out = await execute_working_copy_tool(agent, state, "create_working_copy", {"name": "base-fix"})
    assert "base-fix" in out and "active" in out
    out = await execute_working_copy_tool(
        agent, state, "create_working_copy", {"name": "exp", "from": "base-fix"}
    )
    assert "exp" in out
    out = await execute_working_copy_tool(agent, state, "list_working_copies", {})
    assert "base-fix" in out and "exp" in out
    out = await execute_working_copy_tool(agent, state, "switch_working_copy", {"name": "base-fix"})
    assert "base-fix" in out
    out = await execute_working_copy_tool(agent, state, "drop_working_copy", {"name": "exp"})
    assert "Dropped" in out
    out = await execute_working_copy_tool(agent, state, "switch_working_copy", {"name": "nope"})
    assert out.startswith("ERROR")


async def test_tools_unavailable_for_file_sessions():
    agent, _ = _agent()
    raw = pd.DataFrame({"a": [1]})
    st = AgentState(session_id="s-file")
    st.set_data_context(
        DataContext(
            file_id="f",
            filename="d.csv",
            raw_dataframe=raw.copy(deep=True),
            working_dataframe=raw.copy(deep=True),
            uploaded_at=datetime.now(),
        )
    )
    out = await execute_working_copy_tool(agent, st, "list_working_copies", {})
    assert "ERROR" in out and "SQL datasource" in out


# ---------------------------------------------------------------- adapters


def test_resolve_adapter_copy_variant():
    state = _state()
    meta = state.data_context.metadata
    wc.register_copy(meta, name="working", table="t1")
    wc.register_copy(meta, name="v2", table="t2", parent="working")

    adapter = table_adapters.resolve_adapter(state.data_context, "copy:working")
    assert adapter.table == "t1"
    # "temp" resolves to the active copy via the legacy mirror.
    assert table_adapters.resolve_adapter(state.data_context, "temp").table == "t2"
    with pytest.raises(table_adapters.TempTableUnavailable):
        table_adapters.resolve_adapter(state.data_context, "copy:nope")


# ---------------------------------------------------------------- restore


def test_restore_keeps_working_copy_refs():
    meta = {
        "source": "postgres",
        "temp_schema": "stych_temp",
        "temp_table": "t1",
        "temp_table_generation": 4,
        "working_copies": [{"name": "working", "table": "t1", "parent": "source"}],
        "active_copy": "working",
    }
    out = _restore_sensitive_meta(meta)
    assert out["temp_table"] == "t1"
    assert out["temp_table_generation"] == 4
    assert out["working_copies"][0]["table"] == "t1"
    assert out["active_copy"] == "working"


# ------------------------------------------------------- pinned base snapshot


async def test_pinned_base_is_created_once_and_reused_by_later_copies():
    """The pin is taken from the source once; every later copy clones it, so a
    whole lineage shares one row-id stamp even without a natural key."""
    agent, fake = _agent()
    state = _state()
    await agent.ensure_temp_table(state)
    meta = state.data_context.metadata
    pin = wc.pinned_base(meta)
    assert pin and fake.created_from_source == [pin["table"]]

    await agent.create_working_copy(state, name="second", parent=wc.SOURCE_PARENT)
    # Still only one read of the customer's table.
    assert fake.created_from_source == [pin["table"]]


def test_pinned_base_is_read_only():
    meta = {}
    wc.register_copy(meta, name=wc.PINNED_BASE_NAME, table="t_base", role=wc.BASE_ROLE)
    wc.register_copy(meta, name="working", table="t_work")
    assert meta[wc.ACTIVE_KEY] == "working"
    with pytest.raises(wc.WorkingCopyError, match="read-only"):
        wc.set_active(meta, wc.PINNED_BASE_NAME)


def test_base_is_a_reserved_copy_name():
    with pytest.raises(wc.WorkingCopyError, match="reserved"):
        wc.validate_name(wc.PINNED_BASE_NAME)


def test_dropping_every_writable_copy_never_activates_the_pin():
    """The pin must not become a write target just because nothing else is left."""
    meta = {}
    wc.register_copy(meta, name=wc.PINNED_BASE_NAME, table="t_base", role=wc.BASE_ROLE)
    wc.register_copy(meta, name="working", table="t_work")
    wc.remove_copy(meta, "working")
    assert meta.get(wc.ACTIVE_KEY) is None
    assert wc.pinned_base(meta) is not None


async def test_agent_cannot_drop_the_pinned_base():
    agent, fake = _agent()
    state = _state()
    await agent.ensure_temp_table(state)
    meta = state.data_context.metadata
    pin = wc.pinned_base(meta)

    with pytest.raises(wc.WorkingCopyError, match="cannot be dropped"):
        await agent.drop_working_copy(state, wc.PINNED_BASE_NAME)

    assert pin["table"] not in fake.dropped
    assert wc.pinned_base(meta) == pin
