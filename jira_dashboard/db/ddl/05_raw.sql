CREATE TABLE test_issue_raw (
  issue_id     NUMBER(12)        NOT NULL,
  payload      BLOB              NOT NULL,   -- gzip(JSON)
  payload_hash VARCHAR2(64 BYTE) NOT NULL,   -- sha256 hex
  fetched_at   TIMESTAMP DEFAULT SYS_EXTRACT_UTC(SYSTIMESTAMP) NOT NULL,
  CONSTRAINT test_pk_issue_raw PRIMARY KEY (issue_id),
  CONSTRAINT test_fk_issue_raw FOREIGN KEY (issue_id) REFERENCES test_jira_issue (issue_id)
)
LOB (payload) STORE AS SECUREFILE (DISABLE STORAGE IN ROW);

CREATE TABLE test_changelog_raw (
  issue_id     NUMBER(12)        NOT NULL,
  payload      BLOB              NOT NULL,   -- gzip(JSON)
  payload_hash VARCHAR2(64 BYTE) NOT NULL,
  fetched_at   TIMESTAMP DEFAULT SYS_EXTRACT_UTC(SYSTIMESTAMP) NOT NULL,
  CONSTRAINT test_pk_changelog_raw PRIMARY KEY (issue_id),
  CONSTRAINT test_fk_changelog_raw FOREIGN KEY (issue_id) REFERENCES test_jira_issue (issue_id)
)
LOB (payload) STORE AS SECUREFILE (DISABLE STORAGE IN ROW);
