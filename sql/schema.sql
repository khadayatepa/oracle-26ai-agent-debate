-- ============================================================================
--  Multi-Agent Debate over Oracle 23ai / 26ai  —  schema
--  Scenario: a credit / loan-approval decision for one customer.
--  Run this against an Oracle Database 23ai (or 26ai) PDB.
--  NOTE: seed.py creates these objects too (and fills the VECTOR column with
--        real embeddings). This file is provided so you can inspect / run the
--        DDL by hand. It does NOT insert embeddings (those need OpenAI).
-- ============================================================================

-- Clean slate -----------------------------------------------------------------
BEGIN
  FOR t IN (SELECT table_name FROM user_tables
            WHERE table_name IN ('TRANSACTIONS','RISK_PROFILES','CUSTOMERS')) LOOP
    EXECUTE IMMEDIATE 'DROP TABLE ' || t.table_name || ' CASCADE CONSTRAINTS';
  END LOOP;
END;
/

-- Customers (transactional master data) --------------------------------------
CREATE TABLE customers (
  customer_id        NUMBER PRIMARY KEY,
  name               VARCHAR2(120) NOT NULL,
  industry           VARCHAR2(80),
  years_in_business  NUMBER,
  annual_revenue     NUMBER,
  requested_credit   NUMBER
);

-- Cash-flow / transaction history --------------------------------------------
-- No IDENTITY column on purpose: seed.py bulk-loads via INSERT ALL, which
-- evaluates an identity sequence only once per statement (causing PK collisions).
CREATE TABLE transactions (
  customer_id  NUMBER REFERENCES customers(customer_id),
  txn_date     DATE,
  amount       NUMBER,          -- positive = inflow, negative = outflow
  txn_type     VARCHAR2(40)
);

-- Risk profiles with a VECTOR embedding of the narrative -----------------------
-- EMBED_DIM must match your embedding model (text-embedding-3-small = 1536).
CREATE TABLE risk_profiles (
  customer_id   NUMBER,
  label         VARCHAR2(20),          -- GOOD | WATCH | FRAUD  (ground truth)
  risk_score    NUMBER,                -- 0 (safe) .. 100 (toxic)
  profile_text  VARCHAR2(4000),
  embedding     VECTOR(1536, FLOAT32)
);

-- HNSW vector index for fast approximate similarity search --------------------
-- (Valid Oracle 23ai syntax. Drop & recreate after the table is populated.)
CREATE VECTOR INDEX risk_profiles_hnsw_idx
  ON risk_profiles (embedding)
  ORGANIZATION INMEMORY NEIGHBOR GRAPH
  DISTANCE COSINE
  WITH TARGET ACCURACY 95;

-- Example similarity query the agents' vector_search tool generates ------------
--   SELECT customer_id, label, risk_score,
--          ROUND(VECTOR_DISTANCE(embedding, TO_VECTOR(:v), COSINE), 4) AS distance,
--          profile_text
--   FROM   risk_profiles
--   ORDER  BY VECTOR_DISTANCE(embedding, TO_VECTOR(:v), COSINE)
--   FETCH  APPROX FIRST 5 ROWS ONLY;
