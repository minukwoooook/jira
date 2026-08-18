import gzip
import hashlib
import json
from dataclasses import dataclass

import oracledb

_RAW_TABLES = {"test_issue_raw", "test_changelog_raw"}

_SELECT_EXISTING = """
SELECT i.jira_issue_id, i.issue_id, r.payload_hash
FROM   test_jira_issue i
LEFT   JOIN test_issue_raw r ON r.issue_id = i.issue_id
WHERE  i.instance_id = :instance_id AND i.jira_issue_id IN ({placeholders})
"""

_NEXT_IDS = """
SELECT test_seq_issue_id.NEXTVAL FROM dual CONNECT BY LEVEL <= :n
"""

_MERGE_ISSUE = """
MERGE INTO test_jira_issue t
USING (SELECT :instance_id AS instance_id,
              :jira_issue_id AS jira_issue_id FROM dual) s
ON (t.instance_id = s.instance_id AND t.jira_issue_id = s.jira_issue_id)
WHEN MATCHED THEN UPDATE SET
  t.project_id = :project_id, t.issue_key = :issue_key,
  t.issue_type_name = :issue_type_name, t.status_name = :status_name,
  t.status_category = :status_category, t.priority_name = :priority_name,
  t.resolution_name = :resolution_name,
  t.assignee_user_key = :assignee_user_key,
  t.assignee_display_name = :assignee_display_name,
  t.reporter_user_key = :reporter_user_key,
  t.reporter_display_name = :reporter_display_name,
  t.parent_key = :parent_key, t.summary = :summary,
  t.created_at = :created_at, t.updated_at = :updated_at,
  t.resolved_at = :resolved_at, t.due_date = :due_date,
  t.original_estimate_sec = :original_estimate_sec,
  t.remaining_estimate_sec = :remaining_estimate_sec,
  t.time_spent_sec = :time_spent_sec,
  t.synced_at = (SYS_EXTRACT_UTC(SYSTIMESTAMP) + INTERVAL '9' HOUR),
  t.deleted_at = NULL, t.delete_reason = NULL
WHEN NOT MATCHED THEN
  INSERT (issue_id, instance_id, project_id, jira_issue_id, issue_key,
          issue_type_name, status_name, status_category, priority_name,
          resolution_name, assignee_user_key, assignee_display_name,
          reporter_user_key, reporter_display_name, parent_key, summary,
          created_at, updated_at, resolved_at, due_date,
          original_estimate_sec, remaining_estimate_sec, time_spent_sec)
  VALUES (:issue_id, :instance_id, :project_id, :jira_issue_id, :issue_key,
          :issue_type_name, :status_name, :status_category, :priority_name,
          :resolution_name, :assignee_user_key, :assignee_display_name,
          :reporter_user_key, :reporter_display_name, :parent_key, :summary,
          :created_at, :updated_at, :resolved_at, :due_date,
          :original_estimate_sec, :remaining_estimate_sec, :time_spent_sec)
"""

_TOUCH_SYNCED = """
UPDATE test_jira_issue SET synced_at = (SYS_EXTRACT_UTC(SYSTIMESTAMP) + INTERVAL '9' HOUR)
WHERE  issue_id = :issue_id
"""

_MERGE_RAW = """
MERGE INTO {table} t
USING (SELECT :issue_id AS issue_id FROM dual) s
ON (t.issue_id = s.issue_id)
WHEN MATCHED THEN UPDATE SET t.payload = :payload,
     t.payload_hash = :payload_hash,
     t.fetched_at = (SYS_EXTRACT_UTC(SYSTIMESTAMP) + INTERVAL '9' HOUR)
WHEN NOT MATCHED THEN INSERT (issue_id, payload, payload_hash)
                      VALUES (:issue_id, :payload, :payload_hash)
"""

_DELETE_VALUES = "DELETE FROM test_issue_field_value WHERE issue_id = :issue_id"

_INSERT_VALUE = """
INSERT INTO test_issue_field_value
       (issue_id, field_pk, val_seq, val_str, val_num, val_date, val_id)
VALUES (:issue_id, :field_pk, :val_seq, :val_str, :val_num, :val_date, :val_id)
"""


@dataclass(frozen=True)
class ExistingIssue:
    issue_id: int
    payload_hash: str | None


def canonical_json(obj) -> bytes:
    """안정적인 바이트 표현. 해시가 gzip 프레이밍에 좌우되면 안 된다 (Correction 1)."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")


def gzip_bytes(data: bytes) -> bytes:
    """mtime=0으로 고정해 같은 입력이 항상 같은 출력을 내도록 한다.

    gzip.compress()는 기본적으로 헤더에 현재 시각을 적어 넣어, 같은 JSON을
    두 번 압축해도 바이트가 달라진다. 그 압축 결과를 해시하면 payload_hash가
    두 번째 실행에서 절대 일치하지 않아 스킵 경로가 죽는다 (Correction 1).
    """
    return gzip.compress(data, mtime=0)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_existing(conn, instance_id: int,
                  jira_issue_ids: list[str]) -> dict[str, ExistingIssue]:
    """적재 ①단계. FK 때문에 raw를 먼저 쓸 수 없으므로 읽기를 먼저 한다 (spec §5.2)."""
    if not jira_issue_ids:
        return {}
    binds = {f"b{i}": v for i, v in enumerate(jira_issue_ids)}
    sql = _SELECT_EXISTING.format(
        placeholders=", ".join(f":{k}" for k in binds)
    )
    cur = conn.cursor()
    cur.execute(sql, instance_id=instance_id, **binds)
    return {j: ExistingIssue(iid, h) for j, iid, h in cur.fetchall()}


def next_issue_ids(conn, n: int) -> list[int]:
    """MERGE가 RETURNING을 못 쓰므로 시퀀스에서 미리 받는다 (spec §3.3.0)."""
    if n <= 0:
        return []
    cur = conn.cursor()
    cur.execute(_NEXT_IDS, n=n)
    return [r[0] for r in cur.fetchall()]


def upsert_issues(conn, rows: list[dict]) -> None:
    if not rows:
        return
    conn.cursor().executemany(_MERGE_ISSUE, rows, batcherrors=False)


def touch_synced_at(conn, issue_ids: list[int]) -> None:
    if not issue_ids:
        return
    conn.cursor().executemany(
        _TOUCH_SYNCED, [{"issue_id": i} for i in issue_ids], batcherrors=False
    )


def upsert_raw(conn, table: str, rows: list[dict]) -> None:
    if table not in _RAW_TABLES:        # 식별자는 화이트리스트에서만 온다
        raise ValueError(f"unknown raw table: {table}")
    if not rows:
        return
    cur = conn.cursor()
    cur.setinputsizes(payload=oracledb.DB_TYPE_BLOB)
    cur.executemany(_MERGE_RAW.format(table=table), rows, batcherrors=False)


def replace_field_values(conn, issue_id: int, values: list[dict]) -> None:
    """부분 갱신하지 않는다. 이슈 단위로 지우고 다시 넣는다 (spec §5.2)."""
    cur = conn.cursor()
    cur.execute(_DELETE_VALUES, issue_id=issue_id)
    if values:
        cur.executemany(_INSERT_VALUE, values, batcherrors=False)
