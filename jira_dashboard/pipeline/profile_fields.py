import logging

from jira_dashboard.db.repository.catalog import field_pk_by_field_id
from jira_dashboard.jira.fieldmap import SYSTEM_FIELD_MAP

log = logging.getLogger(__name__)

# 컬럼명은 SYSTEM_FIELD_MAP에서만 온다 — SQL 조립이 안전한 이유
COLUMN_FIELDS: list[tuple[str, str]] = [
    (field_id, spec.column_name) for field_id, spec in SYSTEM_FIELD_MAP.items()
]

RESET_COUNTS = """
UPDATE test_jira_project_field SET issue_count = 0, distinct_value_count = 0
WHERE  project_id IN (SELECT project_id FROM test_jira_project
                      WHERE instance_id = :instance_id)
"""

MERGE_EAV_COUNTS = """
MERGE INTO test_jira_project_field t
USING (
  SELECT i.project_id, v.field_pk,
         COUNT(DISTINCT v.issue_id) AS issue_count,
         COUNT(DISTINCT COALESCE(v.val_str, TO_CHAR(v.val_num, 'TM'),
                                 TO_CHAR(v.val_date, 'YYYY-MM-DD HH24:MI:SS.FF6')))
           AS distinct_value_count
  FROM   test_issue_field_value v
  JOIN   test_jira_issue i ON i.issue_id = v.issue_id
  WHERE  i.instance_id = :instance_id AND i.deleted_at IS NULL
  GROUP  BY i.project_id, v.field_pk
) s ON (t.project_id = s.project_id AND t.field_pk = s.field_pk)
WHEN MATCHED THEN UPDATE SET
  t.issue_count = s.issue_count,
  t.distinct_value_count = s.distinct_value_count,
  t.last_profiled_at = (SYS_EXTRACT_UTC(SYSTIMESTAMP) + INTERVAL '9' HOUR)
WHEN NOT MATCHED THEN
  INSERT (project_id, field_pk, issue_count, distinct_value_count, last_profiled_at)
  VALUES (s.project_id, s.field_pk, s.issue_count, s.distinct_value_count,
          (SYS_EXTRACT_UTC(SYSTIMESTAMP) + INTERVAL '9' HOUR))
"""

MERGE_COLUMN_COUNTS = """
MERGE INTO test_jira_project_field t
USING (SELECT :project_id AS project_id, :field_pk AS field_pk FROM dual) s
ON (t.project_id = s.project_id AND t.field_pk = s.field_pk)
WHEN MATCHED THEN UPDATE SET t.issue_count = :issue_count,
     t.distinct_value_count = :distinct_value_count,
     t.last_profiled_at = (SYS_EXTRACT_UTC(SYSTIMESTAMP) + INTERVAL '9' HOUR)
WHEN NOT MATCHED THEN
  INSERT (project_id, field_pk, issue_count, distinct_value_count, last_profiled_at)
  VALUES (:project_id, :field_pk, :issue_count, :distinct_value_count,
          (SYS_EXTRACT_UTC(SYSTIMESTAMP) + INTERVAL '9' HOUR))
"""

SELECT_AXIS_CANDIDATES = """
SELECT f.field_id, f.field_name, pf.issue_count
FROM   test_jira_project_field pf
JOIN   test_jira_field f ON f.field_pk = pf.field_pk
WHERE  pf.project_id = :project_id AND f.is_dimension = 'Y'
ORDER  BY pf.issue_count DESC, f.field_name
"""


def _column_scan_sql() -> str:
    counts = ",\n       ".join(
        f"COUNT({column}) AS c_{i}" for i, (_, column) in enumerate(COLUMN_FIELDS)
    )
    distincts = ",\n       ".join(
        f"COUNT(DISTINCT {column}) AS d_{i}"
        for i, (_, column) in enumerate(COLUMN_FIELDS)
    )
    return (
        f"SELECT project_id,\n       {counts},\n       {distincts}\n"
        "FROM   test_jira_issue\n"
        "WHERE  instance_id = :instance_id AND deleted_at IS NULL\n"
        "GROUP  BY project_id"
    )


def profile_fields(conn, instance_id: int) -> int:
    cur = conn.cursor()
    # ① 옛 카운트를 0으로. 안 하면 값을 비운 필드가 계속 축 후보에 뜬다.
    cur.execute(RESET_COUNTS, instance_id=instance_id)
    # ② EAV 전체를 한 번만 훑는다
    cur.execute(MERGE_EAV_COUNTS, instance_id=instance_id)
    updated = cur.rowcount

    # ③ 고정 컬럼은 JIRA_ISSUE 1회 스캔으로 전부 계산
    cur.execute(_column_scan_sql(), instance_id=instance_id)
    rows = cur.fetchall()
    field_pks = field_pk_by_field_id(conn, instance_id)

    n = len(COLUMN_FIELDS)
    payload = []
    for row in rows:
        project_id = row[0]
        for idx, (field_id, _) in enumerate(COLUMN_FIELDS):
            field_pk = field_pks.get(field_id)
            if field_pk is None:
                continue
            payload.append({
                "project_id": project_id, "field_pk": field_pk,
                "issue_count": row[1 + idx],
                "distinct_value_count": row[1 + n + idx],
            })
    if payload:
        cur.executemany(MERGE_COLUMN_COUNTS, payload, batcherrors=False)
        updated += len(payload)

    for field_id in ("timespent", "timeoriginalestimate", "timeestimate"):
        field_pk = field_pks.get(field_id)
        if field_pk is None:
            continue
        # all()은 빈 generator에서 True다 — 프로젝트가 없거나 이 필드가 payload에
        # 없으면 "Time Tracking이 꺼졌다"는 로그가 근거 없이 나왔다. 관측한 행이
        # 있을 때만 판단한다 (R34의 "관측하지 못한 것에 판정을 주지 않는다"의 로그판).
        counts = [p["issue_count"] for p in payload if p["field_pk"] == field_pk]
        if counts and all(c == 0 for c in counts):
            log.info("field %s has no values — Time Tracking may be disabled", field_id)

    conn.commit()
    return updated


def axis_candidates(conn, project_id: int) -> list[tuple[str, str, int]]:
    cur = conn.cursor()
    cur.execute(SELECT_AXIS_CANDIDATES, project_id=project_id)
    return list(cur.fetchall())
