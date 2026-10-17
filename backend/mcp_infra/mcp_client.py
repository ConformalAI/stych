"""
MCPClientManager — connect, list tools, and call tools on MCP servers.

Create one instance per IntegrationAgent.execute() call.

Transport behaviour
-------------------
* http (Streamable HTTP, modern spec)
    Uses anyio task groups internally so it CANNOT be held open across calls
    with manual __aenter__/__aexit__. Each list_tools / call_tool opens its own
    fresh `async with` block. This is correct: Streamable HTTP is HTTP POST-based
    and stateless between operations.

* http_sse (legacy SSE)
    Context manager is entered once and cached for the agent turn.

* stdio
    Subprocess is spawned once and cached for the agent turn.

Always call close_all() in a finally block (it is a no-op for http connections
since they are closed after each call).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from typing import Any, Dict, List

from mcp_infra.registry import MCPServerDef
from mcp_infra.schema_patches import apply_tool_schema_patches

logger = logging.getLogger(__name__)

# Max characters returned from a single tool call to avoid flooding the context.
MAX_RESULT_CHARS = 8_000


def _google_oauth_client_id(credentials: Dict[str, str]) -> str:
    return credentials.get("client_id") or os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")


def _google_oauth_client_secret() -> str:
    """App-level secret — never read from per-user credential blobs."""
    return os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")


class MCPClientManager:
    """
    Per-turn MCP connection manager.

    Usage::

        mgr = MCPClientManager()
        try:
            tools = await mgr.list_tools(server_def, credentials)
            result = await mgr.call_tool(server_def, "tool_name", {...}, credentials)
        finally:
            await mgr.close_all()
    """

    def __init__(self) -> None:
        # server_id → dedicated session actor for long-lived transports (sse, stdio).
        # Each actor owns its anyio cancel scopes on its OWN task (see _SessionActor),
        # so a messy subprocess teardown can never cancel the caller's task / LLM calls.
        self._actors: Dict[str, "_SessionActor"] = {}
        # server_id → lock serializing actor creation. Without this, two
        # parallel_safe tool calls hitting the same server_id (e.g. two
        # concurrent list_mcp_tools) can both see "no live actor" and each
        # spawn their own subprocess; the second write to self._actors[sid]
        # then orphans the first, which close_all() never tears down.
        self._actor_locks: Dict[str, asyncio.Lock] = {}
        # server_id → tool schemas, cached for the lifetime of this manager (one
        # agent turn) so repeated list_tools calls for the same server don't
        # re-open a connection and re-fetch — a latency trim for the ReAct loop.
        self._tools_cache: Dict[str, List[Dict[str, Any]]] = {}
        # temp files/dirs to clean up on close_all()
        self._tmp_files: List[str] = []
        self._tmp_dirs: List[str] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def list_tools(
        self, server_def: MCPServerDef, credentials: Dict[str, str]
    ) -> List[Dict[str, Any]]:
        """Return a list of tool schemas from the MCP server (cached per turn)."""
        sid = server_def.server_id
        if sid in self._tools_cache:
            logger.debug(
                "[MCP] %s list_tools → cache hit (%d tools)", sid, len(self._tools_cache[sid])
            )
            return self._tools_cache[sid]

        if server_def.transport == "http":
            tools = await self._list_tools_http(server_def, credentials)
        else:
            actor = await self._get_actor(server_def, credentials)
            tools = _parse_tools(await actor.request("list_tools", None))
            logger.debug(
                "[MCP] %s list_tools → %d tools: %s",
                sid,
                len(tools),
                [t["name"] for t in tools],
            )
        tools = apply_tool_schema_patches(sid, tools)
        self._tools_cache[sid] = tools
        return tools

    async def call_tool(
        self,
        server_def: MCPServerDef,
        tool_name: str,
        arguments: Dict[str, Any],
        credentials: Dict[str, str],
    ) -> str:
        """Call a tool and return the result as a string (truncated).

        When the MCP server signals isError=True the result is prefixed with
        "[MCP Error]" so the LLM / agent can unambiguously detect a failure
        without an exception being raised.  Use call_tool_strict when a hard
        exception is required instead.
        """
        if server_def.transport == "http":
            return await self._call_tool_http(server_def, tool_name, arguments, credentials)

        logger.debug(
            "[MCP] %s → %s args=%s",
            server_def.server_id,
            tool_name,
            json.dumps(arguments, default=str)[:500],
        )
        actor = await self._get_actor(server_def, credentials)
        result_obj = await actor.request("call_tool", (tool_name, arguments))
        result = _format_result(result_obj)
        is_error = getattr(result_obj, "isError", False)
        if is_error:
            result = (
                f"[MCP Error] {result}"
                if result
                else "[MCP Error] Tool returned an error with no details."
            )
        logger.debug(
            "[MCP] %s ← %s isError=%s result=%s",
            server_def.server_id,
            tool_name,
            is_error,
            result[:300],
        )
        return result

    async def call_tool_strict(
        self,
        server_def: MCPServerDef,
        tool_name: str,
        arguments: Dict[str, Any],
        credentials: Dict[str, str],
        full: bool = False,
    ) -> str:
        """Call a tool and return the result as a string.

        Unlike call_tool, raises RuntimeError if the MCP server signals
        isError=True.  Use this when a silent failure is indistinguishable from
        success (e.g. write operations where no exception is otherwise raised).

        Set ``full=True`` to skip the MAX_RESULT_CHARS truncation. The cap exists
        to protect the LLM context window; server-side bulk reads (e.g. pulling a
        whole sheet to a workspace file) never reach the LLM and MUST get the
        untruncated payload — otherwise large JSON responses are clipped mid-token
        and fail to parse.
        """
        if server_def.transport == "http":
            result_obj = await self._call_tool_http_result(
                server_def, tool_name, arguments, credentials
            )
        else:
            logger.debug(
                "[MCP] %s → %s args=%s",
                server_def.server_id,
                tool_name,
                json.dumps(arguments, default=str)[:500],
            )
            actor = await self._get_actor(server_def, credentials)
            result_obj = await actor.request("call_tool", (tool_name, arguments))

        result = _format_result(result_obj, max_chars=0 if full else MAX_RESULT_CHARS)
        is_error = getattr(result_obj, "isError", False)
        logger.debug(
            "[MCP] %s ← %s isError=%s result=%s",
            server_def.server_id,
            tool_name,
            is_error,
            result[:300],
        )
        if is_error:
            raise RuntimeError(result)
        return result

    async def close_all(self) -> None:
        """Tear down all cached long-lived connections (sse / stdio).

        Each actor tears its transport down inside its OWN task, so any anyio
        teardown cancellation is confined there and never touches this caller.
        """
        for _server_id, actor in list(self._actors.items()):
            await actor.close()
        self._actors.clear()
        self._tools_cache.clear()
        for tmp_path in self._tmp_files:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        self._tmp_files.clear()
        for tmp_dir in self._tmp_dirs:
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass
        self._tmp_dirs.clear()

    # ------------------------------------------------------------------
    # Streamable HTTP — fresh connection per call (anyio-safe)
    # ------------------------------------------------------------------

    async def _list_tools_http(
        self, server_def: MCPServerDef, credentials: Dict[str, str]
    ) -> List[Dict[str, Any]]:
        import httpx
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        url = server_def.http_url or ""
        headers = _build_http_headers(credentials)
        logger.info("[MCP] connecting to %s via http (list_tools)", server_def.server_id)

        async with httpx.AsyncClient(headers=headers, timeout=30) as http_client:
            async with streamable_http_client(url, http_client=http_client) as (
                read_stream,
                write_stream,
                _get_session_id,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools = _parse_tools(await session.list_tools())
                    logger.debug(
                        "[MCP] %s list_tools → %d tools: %s",
                        server_def.server_id,
                        len(tools),
                        [t["name"] for t in tools],
                    )
                    return tools

    async def _call_tool_http(
        self,
        server_def: MCPServerDef,
        tool_name: str,
        arguments: Dict[str, Any],
        credentials: Dict[str, str],
    ) -> str:
        result_obj = await self._call_tool_http_result(
            server_def, tool_name, arguments, credentials
        )
        result = _format_result(result_obj)
        is_error = getattr(result_obj, "isError", False)
        if is_error:
            result = (
                f"[MCP Error] {result}"
                if result
                else "[MCP Error] Tool returned an error with no details."
            )
        logger.debug(
            "[MCP] %s ← %s isError=%s result=%s",
            server_def.server_id,
            tool_name,
            is_error,
            result[:300],
        )
        return result

    async def _call_tool_http_result(
        self,
        server_def: MCPServerDef,
        tool_name: str,
        arguments: Dict[str, Any],
        credentials: Dict[str, str],
    ) -> Any:
        """Like _call_tool_http but returns the raw CallToolResult object."""
        import httpx
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        url = server_def.http_url or ""
        headers = _build_http_headers(credentials)
        logger.debug(
            "[MCP] %s → %s args=%s",
            server_def.server_id,
            tool_name,
            json.dumps(arguments, default=str)[:500],
        )

        async with httpx.AsyncClient(headers=headers, timeout=60) as http_client:
            async with streamable_http_client(url, http_client=http_client) as (
                read_stream,
                write_stream,
                _get_session_id,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    return await session.call_tool(tool_name, arguments)

    # ------------------------------------------------------------------
    # Cached long-lived transports (SSE / stdio)
    # ------------------------------------------------------------------

    async def _get_actor(
        self, server_def: MCPServerDef, credentials: Dict[str, str]
    ) -> "_SessionActor":
        """Return a running session actor for a cached transport, creating it once.

        The client context manager is BUILT here (synchronous env/credential setup)
        but ENTERED inside the actor's own task, so all of the transport's anyio
        cancel scopes are bound to that task and can never leak into the caller.

        Serialized per server_id: parallel_safe tool calls (e.g. two concurrent
        list_mcp_tools for the same server) can race here, so creation is guarded
        by a per-sid lock — the double-checked read avoids taking the lock on the
        common (already-connected) path.
        """
        sid = server_def.server_id
        actor = self._actors.get(sid)
        if actor is not None and actor.alive:
            return actor

        lock = self._actor_locks.setdefault(sid, asyncio.Lock())
        async with lock:
            actor = self._actors.get(sid)
            if actor is not None and actor.alive:
                return actor

            if server_def.transport == "http_sse":
                client_cm = self._build_http_sse_client_cm(server_def, credentials)
            elif server_def.transport == "stdio":
                client_cm = self._build_stdio_client_cm(server_def, credentials)
            else:
                raise ValueError(f"Unsupported cached transport: {server_def.transport!r}")

            actor = _SessionActor(sid)
            await actor.start(client_cm)  # propagates connect/initialize errors
            self._actors[sid] = actor
            return actor

    def _build_http_sse_client_cm(
        self, server_def: MCPServerDef, credentials: Dict[str, str]
    ) -> Any:
        """Build (do not enter) the legacy SSE client context manager."""
        from mcp.client.sse import sse_client

        url = server_def.http_url or ""
        headers = _build_http_headers(credentials)
        logger.info("[MCP] connecting to %s via http_sse", server_def.server_id)
        return sse_client(url, headers=headers)

    def _build_stdio_client_cm(self, server_def: MCPServerDef, credentials: Dict[str, str]) -> Any:
        """Build (do not enter) the stdio client context manager + env/creds setup."""
        from mcp.client.stdio import StdioServerParameters, stdio_client

        cmd = server_def.stdio_cmd or []
        if not cmd:
            raise ValueError(f"stdio_cmd is empty for server {server_def.server_id!r}")
        logger.info("[MCP] connecting to %s via stdio cmd=%s", server_def.server_id, cmd[0])

        env = dict(os.environ)

        auth_style = getattr(server_def, "auth_style", None)

        if auth_style == "gsheets_home":
            # mcp-google-sheets-full: reads token files from ~/.mcp-google-sheets/.
            # Override HOME to a per-request temp dir for per-user isolation.
            auth_mode = credentials.get("auth_mode", "service_account")

            if auth_mode == "oauth":
                tmp_home = tempfile.mkdtemp(prefix="stych_gsheets_home_")
                config_dir = os.path.join(tmp_home, ".mcp-google-sheets")
                os.makedirs(config_dir, exist_ok=True)

                gdrive_creds = {
                    "installed": {
                        "client_id": _google_oauth_client_id(credentials),
                        "client_secret": _google_oauth_client_secret(),
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
                    }
                }
                with open(os.path.join(config_dir, "gdrive-credentials.json"), "w") as f:
                    json.dump(gdrive_creds, f)

                gdrive_token = {
                    "access_token": credentials.get("access_token", ""),
                    "refresh_token": credentials.get("refresh_token", ""),
                    "token_type": "Bearer",
                    "expiry_date": credentials.get("expiry_date", 0),
                    "scope": credentials.get(
                        "scope",
                        "https://www.googleapis.com/auth/spreadsheets "
                        "https://www.googleapis.com/auth/drive openid email",
                    ),
                }
                with open(os.path.join(config_dir, "gdrive-token.json"), "w") as f:
                    json.dump(gdrive_token, f)

                env["HOME"] = tmp_home
                self._tmp_dirs.append(tmp_home)

            else:
                # Service account mode: GOOGLE_APPLICATION_CREDENTIALS → file path
                svc = credentials.get("service_account_json", "")
                if svc:
                    tmp = tempfile.NamedTemporaryFile(
                        mode="w", suffix=".json", delete=False, prefix="stych_gsa_"
                    )
                    tmp.write(svc)
                    tmp.flush()
                    tmp.close()
                    env["GOOGLE_APPLICATION_CREDENTIALS"] = tmp.name
                    self._tmp_files.append(tmp.name)

        elif auth_style == "notion_token":
            # @notionhq/notion-mcp-server: reads auth from OPENAPI_MCP_HEADERS env var.
            # The value must be a JSON object with Authorization and Notion-Version headers.
            api_key = credentials.get("api_key", "")
            if api_key:
                env["OPENAPI_MCP_HEADERS"] = json.dumps(
                    {
                        "Authorization": f"Bearer {api_key}",
                        "Notion-Version": "2022-06-28",
                    }
                )

        elif auth_style == "workspace_token":
            # @dguido/google-workspace-mcp: reads a standard googleapis token JSON
            # from the path set in GOOGLE_WORKSPACE_MCP_TOKEN_PATH.
            token_data = {
                "access_token": credentials.get("access_token", ""),
                "refresh_token": credentials.get("refresh_token", ""),
                "token_type": "Bearer",
                "expiry_date": credentials.get("expiry_date", 0),
                "scope": credentials.get("scope", ""),
            }
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, prefix="stych_gworkspace_token_"
            )
            json.dump(token_data, tmp)
            tmp.flush()
            tmp.close()
            env["GOOGLE_CLIENT_ID"] = _google_oauth_client_id(credentials)
            env["GOOGLE_CLIENT_SECRET"] = _google_oauth_client_secret()
            env["GOOGLE_WORKSPACE_MCP_TOKEN_PATH"] = tmp.name
            self._tmp_files.append(tmp.name)
            # Limit exposed tools to only the services this server_id is configured for.
            # Without this, the package exposes all 7 services (docs, drive, sheets,
            # slides, calendar, gmail, contacts), causing agent confusion and scope failures.
            workspace_services = getattr(server_def, "workspace_services", None)
            if workspace_services:
                env["GOOGLE_WORKSPACE_SERVICES"] = workspace_services

        params = StdioServerParameters(command=cmd[0], args=cmd[1:], env=env)
        return stdio_client(params)


# ------------------------------------------------------------------
# Session actor — isolates a cached MCP transport on its own task
# ------------------------------------------------------------------


class _SessionActor:
    """Owns one MCP session (connect → calls → teardown) on a dedicated task.

    Why this exists
    ---------------
    anyio cancel scopes are task-affine: the stdio/sse transport and ``ClientSession``
    push cancel scopes onto the *running task's* scope stack, and anyio re-delivers
    ``CancelledError`` at every checkpoint until they are exited. The caller's task
    also runs the LLM/httpx I/O. If those scopes lived on the caller's task, a messy
    subprocess teardown (lingering process → cancelled ``_process.wait()``) would
    leave an orphaned, still-cancelled scope on that task and cancel the *next* LLM
    call. Confining the whole lifecycle to this actor's own task means a bad teardown
    can only cancel this (dying) task — the caller stays pristine.

    The caller interacts only through an ``asyncio.Queue`` + per-request ``Future``,
    which are safe to use across tasks.
    """

    def __init__(self, server_id: str) -> None:
        self._server_id = server_id
        self._requests: asyncio.Queue = asyncio.Queue()
        self._task: Any = None

    @property
    def alive(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, client_cm: Any) -> None:
        ready: asyncio.Future = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._serve(client_cm, ready))
        await ready  # raises here if connect/initialize failed

    async def _serve(self, client_cm: Any, ready: "asyncio.Future") -> None:
        from mcp.client.session import ClientSession

        try:
            async with client_cm as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    logger.debug("[MCP] %s actor session initialized", self._server_id)
                    if not ready.done():
                        ready.set_result(None)
                    await self._serve_requests(session)
        except BaseException as exc:  # noqa: BLE001
            # Either a connect failure (surface via ready) or a serve/teardown error.
            # Never re-raise: this task is ending, so its (possibly cancel-poisoned)
            # scope stack dies with it and the caller's task is untouched.
            if not ready.done():
                ready.set_exception(exc)
            else:
                logger.debug("[MCP] %s actor exited: %r", self._server_id, exc)

    async def _serve_requests(self, session: Any) -> None:
        while True:
            item = await self._requests.get()
            if item is None:
                return  # shutdown sentinel → fall out so the async-with teardown runs here
            kind, payload, fut = item
            try:
                if kind == "list_tools":
                    res: Any = await session.list_tools()
                else:
                    tool_name, arguments = payload
                    res = await session.call_tool(tool_name, arguments)
            except asyncio.CancelledError:
                if not fut.done():
                    fut.set_exception(asyncio.CancelledError())
                raise  # a genuine cancellation of the actor task — let it tear down
            except BaseException as exc:  # noqa: BLE001
                if not fut.done():
                    fut.set_exception(exc)
                continue
            if not fut.done():
                fut.set_result(res)

    async def request(self, kind: str, payload: Any) -> Any:
        if not self.alive:
            raise RuntimeError(f"MCP session actor for {self._server_id!r} is not running")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._requests.put_nowait((kind, payload, fut))
        return await fut

    async def close(self) -> None:
        if self._task is None:
            return
        if not self._task.done():
            self._requests.put_nowait(None)
        # asyncio.wait never raises on the task's own exception; teardown errors stay
        # contained in the actor task. (A genuine cancellation of THIS caller while
        # waiting still propagates, which is correct.)
        await asyncio.wait({self._task})


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _build_http_headers(credentials: Dict[str, str]) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    api_key = credentials.get("api_key", "")
    if api_key:
        # TryOpenData uses X-API-Key header (not Authorization: Bearer)
        headers["X-API-Key"] = api_key
    return headers


def _parse_tools(result: Any) -> List[Dict[str, Any]]:
    tools = []
    for t in result.tools:
        # Read via the wire-format alias (model_dump(by_alias=True)) instead of
        # direct attribute access. The mcp SDK has renamed Tool's Python field
        # names across versions (e.g. 2.0.0 renamed inputSchema -> input_schema,
        # keeping "inputSchema" only as the serialization alias) while the wire
        # shape — and therefore the alias — stays stable. Direct `t.inputSchema`
        # access breaks the moment the underlying field is renamed.
        data = (
            t.model_dump(by_alias=True, exclude_none=True) if hasattr(t, "model_dump") else dict(t)
        )
        schema = data.get("inputSchema")
        if not isinstance(schema, dict):
            schema = schema.model_dump() if hasattr(schema, "model_dump") else {}
        tools.append(
            {
                "name": data.get("name", ""),
                "description": data.get("description") or "",
                "inputSchema": schema,
            }
        )
    return tools


def _format_result(result: Any, max_chars: int = MAX_RESULT_CHARS) -> str:
    parts: List[str] = []
    for block in getattr(result, "content", None) or []:
        if hasattr(block, "text"):
            parts.append(block.text)
        elif hasattr(block, "data"):
            parts.append(f"[binary data: {len(block.data)} bytes]")
        else:
            parts.append(str(block))

    output = "\n".join(parts)
    if not output.strip():
        output = "(Tool returned an empty response)"
    if max_chars and len(output) > max_chars:
        output = output[:max_chars] + f"\n… [truncated at {max_chars} chars]"
    return output
