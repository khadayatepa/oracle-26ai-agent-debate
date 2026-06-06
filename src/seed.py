"""
Create the schema and load demo data WITH real OpenAI embeddings — entirely
through the SQLcl MCP server (the same path the debate agents use).

No python-oracledb / wallet config required: if `sql -mcp` can reach your saved
connection, seeding works. Embeddings are generated client-side via OpenAI and
inserted as TO_VECTOR(...) literals.

Run:  python src/seed.py
"""
from __future__ import annotations

import asyncio
import datetime as dt

from openai import AsyncOpenAI

import config
from mcp_oracle import open_oracle_mcp, OracleMCP
from tools import _vector_literal

# --- demo data --------------------------------------------------------------
CUSTOMERS = [
    # id, name, industry, years, annual_revenue, requested_credit
    (994221, "Helios Freight Logistics", "Freight & Logistics", 7, 4_200_000, 2_000_000),
    (771043, "Cedarworks Manufacturing", "Industrial Manufacturing", 22, 9_800_000, 1_500_000),
    (880210, "Pinpoint Retail Group", "Retail", 4, 1_300_000, 900_000),
]


def _txns_994221() -> list[tuple]:
    base = dt.date(2025, 1, 1)
    rows: list[tuple] = []
    inflow = 280_000
    for m in range(12):
        d = base + dt.timedelta(days=30 * m)
        inflow = int(inflow * 1.06)  # steady ~6% monthly revenue growth
        rows.append((994221, d, inflow, "CUSTOMER_RECEIPT"))
        rows.append((994221, d + dt.timedelta(days=5), -int(inflow * 0.55), "PAYROLL_AND_OPEX"))
    for d, amt in [
        (dt.date(2025, 4, 18), -250_000),
        (dt.date(2025, 7, 22), -300_000),
        (dt.date(2025, 10, 9), -250_000),
    ]:
        rows.append((994221, d, amt, "OUTBOUND_WIRE"))  # suspicious round-number wires
    return rows


def _txns_other(cid: int, monthly: int) -> list[tuple]:
    base = dt.date(2025, 1, 1)
    rows: list[tuple] = []
    for m in range(12):
        d = base + dt.timedelta(days=30 * m)
        rows.append((cid, d, monthly, "CUSTOMER_RECEIPT"))
        rows.append((cid, d + dt.timedelta(days=4), -int(monthly * 0.6), "PAYROLL_AND_OPEX"))
    return rows


RISK_PROFILES = [
    (994221, "WATCH", 58,
     "Mid-size freight logistics firm, 7 years operating, strong recent revenue "
     "growth and consistent customer receipts, but several large round-number "
     "outbound wire transfers and rapid expansion into new regions."),
    (771043, "GOOD", 12,
     "Established industrial manufacturer, 22 years operating, steady recurring "
     "revenue from long-term supplier contracts and a clean on-time repayment "
     "history across multiple prior facilities."),
    (880210, "WATCH", 49,
     "Young retail group with thin margins and seasonal cash flow swings; limited "
     "credit history but no adverse events on record."),
    (900001, "FRAUD", 95,
     "Freight forwarding shell company showing rapid revenue growth with circular "
     "invoicing and large round-number transfers to offshore accounts; defaulted "
     "after nine months amid invoice-fraud findings."),
    (900002, "FRAUD", 91,
     "Logistics operator inflating receivables with frequent large outbound wires "
     "and mismatched shipping manifests; flagged for trade-based money laundering."),
    (900003, "GOOD", 8,
     "Regional carrier with diversified customer base, predictable monthly "
     "receipts, modest leverage, and a decade of flawless repayments."),
]


# --- SQL helpers ------------------------------------------------------------
def _q(text: str) -> str:
    """Quote a string literal for SQL, escaping quotes and '&'.

    SQLcl treats '&' as a substitution-variable prefix and SET DEFINE OFF does not
    persist across MCP calls, so encode any '&' as CHR(38) concatenation instead.
    """
    t = text.replace("'", "''")
    if "&" in t:
        return "'" + t.replace("&", "'||CHR(38)||'") + "'"
    return "'" + t + "'"


def _date(d: dt.date) -> str:
    return f"TO_DATE('{d.isoformat()}','YYYY-MM-DD')"


DROP_BLOCK = (
    "BEGIN FOR t IN (SELECT table_name FROM user_tables WHERE table_name IN "
    "('TRANSACTIONS','RISK_PROFILES','CUSTOMERS')) LOOP "
    "EXECUTE IMMEDIATE 'DROP TABLE '||t.table_name||' CASCADE CONSTRAINTS'; "
    "END LOOP; END;"
)

CREATE_CUSTOMERS = (
    "CREATE TABLE customers (customer_id NUMBER PRIMARY KEY, name VARCHAR2(120), "
    "industry VARCHAR2(80), years_in_business NUMBER, annual_revenue NUMBER, "
    "requested_credit NUMBER)"
)
CREATE_TRANSACTIONS = (
    # No IDENTITY column: INSERT ALL evaluates an identity sequence only once for
    # the whole statement, which collides. We never reference a txn id anyway.
    "CREATE TABLE transactions (customer_id NUMBER, txn_date DATE, amount NUMBER, "
    "txn_type VARCHAR2(40))"
)


def create_risk_profiles_ddl() -> str:
    return (
        "CREATE TABLE risk_profiles (customer_id NUMBER, label VARCHAR2(20), "
        "risk_score NUMBER, profile_text VARCHAR2(4000), "
        f"embedding VECTOR({config.EMBED_DIM}, FLOAT32))"
    )


def insert_all(table_cols: str, value_rows: list[str]) -> str:
    """Build a single INSERT ALL statement."""
    body = "\n".join(f"  INTO {table_cols} VALUES ({row})" for row in value_rows)
    return f"INSERT ALL\n{body}\nSELECT 1 FROM DUAL"


async def _run(mcp: OracleMCP, sql: str, what: str) -> None:
    out = await mcp.run_sql(sql)
    first = out.splitlines()[0] if out else "ok"
    if "ORA-" in out or "Error" in out or "cancelled" in out:
        ora = next((ln for ln in out.splitlines() if "ORA-" in ln), "")
        raise RuntimeError(f"{what} FAILED:\n{ora or out[-600:]}")
    print(f"  · {what}: {first[:80]}")


async def main() -> None:
    client = AsyncOpenAI(api_key=config.OPENAI_API_KEY or None)

    print(f"Embedding {len(RISK_PROFILES)} risk narratives with {config.EMBED_MODEL} ...")
    emb = await client.embeddings.create(
        model=config.EMBED_MODEL, input=[p[3] for p in RISK_PROFILES]
    )
    vectors = [d.embedding for d in emb.data]

    async with open_oracle_mcp(config.SQLCL_COMMAND, config.ORACLE_MCP_CONNECTION) as mcp:
        print(f"Connected via SQLcl MCP (connection '{config.ORACLE_MCP_CONNECTION}').\n")

        # Stop SQLcl from treating '&' in data (e.g. "Freight & Logistics") as a
        # substitution variable.
        await mcp.run_sqlcl("set define off")

        print("Creating tables ...")
        await _run(mcp, DROP_BLOCK, "drop old tables")
        await _run(mcp, CREATE_CUSTOMERS, "customers")
        await _run(mcp, CREATE_TRANSACTIONS, "transactions")
        await _run(mcp, create_risk_profiles_ddl(), "risk_profiles")

        print("Inserting customers ...")
        cust_rows = [
            f"{cid},{_q(name)},{_q(ind)},{yrs},{rev},{req}"
            for (cid, name, ind, yrs, rev, req) in CUSTOMERS
        ]
        await _run(mcp, insert_all("customers", cust_rows), "customers loaded")

        print("Inserting transactions ...")
        all_txns = (
            _txns_994221()
            + _txns_other(771043, 800_000)
            + _txns_other(880210, 110_000)
        )
        txn_rows = [f"{cid},{_date(d)},{amt},{_q(t)}" for (cid, d, amt, t) in all_txns]
        await _run(
            mcp,
            insert_all("transactions (customer_id, txn_date, amount, txn_type)", txn_rows),
            f"{len(txn_rows)} transactions loaded",
        )

        print("Inserting risk profiles + embeddings ...")
        for (cid, label, score, text), vec in zip(RISK_PROFILES, vectors):
            sql = (
                "INSERT INTO risk_profiles (customer_id, label, risk_score, profile_text, embedding) "
                f"VALUES ({cid},{_q(label)},{score},{_q(text)},"
                f"TO_VECTOR('{_vector_literal(vec)}'))"
            )
            await mcp.run_sql(sql)
        print(f"  · {len(RISK_PROFILES)} profiles loaded")

        print("Building HNSW vector index ...")
        try:
            await mcp.run_sql("DROP INDEX risk_profiles_hnsw_idx")
        except Exception:
            pass
        try:
            await mcp.run_sql(
                "CREATE VECTOR INDEX risk_profiles_hnsw_idx ON risk_profiles (embedding) "
                "ORGANIZATION INMEMORY NEIGHBOR GRAPH DISTANCE COSINE WITH TARGET ACCURACY 95"
            )
            print("  · index created")
        except Exception as exc:
            # ADB may need the vector memory pool. Not fatal: FETCH APPROX falls
            # back to an exact search when no index exists.
            print(f"  ! Skipping vector index ({exc}). Exact search will be used.")

        await mcp.run_sql("COMMIT")
        print("\nDone. Seed complete.")


if __name__ == "__main__":
    asyncio.run(main())
