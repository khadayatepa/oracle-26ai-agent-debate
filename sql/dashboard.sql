-- ============================================================================
--  Persistence + view for the debate dashboard (APEX or Streamlit).
--  Run as the DEBATE user (seed.py / debate.py create these automatically too).
-- ============================================================================

-- One row per debate run -----------------------------------------------------
CREATE TABLE debate_runs (
  run_id       NUMBER PRIMARY KEY,                 -- epoch id assigned by debate.py
  customer_id  NUMBER,
  created_at   TIMESTAMP DEFAULT SYSTIMESTAMP,
  model        VARCHAR2(60),
  verdict      CLOB
);

-- One row per argument turn (Alpha opening, Beta, Alpha rebuttal, ...) --------
CREATE TABLE debate_arguments (
  run_id       NUMBER,
  seq          NUMBER,
  phase        VARCHAR2(30),     -- opening | rebuttal
  persona      VARCHAR2(20),     -- ALPHA | BETA
  content      CLOB,
  created_at   TIMESTAMP DEFAULT SYSTIMESTAMP
);

-- Flattened feed an APEX report / cards region (or Streamlit) binds to --------
CREATE OR REPLACE VIEW v_debate_feed AS
SELECT r.run_id,
       r.customer_id,
       c.name              AS customer_name,
       r.created_at,
       r.model,
       a.seq,
       a.phase,
       a.persona,
       a.content           AS argument,
       r.verdict
FROM   debate_runs r
JOIN   debate_arguments a ON a.run_id = r.run_id
LEFT   JOIN customers   c ON c.customer_id = r.customer_id;

-- Latest run per customer (handy for an APEX dashboard "current verdict" card)
CREATE OR REPLACE VIEW v_debate_latest AS
SELECT r.*
FROM   debate_runs r
WHERE  r.created_at = (SELECT MAX(r2.created_at) FROM debate_runs r2
                       WHERE r2.customer_id = r.customer_id);
