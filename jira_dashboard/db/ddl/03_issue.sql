-- issue_id만 IDENTITY가 아니라 명시적 시퀀스를 쓴다. 이유는 바로 아래 설명.
CREATE SEQUENCE test_seq_issue_id START WITH 1 INCREMENT BY 1 CACHE 1000 NOCYCLE;

CREATE TABLE test_jira_issue (
  issue_id               NUMBER(12)          NOT NULL,
  instance_id            NUMBER(10)          NOT NULL,
  project_id             NUMBER(10)          NOT NULL,
  jira_issue_id          VARCHAR2(50 BYTE)   NOT NULL,
  issue_key              VARCHAR2(50 BYTE)   NOT NULL,
  issue_type_name        VARCHAR2(100 BYTE),
  status_name            VARCHAR2(100 BYTE),
  status_category        VARCHAR2(20 BYTE),          -- ★ 3.3.1 참고
  priority_name          VARCHAR2(100 BYTE),
  resolution_name        VARCHAR2(100 BYTE),
  assignee_user_key      VARCHAR2(255 BYTE),
  assignee_display_name  VARCHAR2(255 BYTE),
  reporter_user_key      VARCHAR2(255 BYTE),
  reporter_display_name  VARCHAR2(255 BYTE),
  parent_key             VARCHAR2(50 BYTE),
  summary                VARCHAR2(1024 BYTE),
  created_at             TIMESTAMP           NOT NULL,   -- UTC
  updated_at             TIMESTAMP           NOT NULL,   -- UTC, 동기화 워터마크 기준
  resolved_at            TIMESTAMP,                      -- UTC
  due_date               DATE,                           -- Jira duedate는 날짜만
  first_done_at          TIMESTAMP,                      -- ★ 3.3.2 참고, 파생
  original_estimate_sec  NUMBER(12),
  remaining_estimate_sec NUMBER(12),
  time_spent_sec         NUMBER(12),
  synced_at              TIMESTAMP DEFAULT SYS_EXTRACT_UTC(SYSTIMESTAMP) NOT NULL,
  deleted_at             TIMESTAMP,
  delete_reason          VARCHAR2(20 BYTE),              -- ★ 3.3.4 참고
  CONSTRAINT test_pk_jira_issue      PRIMARY KEY (issue_id),
  CONSTRAINT test_uq_jira_issue_id   UNIQUE (instance_id, jira_issue_id),
  CONSTRAINT test_uq_jira_issue_key  UNIQUE (instance_id, issue_key),
  CONSTRAINT test_fk_jira_issue_inst FOREIGN KEY (instance_id)
                                REFERENCES test_jira_instance (instance_id),
  CONSTRAINT test_fk_jira_issue_proj FOREIGN KEY (project_id)
                                REFERENCES test_jira_project (project_id),
  CONSTRAINT test_ck_jira_issue_sc   CHECK (status_category IN
                                ('new','indeterminate','done','undefined')),
  CONSTRAINT test_ck_jira_issue_del  CHECK (
      (deleted_at IS NULL     AND delete_reason IS NULL)
   OR (deleted_at IS NOT NULL AND delete_reason IN ('DELETED','MOVED_OUT')))
);

CREATE INDEX test_ix_issue_proj_created  ON test_jira_issue (project_id, created_at);
CREATE INDEX test_ix_issue_proj_status   ON test_jira_issue (project_id, status_name);
CREATE INDEX test_ix_issue_proj_statcat  ON test_jira_issue (project_id, status_category);
CREATE INDEX test_ix_issue_proj_resolved ON test_jira_issue (project_id, resolved_at);
CREATE INDEX test_ix_issue_sync          ON test_jira_issue (instance_id, updated_at);
CREATE INDEX test_ix_issue_assignee      ON test_jira_issue (assignee_user_key);

CREATE TABLE test_issue_field_value (
  issue_id NUMBER(12)          NOT NULL,
  field_pk NUMBER(10)          NOT NULL,
  val_seq  NUMBER(4)           DEFAULT 0 NOT NULL,   -- 다중값 필드의 순번
  val_str  VARCHAR2(1000 BYTE),
  val_num  NUMBER,
  val_date TIMESTAMP,                                 -- UTC
  val_id   VARCHAR2(100 BYTE),                        -- 옵션/사용자의 원본 ID
  CONSTRAINT test_pk_ifv       PRIMARY KEY (issue_id, field_pk, val_seq),
  CONSTRAINT test_fk_ifv_issue FOREIGN KEY (issue_id) REFERENCES test_jira_issue (issue_id),
  CONSTRAINT test_fk_ifv_field FOREIGN KEY (field_pk) REFERENCES test_jira_field (field_pk)
);

CREATE INDEX test_ix_ifv_str ON test_issue_field_value (field_pk, val_str, issue_id);

-- 필요해진 시점에만 생성
-- CREATE INDEX test_ix_ifv_num  ON test_issue_field_value (field_pk, val_num,  issue_id);
-- CREATE INDEX test_ix_ifv_date ON test_issue_field_value (field_pk, val_date, issue_id);
