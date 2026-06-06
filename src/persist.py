"""
Persist debate runs to Oracle (via MCP) so a dashboard can read them.

Long agent outputs exceed the 4000-byte SQL string-literal limit, so CLOBs are
built by concatenating <=1500-char TO_CLOB(...) chunks — a plain INSERT, no PL/SQL.
'&' is encoded as CHR(38) because SQLcl treats it as a substitution prefix.
"""
from __future__ import annotations

from mcp_oracle import OracleMCP

def _create_if_absent(ddl: str) -> str:
    """Wrap a CREATE so re-running is a no-op (swallow ORA-00955 name in use)."""
    body = ddl.replace("'", "''")
    return (
        "BEGIN EXECUTE IMMEDIATE '" + body + "'; "
        "EXCEPTION WHEN OTHERS THEN IF SQLCODE != -955 THEN RAISE; END IF; END;"
    )


DDL = [
    _create_if_absent(
        "CREATE TABLE debate_runs (run_id NUMBER PRIMARY KEY, customer_id NUMBER, "
        "created_at TIMESTAMP DEFAULT SYSTIMESTAMP, model VARCHAR2(60), verdict CLOB)"
    ),
    _create_if_absent(
        "CREATE TABLE debate_arguments (run_id NUMBER, seq NUMBER, phase VARCHAR2(30), "
        "persona VARCHAR2(20), content CLOB, created_at TIMESTAMP DEFAULT SYSTIMESTAMP)"
    ),
    "CREATE OR REPLACE VIEW v_debate_feed AS "
    "SELECT r.run_id, r.customer_id, c.name AS customer_name, r.created_at, r.model, "
    "a.seq, a.phase, a.persona, a.content AS argument, r.verdict "
    "FROM debate_runs r JOIN debate_arguments a ON a.run_id = r.run_id "
    "LEFT JOIN customers c ON c.customer_id = r.customer_id",
]


def _q(text: str) -> str:
    """Quote a literal, neutralising everything SQLcl mishandles in transit:
    quotes (double them), '&' (substitution prefix), and newlines (a blank line
    ends a statement in SQLcl, truncating the literal -> ORA-01756). Encode the
    last two as CHR() concatenation so the statement stays on one line.
    """
    t = (text or "").replace("'", "''")
    t = t.replace("&", "'||CHR(38)||'")
    t = t.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "'||CHR(10)||'")
    return "'" + t + "'"


def _clob_expr(text: str, size: int = 1500) -> str:
    raw = text or ""
    chunks = [raw[i : i + size] for i in range(0, len(raw), size)] or [""]
    return "||".join("TO_CLOB(" + _q(c) + ")" for c in chunks)


async def ensure_tables(mcp: OracleMCP) -> None:
    for stmt in DDL:
        await mcp.run_sql(stmt)


async def _exec(mcp: OracleMCP, sql: str, what: str) -> None:
    out = await mcp.run_sql(sql)
    if "ORA-" in out or "Error" in out or "cancelled" in out:
        ora = next((ln for ln in out.splitlines() if "ORA-" in ln), out[:300])
        raise RuntimeError(f"persist {what} FAILED: {ora}")


async def save_debate(
    mcp: OracleMCP,
    *,
    run_id: int,
    customer_id: int,
    model: str,
    arguments: list[tuple[int, str, str, str]],  # (seq, phase, persona, content)
    verdict: str,
) -> None:
    await _exec(
        mcp,
        "INSERT INTO debate_runs (run_id, customer_id, model, verdict) VALUES "
        f"({run_id}, {customer_id}, {_q(model)}, {_clob_expr(verdict)})",
        "insert run",
    )
    for seq, phase, persona, content in arguments:
        await _exec(
            mcp,
            "INSERT INTO debate_arguments (run_id, seq, phase, persona, content) VALUES "
            f"({run_id}, {seq}, {_q(phase)}, {_q(persona)}, {_clob_expr(content)})",
            f"insert arg seq={seq}",
        )
    await _exec(mcp, "COMMIT", "commit")
