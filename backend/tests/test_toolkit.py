"""Unit tests for the unified Tool/Toolkit abstraction."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, Field

from react_engine.loop import PauseSignal
from react_engine.toolkit import Tool, Toolkit


class EchoArgs(BaseModel):
    text: str = Field(..., min_length=1)
    times: int = Field(1, ge=1, le=3)


def _echo(args):
    return args["text"] * args.get("times", 1)


async def _aecho(args):
    return f"async:{args['text']}"


def test_schema_from_pydantic_model():
    tk = Toolkit([Tool(name="echo", description="d", handler=_echo, input_model=EchoArgs)])
    schema = tk.schemas()[0]
    assert schema["function"]["name"] == "echo"
    params = schema["function"]["parameters"]
    assert params["properties"]["text"]["minLength"] == 1
    assert params["required"] == ["text"]


async def test_execute_validates_before_handler():
    called = []

    def handler(args):
        called.append(args)
        return "ran"

    tk = Toolkit([Tool(name="echo", description="d", handler=handler, input_model=EchoArgs)])
    out = await tk.execute("echo", {"times": 2})  # missing required `text`
    assert json.loads(out)["validation_errors"][0]["path"] == "text"
    assert called == []  # handler never ran
    assert await tk.execute("echo", {"text": "hi"}) == "ran"


async def test_execute_sync_and_async_handlers():
    tk = Toolkit(
        [
            Tool(name="s", description="d", handler=_echo, input_model=EchoArgs),
            Tool(name="a", description="d", handler=_aecho),
        ]
    )
    assert await tk.execute("s", {"text": "x", "times": 2}) == "xx"
    assert await tk.execute("a", {"text": "y"}) == "async:y"


async def test_unknown_tool_and_exception_wrapping():
    def boom(args):
        raise RuntimeError("kapow")

    tk = Toolkit([Tool(name="boom", description="d", handler=boom)])
    assert (await tk.execute("nope", {})).startswith("ERROR: Unknown tool")
    assert (await tk.execute("boom", {})).startswith("ERROR calling boom: kapow")


async def test_pause_signal_propagates():
    def pause(args):
        raise PauseSignal({"question": "ok?"})

    tk = Toolkit([Tool(name="ask", description="d", handler=pause, hitl=True)])
    with pytest.raises(PauseSignal):
        await tk.execute("ask", {})


def test_required_check_for_raw_schemas():
    tk = Toolkit(
        [
            Tool(
                name="raw",
                description="d",
                handler=_echo,
                parameters={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            )
        ]
    )
    err = tk._tools["raw"].validate_args({})
    assert "missing required argument" in err
    assert tk._tools["raw"].validate_args({"text": "x"}) is None


def test_from_schemas_fails_fast_on_drift():
    schemas = [{"type": "function", "function": {"name": "ghost", "parameters": {}}}]
    with pytest.raises(ValueError, match="no executor"):
        Toolkit.from_schemas(schemas, lambda n, a: "x", known_names={"real_tool"})


def test_flag_invariants():
    with pytest.raises(ValueError, match="cannot be parallel_safe"):
        Tool(name="t", description="d", handler=_echo, hitl=True, parallel_safe=True)
    with pytest.raises(ValueError, match="Duplicate"):
        Toolkit(
            [
                Tool(name="t", description="d", handler=_echo),
                Tool(name="t", description="d", handler=_echo),
            ]
        )


def test_flag_sets_exposed_to_engine():
    tk = Toolkit(
        [
            Tool(name="read", description="d", handler=_echo, parallel_safe=True),
            Tool(name="ask", description="d", handler=_echo, hitl=True),
            Tool(name="write", description="d", handler=_echo),
        ]
    )
    assert tk.parallel_safe_names() == frozenset({"read"})
    assert tk.hitl_names() == frozenset({"ask"})
