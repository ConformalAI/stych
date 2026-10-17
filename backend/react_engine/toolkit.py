"""
Unified tool abstraction for the ReAct engine.

A ``Tool`` owns everything about one callable in one place: the model-facing
name/description/JSON schema, the handler that executes it, and execution
flags (``parallel_safe``, ``hitl``). A ``Toolkit`` binds a set of Tools and
gives the engine the two things it needs — the provider schema list and a
``(name, args, call_id) -> observation`` executor — plus the flag sets used
for parallel dispatch.

This replaces the previous split where the JSON schema lived in one module and
the dispatch (``if name == ...`` / ``getattr``) in another, with nothing
failing at construction time when they drifted.

Schemas come from either:

* ``input_model`` — a Pydantic model; the JSON schema is *generated* from it
  and arguments are validated against it before the handler runs (invalid
  calls never reach the handler; the model gets a structured
  ``validation_errors`` observation it can act on), or
* ``parameters`` — a hand-written JSON schema, for tools whose inputs are not
  worth a model. Only ``required`` fields are pre-checked in this mode.

``Toolkit.from_schemas`` wraps an existing ``(name, args) -> str`` executor and
schema list into a Toolkit, verifying the schema names against the executor's
known-name set at construction — the incremental migration path for agents
that keep their legacy registries.
"""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Type

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

# Handlers take the (validated) argument dict and return the observation.
ToolHandler = Callable[[Dict[str, Any]], Any]

_MAX_ERROR_CHARS = 2_000


def _flatten_validation_error(exc: ValidationError) -> List[Dict[str, str]]:
    """Pydantic error → the ``{path, code, message}`` list agents already use."""
    return [
        {
            "path": ".".join(str(part) for part in err["loc"]),
            "code": err["type"],
            "message": err["msg"],
        }
        for err in exc.errors()
    ]


@dataclass(frozen=True)
class Tool:
    """One agent tool: schema, handler, and execution flags, co-located."""

    name: str
    description: str
    handler: ToolHandler
    # Exactly one of input_model / parameters should be provided; a tool with
    # neither takes no arguments.
    input_model: Optional[Type[BaseModel]] = None
    parameters: Optional[Dict[str, Any]] = None
    # True when the tool has no side effects that can race a sibling call —
    # the engine may run it concurrently with other parallel_safe calls.
    parallel_safe: bool = False
    # True when the tool may raise PauseSignal (human-in-the-loop); never
    # dispatched concurrently.
    hitl: bool = False

    def __post_init__(self) -> None:
        if self.input_model is not None and self.parameters is not None:
            raise ValueError(f"Tool {self.name!r}: give input_model or parameters, not both")
        if self.hitl and self.parallel_safe:
            raise ValueError(f"Tool {self.name!r}: an hitl tool cannot be parallel_safe")

    def json_schema(self) -> Dict[str, Any]:
        """This tool as an OpenAI-format function schema entry."""
        if self.input_model is not None:
            params = self.input_model.model_json_schema()
        elif self.parameters is not None:
            params = self.parameters
        else:
            params = {"type": "object", "properties": {}}
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": params,
            },
        }

    def validate_args(self, args: Any) -> Optional[str]:
        """Return an error observation if *args* are invalid, else None."""
        if not isinstance(args, dict):
            args = {}
        if self.input_model is not None:
            try:
                self.input_model.model_validate(args)
            except ValidationError as exc:
                return json.dumps(
                    {"validation_errors": _flatten_validation_error(exc)}, default=str
                )
            return None
        required = set((self.parameters or {}).get("required", []) or [])
        missing = sorted(required - set(args.keys()))
        if missing:
            return f"ERROR: `{self.name}` is missing required argument(s): {missing}."
        return None


class Toolkit:
    """A named, validated set of Tools bound to one agent run."""

    def __init__(self, tools: Sequence[Tool]):
        names = [t.name for t in tools]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"Duplicate tool names: {sorted(dupes)}")
        self._tools: Dict[str, Tool] = {t.name: t for t in tools}

    @classmethod
    def from_schemas(
        cls,
        schemas: Iterable[Dict[str, Any]],
        executor: Callable[[str, Any], Any],
        *,
        known_names: Optional[Iterable[str]] = None,
        parallel_safe: Iterable[str] = (),
        hitl: Iterable[str] = (),
    ) -> "Toolkit":
        """Wrap a legacy (schema list + shared executor) pair into a Toolkit.

        When *known_names* is given (e.g. a registry's TOOL_NAMES), every schema
        name must be executable — construction fails on drift instead of the
        model discovering a phantom tool at runtime.
        """
        parallel_set = set(parallel_safe)
        hitl_set = set(hitl)
        tools: List[Tool] = []
        for schema in schemas:
            fn = schema.get("function", schema)
            name = str(fn.get("name", ""))
            if known_names is not None and name not in set(known_names):
                raise ValueError(
                    f"Tool schema {name!r} has no executor (known: {sorted(known_names)})"
                )

            def _handler(args: Dict[str, Any], _name: str = name) -> Any:
                return executor(_name, args)

            tools.append(
                Tool(
                    name=name,
                    description=str(fn.get("description", "")),
                    handler=_handler,
                    parameters=fn.get("parameters") or {"type": "object", "properties": {}},
                    parallel_safe=name in parallel_set,
                    hitl=name in hitl_set,
                )
            )
        return cls(tools)

    # ------------------------------------------------------------- engine API

    def schemas(self) -> List[Dict[str, Any]]:
        return [t.json_schema() for t in self._tools.values()]

    def parallel_safe_names(self) -> frozenset:
        return frozenset(n for n, t in self._tools.items() if t.parallel_safe)

    def hitl_names(self) -> frozenset:
        return frozenset(n for n, t in self._tools.items() if t.hitl)

    async def execute(self, name: str, args: Any, _call_id: str = "") -> str:
        """Validate and run one tool call; always returns an observation string.

        PauseSignal is intentionally NOT caught — it is the engine's HITL
        control flow, not an error.
        """
        from react_engine.loop import PauseSignal  # noqa: PLC0415 — avoid cycle

        tool = self._tools.get(name)
        if tool is None:
            return f"ERROR: Unknown tool {name!r}. Available: {sorted(self._tools)}"
        args = args if isinstance(args, dict) else {}
        invalid = tool.validate_args(args)
        if invalid is not None:
            return invalid
        try:
            result = tool.handler(args)
            if inspect.isawaitable(result):
                result = await result
            return str(result)
        except PauseSignal:
            raise
        except Exception as exc:  # noqa: BLE001 — observations must never crash the loop
            logger.exception("tool %s failed", name)
            return f"ERROR calling {name}: {str(exc)[:_MAX_ERROR_CHARS]}"
