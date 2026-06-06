"""
Streamlit dashboard for the multi-agent debate, reading from Oracle 26ai through
the SQLcl MCP server (same path the agents use — no direct DB driver / wallet).

Run from the project root:
    streamlit run src/dashboard.py

Each refresh launches `sql -mcp` once (a few seconds for the JVM), loads every
persisted run, and caches it; selecting a run is then instant.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import sys

# MCP spawns a subprocess; on Windows that needs the Proactor loop.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

sys.path.insert(0, os.path.dirname(__file__))

import streamlit as st

import config
import dashboard_data as dd
from mcp_oracle import open_oracle_mcp

PERSONA_STYLE = {
    "ALPHA": ("🔥 Agent Alpha — Growth", "#e8f5e9", "#2e7d32"),
    "BETA": ("❄️ Agent Beta — Risk", "#fdecea", "#c62828"),
}


def _load_all_sync() -> list[dict]:
    """Open one MCP session, return every run with its full detail."""

    async def _go() -> list[dict]:
        async with open_oracle_mcp(config.SQLCL_COMMAND, config.ORACLE_MCP_CONNECTION) as mcp:
            runs = await dd.list_runs(mcp)
            out: list[dict] = []
            for r in runs:
                detail = await dd.get_run(mcp, r["run_id"])
                if detail:
                    out.append(detail)
            return out

    # Run in a fresh thread so we never collide with Streamlit's own event loop.
    with concurrent.futures.ThreadPoolExecutor(1) as ex:
        return ex.submit(lambda: asyncio.run(_go())).result()


@st.cache_data(show_spinner="Loading debates from Oracle 26ai via SQLcl MCP…")
def load_all(nonce: int) -> list[dict]:
    return _load_all_sync()


def argument_card(persona: str, phase: str, content: str) -> None:
    title, bg, fg = PERSONA_STYLE.get(persona, (persona, "#eee", "#333"))
    label = f"{title}  ·  {phase}"
    st.markdown(
        f"<div style='background:{bg};border-left:5px solid {fg};"
        f"padding:10px 14px;border-radius:6px;margin-bottom:6px;'>"
        f"<b style='color:{fg};'>{label}</b></div>",
        unsafe_allow_html=True,
    )
    st.markdown(content)


def main() -> None:
    st.set_page_config(page_title="AI Agents Argue · Oracle 26ai", page_icon="⚖️", layout="wide")
    st.title("⚖️ When AI Agents Argue")
    st.caption("Adversarial multi-agent credit debates over Oracle AI Database 26ai · data via the SQLcl MCP server")

    with st.sidebar:
        st.header("Controls")
        nonce = st.session_state.setdefault("nonce", 0)
        if st.button("🔄 Refresh from database"):
            st.session_state["nonce"] = nonce + 1
            st.cache_data.clear()
            st.rerun()

    runs = load_all(st.session_state["nonce"])
    if not runs:
        st.warning("No debates found. Run `python src/debate.py` first to populate the database.")
        return

    with st.sidebar:
        options = {
            f"#{r['run_id']} · {r.get('customer_name') or r['customer_id']} · {r.get('created_at','')}": r
            for r in runs
        }
        choice = st.selectbox("Debate run", list(options.keys()))
    run = options[choice]

    c1, c2, c3 = st.columns(3)
    c1.metric("Applicant", run.get("customer_name") or run["customer_id"])
    c2.metric("Run", run.get("created_at", "—"))
    c3.metric("Model", run.get("model", "—"))

    st.subheader("⚖️ Verdict")
    st.success(run.get("verdict") or "—")

    st.subheader("The debate")
    args = sorted(run.get("arguments") or [], key=lambda a: a.get("seq", 0))
    openings = [a for a in args if a.get("phase") == "opening"]
    rebuttals = [a for a in args if a.get("phase") != "opening"]

    cols = st.columns(2)
    for col, persona in zip(cols, ("ALPHA", "BETA")):
        with col:
            for a in openings:
                if a["persona"] == persona:
                    argument_card(a["persona"], a["phase"], a["content"])

    for a in rebuttals:
        argument_card(a["persona"], a["phase"], a["content"])


if __name__ == "__main__":
    main()
else:
    main()
