"""
The two "tools" the debate agents are allowed to call.

Both ultimately run through the SQLcl MCP `run-sql` tool — the agents never get a
direct DB handle. `vector_search` is a convenience wrapper: it embeds the query
text with OpenAI, then builds *real* Oracle vector SQL and sends it over MCP.
"""
from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

import config
from mcp_oracle import OracleMCP

# OpenAI function-calling schema exposed to every agent.
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": (
                "Run a read-only SQL query against the Oracle credit database via "
                "the SQLcl MCP server. Tables: customers(customer_id, name, industry, "
                "years_in_business, annual_revenue, requested_credit), "
                "transactions(customer_id, txn_date, amount, txn_type) where amount>0 "
                "is an inflow and amount<0 an outflow. Use standard Oracle SQL."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "A single SELECT statement."}
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vector_search",
            "description": (
                "Semantic similarity search over historical risk narratives. Embeds "
                "your query text and returns the nearest risk_profiles rows, each with "
                "a label (GOOD | WATCH | FRAUD), risk_score, cosine distance, and the "
                "narrative. Use it to find whether a customer resembles known fraud / "
                "default patterns or known good payers."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query_text": {
                        "type": "string",
                        "description": "Natural-language description to match against.",
                    },
                    "k": {"type": "integer", "description": "How many neighbours (1-10).", "default": 5},
                },
                "required": ["query_text"],
            },
        },
    },
]


def _vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


async def _embed(client: AsyncOpenAI, text: str) -> list[float]:
    resp = await client.embeddings.create(model=config.EMBED_MODEL, input=text)
    return resp.data[0].embedding


async def dispatch(
    name: str,
    args: dict[str, Any],
    *,
    mcp: OracleMCP,
    openai_client: AsyncOpenAI,
) -> str:
    """Route one tool call from an agent to the database via MCP."""
    if name == "run_sql":
        return await mcp.run_sql(args["sql"])

    if name == "vector_search":
        k = int(args.get("k", 5) or 5)
        k = max(1, min(k, 10))
        vec = await _embed(openai_client, args["query_text"])
        sql = (
            "SELECT customer_id, label, risk_score, "
            f"ROUND(VECTOR_DISTANCE(embedding, TO_VECTOR('{_vector_literal(vec)}'), COSINE), 4) "
            "AS distance, profile_text "
            "FROM risk_profiles "
            f"ORDER BY VECTOR_DISTANCE(embedding, TO_VECTOR('{_vector_literal(vec)}'), COSINE) "
            f"FETCH APPROX FIRST {k} ROWS ONLY"
        )
        return await mcp.run_sql(sql)

    return json.dumps({"error": f"unknown tool {name}"})
