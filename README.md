# Multi-Agent Debate over Oracle 23ai / 26ai (via SQLcl MCP + OpenAI)

Three OpenAI agents argue a credit decision over the **same** Oracle data, reaching
the database **only** through Oracle's SQLcl MCP server:

```
 Agent Alpha (Growth)  ─┐
 Agent Beta  (Risk)    ─┤── run-sql / vector search ──> SQLcl MCP ──> Oracle 23ai/26ai
 Judge (Arbitrator)    ─┘
```

Alpha argues *for* the loan, Beta hunts red flags (including **vector similarity to
known fraud profiles**), Alpha rebuts, and the Judge renders a binding verdict.


## Prerequisites

1. **Oracle Database 23ai or 26ai** (Autonomous DB, or [23ai Free](https://www.oracle.com/database/free/)) with vectors enabled.
2. **SQLcl 25.2+** on your `PATH`, with a **saved connection** for the demo user:
   ```
   sql /nolog
   SQL> conn -save DEBATE -savepwd debate@<your-tns-alias>
   ```
   (Saved connections live in `~/.dbtools`; `list-connections` reads them.)
3. **Python 3.10+** and an **OpenAI API key**.

## Setup

```powershell
pip install -r requirements.txt
copy .env.example .env      # then edit .env (OPENAI_API_KEY + ORACLE_MCP_CONNECTION)
python src/seed.py          # creates tables + real embeddings (+ HNSW index if available)
python src/debate.py        # run the debate (persists each run to the DB)
streamlit run src/dashboard.py   # view debates in a local dashboard
```

**Everything goes through the SQLcl MCP server** — both seeding and the debate. The
agents (and the loader) never hold raw DB credentials; if `sql -mcp` can reach your
saved connection, both scripts work. No `python-oracledb` or wallet config needed.

## Files

| File | Purpose |
| --- | --- |
| `sql/schema.sql` | Reference DDL for `customers`, `transactions`, `risk_profiles` (+ HNSW index). |
| `src/seed.py` | Creates schema and loads demo data + OpenAI embeddings, all via MCP. |
| `src/mcp_oracle.py` | Async bridge to `sql -mcp`; discovers tool arg names at runtime. |
| `src/tools.py` | The `run_sql` + `vector_search` tools exposed to the agents. |
| `src/debate.py` | Orchestrates Alpha / Beta / rebuttal / Judge, then persists the run. |
| `src/persist.py` | Saves each debate to `debate_runs` / `debate_arguments` via MCP. |
| `sql/dashboard.sql` | Persistence tables + `v_debate_feed` view for a dashboard. |
| `src/dashboard_data.py` | Reads debates back out of Oracle as JSON (via MCP). |
| `src/dashboard.py` | Streamlit dashboard over the persisted debates. |
| `blog/BLOG.md` | The rewritten, technically-accurate blog post (Markdown). |
| `blog/blog.html` | WordPress-ready HTML (paste into a Custom HTML block). |

## Real-world gotchas (verified running against Autonomous AI DB 26ai)

These are the issues actually encountered bringing this up against a live 26ai
instance via SQLcl 25.2 — and how the code handles each:

1. **`TNS_ADMIN` is dropped from the MCP subprocess.** The MCP Python SDK launches
   `sql -mcp` with a *minimal* environment, so a wallet/TNS alias like
   `prashant26ai_medium` fails with `ORA-17868: Unknown host`. Fix: `mcp_oracle.py`
   passes the full `os.environ` to the subprocess.
2. **`connect` throws but actually connects.** SQLcl's `connect` tool can raise
   `Cannot invoke "String.length()" because "str" is null` while formatting its
   response, *after* the DB session is established. Fix: we ignore the exception and
   verify with a `SELECT user FROM dual` probe.
3. **`&` in data triggers SQLcl substitution.** `"Freight & Logistics"` raises
   "Substitution cancelled", and `SET DEFINE OFF` does **not** persist across MCP
   calls. Fix: `seed.py` encodes `&` as `CHR(38)` concatenation in literals.
4. **`INSERT ALL` + `IDENTITY` collide.** A `GENERATED ALWAYS AS IDENTITY` column's
   sequence is evaluated once per `INSERT ALL` statement → `ORA-00001`. Fix: the
   transactions table has no identity column.
5. **Windows console encoding.** `cp1252` can't print emoji; `debate.py` reconfigures
   stdout/stderr to UTF-8.

## The demo scenario

Customer **994221** (Helios Freight Logistics) shows genuine revenue growth *and*
several large round-number outbound wires that resemble two seeded `FRAUD`
reference profiles. So Alpha has a real bull case, Beta has a real bear case, and
the vector search makes the tension concrete instead of hypothetical.

> ⚠️ A demo, not a credit-risk product. The verdict is illustrative; don't make
> lending decisions from it.
