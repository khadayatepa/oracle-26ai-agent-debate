"""
Thin async bridge to Oracle's SQLcl MCP server (`sql -mcp`).

Why this exists
---------------
The agents do NOT get raw DB credentials. They talk to the database only through
the SQLcl MCP server, which exposes a fixed catalog of tools:

    list-connections | connect | disconnect | run-sql | run-sqlcl | schema-information

Oracle does not publish the exact JSON argument names for those tools, so instead
of hardcoding (and risking a wrong guess) we read each tool's `inputSchema` at
runtime via `list_tools()` and fill the right property names. That keeps this code
correct even if Oracle renames an argument between SQLcl releases.

Requires: SQLcl 25.2+ on PATH, and a saved connection created with
    conn -save <NAME> -savepwd
"""
from __future__ import annotations

import contextlib
import os
from typing import Any, AsyncIterator

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# SQLcl's tools log these fields and NPE if they arrive null, so always send them.
MCP_CLIENT_ID = "oraclevector-debate/0.1"
MCP_MODEL_ID = os.getenv("OPENAI_MODEL", "gpt-4o")


def _text_of(result: Any) -> str:
    """Flatten an MCP CallToolResult into plain text."""
    parts: list[str] = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(text)
    return "\n".join(parts).strip() or "(no output)"


class OracleMCP:
    """Async context manager around one live SQLcl MCP session."""

    def __init__(self, command: str, connection_name: str):
        self._command = command
        self._connection_name = connection_name
        self._session: ClientSession | None = None
        self._schemas: dict[str, dict] = {}
        self._stack = contextlib.AsyncExitStack()

    # -- lifecycle ----------------------------------------------------------
    async def __aenter__(self) -> "OracleMCP":
        # The MCP SDK launches the subprocess with a *minimal* default environment
        # that drops TNS_ADMIN — so `sql -mcp` can't resolve wallet/TNS aliases.
        # Pass the full parent environment so the saved connection works.
        params = StdioServerParameters(
            command=self._command, args=["-mcp"], env=dict(os.environ)
        )
        try:
            read, write = await self._stack.enter_async_context(stdio_client(params))
            self._session = await self._stack.enter_async_context(
                ClientSession(read, write)
            )
            await self._session.initialize()

            listed = await self._session.list_tools()
            for tool in listed.tools:
                self._schemas[tool.name] = tool.inputSchema or {}

            await self._connect()
        except BaseException:
            # Tear the subprocess down in the same task we created it in, so anyio
            # doesn't complain about exiting a cancel scope from another task.
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.aclose()

    # -- tool calling -------------------------------------------------------
    async def _call(self, tool: str, args: dict[str, Any]) -> str:
        """Call an MCP tool, injecting mcp_client/model when the tool accepts them."""
        assert self._session is not None, "OracleMCP used outside its context"
        props = self._schemas.get(tool, {}).get("properties", {})
        payload = dict(args)
        if "mcp_client" in props:
            payload.setdefault("mcp_client", MCP_CLIENT_ID)
        if "model" in props:
            payload.setdefault("model", MCP_MODEL_ID)
        result = await self._session.call_tool(tool, payload)
        return _text_of(result)

    # -- schema-aware argument resolution -----------------------------------
    def _arg_name(self, tool: str, *hints: str) -> str:
        props: dict = self._schemas.get(tool, {}).get("properties", {})
        if not props:
            # No schema advertised; fall back to the most common name.
            return hints[0]
        for hint in hints:
            for prop in props:
                if hint in prop.lower():
                    return prop
        # Last resort: first declared property.
        return next(iter(props))

    # -- operations ---------------------------------------------------------
    async def _connect(self) -> None:
        name_arg = self._arg_name("connect", "conn", "name")
        try:
            await self._call("connect", {name_arg: self._connection_name})
        except Exception:
            # Known SQLcl MCP quirk: `connect` can throw while formatting its
            # response even though the DB connection was actually established.
            # Don't trust the exception — verify with a probe query below.
            pass
        probe = await self.run_sql("SELECT user FROM dual")
        if any(m in probe for m in ("ORA-", "not established")) or "ERROR" in probe.upper():
            raise RuntimeError(
                f"SQLcl MCP failed to connect to '{self._connection_name}':\n{probe}"
            )

    async def run_sql(self, sql: str) -> str:
        """Execute a SQL / PL-SQL statement through the MCP `run-sql` tool."""
        sql_arg = self._arg_name("run-sql", "sql", "query", "statement")
        return await self._call("run-sql", {sql_arg: sql})

    async def run_sqlcl(self, command: str) -> str:
        """Execute a SQLcl command (e.g. SET DEFINE OFF) via `run-sqlcl`."""
        arg = self._arg_name("run-sqlcl", "sqlcl", "command")
        return await self._call("run-sqlcl", {arg: command})

    @property
    def tool_names(self) -> list[str]:
        return list(self._schemas)


@contextlib.asynccontextmanager
async def open_oracle_mcp(command: str, connection_name: str) -> AsyncIterator[OracleMCP]:
    async with OracleMCP(command, connection_name) as mcp:
        yield mcp
