"""
Orchestrate an adversarial multi-agent debate over an Oracle credit decision.

    Agent Alpha (Growth)  ─┐
    Agent Beta  (Risk)    ─┤──>  SQLcl MCP server  ──>  Oracle 23ai/26ai
    Judge (Arbitrator)    ─┘     (run-sql + vector search)

Each agent runs a real OpenAI function-calling loop: it decides which SQL / vector
queries to run, the orchestrator executes them through MCP, feeds results back, and
the agent keeps going until it produces a final argument. The Judge then reads both
evidence-backed cases and renders a verdict.

Run:  python src/debate.py
"""
from __future__ import annotations

import asyncio
import sys
import textwrap
import time
from typing import Any

# Windows consoles default to cp1252, which can't encode emoji — force UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

from openai import AsyncOpenAI

import config
import persist
from mcp_oracle import open_oracle_mcp, OracleMCP
from tools import TOOL_SPECS, dispatch

MAX_TOOL_TURNS = 8


def _wrap(text: str, indent: str = "   ") -> str:
    out = []
    for para in text.splitlines():
        out.append(textwrap.fill(para, width=92, initial_indent=indent,
                                 subsequent_indent=indent) if para.strip() else "")
    return "\n".join(out)


async def run_agent(
    *,
    client: AsyncOpenAI,
    mcp: OracleMCP,
    system_prompt: str,
    user_prompt: str,
    label: str,
) -> str:
    """A single agent: tool-calling loop until it returns a final text answer."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    for _ in range(MAX_TOOL_TURNS):
        resp = await client.chat.completions.create(
            model=config.OPENAI_MODEL,
            messages=messages,
            tools=TOOL_SPECS,
            temperature=0.4,
        )
        msg = resp.choices[0].message

        if not msg.tool_calls:
            return (msg.content or "").strip()

        # Record the assistant's tool-call turn, then execute each call via MCP.
        messages.append(
            {
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
            }
        )
        for tc in msg.tool_calls:
            import json

            args = json.loads(tc.function.arguments or "{}")
            preview = args.get("sql") or args.get("query_text") or ""
            print(f"      [{label} → {tc.function.name}] {preview[:90]}")
            try:
                result = await dispatch(
                    tc.function.name, args, mcp=mcp, openai_client=client
                )
            except Exception as exc:  # surface DB errors back to the model
                result = f"ERROR: {exc}"
            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": result[:6000]}
            )

    return "(agent exhausted its tool budget without concluding)"


# --- Personas ---------------------------------------------------------------
ALPHA_SYSTEM = """You are Agent Alpha, the Growth advocate on a credit committee.
Your job: argue FOR approving the requested credit. Build the strongest possible
data-backed case using the database tools — query cash-flow trends, revenue,
repayment-relevant signals, and find similar GOOD historical borrowers via
vector_search. Be persuasive but cite real figures you retrieved. Do NOT invent
numbers. End with a clear recommendation and the evidence behind it."""

BETA_SYSTEM = """You are Agent Beta, the Risk auditor on a credit committee.
Your job: argue AGAINST the credit (or for tight limits). Hunt for red flags:
volatile or negative cash flow, large round-number outflows, weak ratios, and —
critically — use vector_search to test whether this customer's profile resembles
known FRAUD or default patterns. Cite real figures and the nearest risky
neighbours (label + distance). Do NOT invent numbers. End with a clear
recommendation and the evidence behind it."""

JUDGE_SYSTEM = """You are the Judge on a credit committee. Two analysts have made
opposing, evidence-backed cases about one applicant. Weigh the SPECIFIC figures
and vector-similarity findings each cited. Penalise claims that lack data. Render
a binding verdict: APPROVE / APPROVE WITH CONDITIONS / DECLINE, an approved credit
amount, the 2-3 decisive data points, and one risk to monitor. Be concise."""


async def main() -> None:
    cid = config.CASE_CUSTOMER_ID
    client = AsyncOpenAI(api_key=config.OPENAI_API_KEY or None)

    print(f"\n=== Credit debate — Customer {cid} ===\n")

    async with open_oracle_mcp(config.SQLCL_COMMAND, config.ORACLE_MCP_CONNECTION) as mcp:
        print(f"Connected to Oracle via SQLcl MCP. Tools: {', '.join(mcp.tool_names)}\n")

        case = (
            f"The credit committee is reviewing customer_id = {cid}. "
            "Investigate using the tools, then make your case."
        )

        print("🔥 Agent Alpha (Growth) is building its case...")
        alpha = await run_agent(
            client=client, mcp=mcp, system_prompt=ALPHA_SYSTEM,
            user_prompt=case, label="ALPHA",
        )
        print("\n🔥 ALPHA:\n" + _wrap(alpha) + "\n")

        print("❄️  Agent Beta (Risk) is building its rebuttal...")
        beta = await run_agent(
            client=client, mcp=mcp, system_prompt=BETA_SYSTEM,
            user_prompt=case + "\n\nAgent Alpha will argue for approval; find the holes.",
            label="BETA",
        )
        print("\n❄️  BETA:\n" + _wrap(beta) + "\n")

        # One rebuttal round: Alpha responds to Beta's specific red flags.
        print("🔁 Agent Alpha rebuts Beta's red flags...")
        alpha_rebuttal = await run_agent(
            client=client, mcp=mcp, system_prompt=ALPHA_SYSTEM,
            user_prompt=(
                f"{case}\n\nThe Risk auditor raised these concerns:\n\n{beta}\n\n"
                "Rebut them with fresh data where you can; concede what is undeniable."
            ),
            label="ALPHA-2",
        )
        print("\n🔁 ALPHA (rebuttal):\n" + _wrap(alpha_rebuttal) + "\n")

        print("⚖️  The Judge is deliberating...")
        verdict = await client.chat.completions.create(
            model=config.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": JUDGE_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Applicant: customer_id {cid}.\n\n"
                        f"=== Alpha (Growth) opening ===\n{alpha}\n\n"
                        f"=== Beta (Risk) ===\n{beta}\n\n"
                        f"=== Alpha rebuttal ===\n{alpha_rebuttal}\n\n"
                        "Render your verdict."
                    ),
                },
            ],
            temperature=0.2,
        )
        verdict_text = verdict.choices[0].message.content or ""
        print("\n⚖️  VERDICT:\n" + _wrap(verdict_text) + "\n")

        # Persist the run so the dashboard (APEX / Streamlit) can show it.
        await persist.ensure_tables(mcp)
        await persist.save_debate(
            mcp,
            run_id=int(time.time()),
            customer_id=cid,
            model=config.OPENAI_MODEL,
            arguments=[
                (1, "opening", "ALPHA", alpha),
                (2, "opening", "BETA", beta),
                (3, "rebuttal", "ALPHA", alpha_rebuttal),
            ],
            verdict=verdict_text,
        )
        print("💾 Saved to debate_runs / debate_arguments (view: v_debate_feed).")


if __name__ == "__main__":
    asyncio.run(main())
