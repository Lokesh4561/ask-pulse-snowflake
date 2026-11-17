-- Optional: Cortex Search service for "why" questions (not available on trial accounts,
-- because trials cannot use the embedding function it needs). After running this,
-- set ASK_PULSE_SEARCH=cortex in the workflows / app to use it instead of keyword retrieval.
USE ROLE ACCOUNTADMIN;
USE SCHEMA ASK_PULSE.CORE;

CREATE OR REPLACE CORTEX SEARCH SERVICE EXPLAIN_SEARCH
  ON SEARCH_TEXT
  ATTRIBUTES LANGUAGE_CODE
  WAREHOUSE = ASK_PULSE_WH
  TARGET_LAG = '1 hour'
  AS SELECT SEARCH_TEXT, PAGE_TITLE, LANGUAGE_CODE, LANGUAGE_NAME, HOUR_UTC, WHY_CATEGORY, WHY_REASON_EN, SOURCES
     FROM EXPLANATIONS;

GRANT USAGE ON CORTEX SEARCH SERVICE ASK_PULSE.CORE.EXPLAIN_SEARCH TO ROLE ASK_PULSE_ROLE;
