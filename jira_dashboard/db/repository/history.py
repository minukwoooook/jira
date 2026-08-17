import logging

from jira_dashboard.jira.models import MAX_CHANGELOG_STR_BYTES, ChangelogItem
from jira_dashboard.jira.parser import truncate

log = logging.getLogger(__name__)

_MERGE_CHANGELOG = """
MERGE INTO test_issue_changelog t
USING (SELECT :issue_id AS issue_id, :jira_history_id AS jira_history_id,
              :item_seq AS item_seq FROM dual) s
ON (t.issue_id = s.issue_id AND t.jira_history_id = s.jira_history_id
    AND t.item_seq = s.item_seq)
WHEN MATCHED THEN UPDATE SET t.field_pk = :field_pk, t.field_name = :field_name,
     t.from_id = :from_id, t.from_str = :from_str,
     t.to_id = :to_id, t.to_str = :to_str
WHEN NOT MATCHED THEN
  INSERT (issue_id, jira_history_id, item_seq, author_user_key,
          author_display_name, changed_at, field_pk, field_name,
          from_id, from_str, to_id, to_str)
  VALUES (:issue_id, :jira_history_id, :item_seq, :author_user_key,
          :author_display_name, :changed_at, :field_pk, :field_name,
          :from_id, :from_str, :to_id, :to_str)
"""


def _resolve_field_pk(item, field_pks: dict[str, int],
                      field_names: dict[str, int]) -> int | None:
    """fieldId → field_id → unambiguous field_name → None (spec §4.2, Correction 3).

    10.3의 ChangeItemBean에는 fieldId가 없다 — field, fieldtype, from, fromString,
    to, toString뿐이다. fieldId만 보고 매칭하면 실제 응답 대부분에서 field_pk가
    None이 되어 TEST_ISSUE_FIELD_HISTORY가 텅 빈다. 그래서 세 단계로 해석한다:
    1) fieldId가 있고 카탈로그에 있으면 그것.
    2) 없으면 field 문자열이 시스템 필드 id 자체인 경우 (예: "status").
    3) 그것도 아니면 커스텀 필드의 표시 이름으로 매칭 — 단, 인스턴스 안에서
       이름이 모호하면 field_names에서 이미 빠져 있으므로 자연히 None이 된다.
    """
    if item.field_id and item.field_id in field_pks:
        return field_pks[item.field_id]
    name = item.field_name
    if name:
        if name in field_pks:      # 시스템 필드는 field 문자열 자체가 field_id다 (예: "status")
            return field_pks[name]
        if name in field_names:    # 커스텀 필드는 표시 이름으로 온다
            return field_names[name]
    return None


def upsert_changelog(conn, issue_id: int, items,
                     field_pks: dict[str, int],
                     field_names: dict[str, int]) -> set[str]:
    """A5: fieldId가 있으면 그것으로 매칭. 없으면 이름으로 시도한다 (Correction 3).

    field_name이 빈 문자열인 항목은 건너뛴다 — TEST_ISSUE_CHANGELOG.field_name은
    NOT NULL이고, Oracle은 빈 문자열을 NULL로 저장하므로 그대로 넣으면 ORA-01400이다.
    반환값은 field_pk 해석에 실패한(카탈로그 어디에도 없는) field_name의 집합이다 —
    호출자가 여러 이슈에 걸쳐 모아서 한 번만 로그하도록 개수 집계는 넘기지 않는다.
    """
    if not items:
        return set()
    rows = []
    unresolved: set[str] = set()
    for item in items:
        if not item.field_name:
            log.warning(
                "issue %s: changelog item %s#%d has no field name; skipping row "
                "(would violate NOT NULL / stores as NULL under Oracle empty-string rules)",
                issue_id, item.history_id, item.item_seq,
            )
            continue
        field_pk = _resolve_field_pk(item, field_pks, field_names)
        if field_pk is None:
            unresolved.add(item.field_name)
        rows.append({
            "issue_id": issue_id, "jira_history_id": item.history_id,
            "item_seq": item.item_seq,
            "author_user_key": item.author_user_key,
            "author_display_name": item.author_display_name,
            "changed_at": item.changed_at,
            "field_pk": field_pk,
            "field_name": item.field_name,
            "from_id": item.from_id,
            "from_str": truncate(item.from_str, MAX_CHANGELOG_STR_BYTES) or None,
            "to_id": item.to_id,
            "to_str": truncate(item.to_str, MAX_CHANGELOG_STR_BYTES) or None,
        })
    if rows:
        conn.cursor().executemany(_MERGE_CHANGELOG, rows, batcherrors=False)
    return unresolved


_SELECT_DIMENSION_FIELDS = """
SELECT field_id, field_pk FROM test_jira_field
WHERE  instance_id = :instance_id AND is_dimension = 'Y'
"""

_SELECT_STATUS_CATEGORIES = """
SELECT DISTINCT status_name, status_category FROM test_jira_issue
WHERE  instance_id = :instance_id AND status_name IS NOT NULL
"""

_SELECT_ISSUE_STATES = """
SELECT issue_id, created_at, status_name FROM test_jira_issue
WHERE  issue_id IN ({placeholders})
"""

_SELECT_CHANGES = """
SELECT c.issue_id, c.jira_history_id, c.item_seq, c.changed_at,
       f.field_id, c.field_name, c.from_id, c.from_str, c.to_id, c.to_str
FROM   test_issue_changelog c
LEFT   JOIN test_jira_field f ON f.field_pk = c.field_pk
WHERE  c.issue_id IN ({placeholders})
ORDER  BY c.issue_id, c.changed_at, c.item_seq
"""

_DELETE_HISTORY = "DELETE FROM test_issue_field_history WHERE issue_id = :issue_id"

_INSERT_HISTORY = """
INSERT INTO test_issue_field_history
       (issue_id, field_pk, valid_from, valid_to, val_str, val_id)
VALUES (:issue_id, :field_pk, :valid_from, :valid_to, :val_str, :val_id)
"""

_MERGE_FIRST_DONE = """
MERGE INTO test_jira_issue t
USING (
  SELECT h.issue_id, MIN(h.valid_from) AS first_done_at
  FROM   test_issue_field_history h
  JOIN   test_jira_field f ON f.field_pk = h.field_pk
                          AND f.field_id = 'status_category'
  WHERE  h.val_str = 'done' AND h.issue_id IN ({placeholders})
  GROUP  BY h.issue_id
) s ON (t.issue_id = s.issue_id)
WHEN MATCHED THEN UPDATE SET t.first_done_at = s.first_done_at
"""


def _binds(issue_ids: list[int]) -> tuple[str, dict]:
    binds = {f"b{i}": v for i, v in enumerate(issue_ids)}
    return ", ".join(f":{k}" for k in binds), binds


def dimension_field_pks(conn, instance_id: int) -> dict[str, int]:
    cur = conn.cursor()
    cur.execute(_SELECT_DIMENSION_FIELDS, instance_id=instance_id)
    return {f: pk for f, pk in cur.fetchall()}


def status_category_map(conn, instance_id: int) -> dict[str, str]:
    """이미 적재된 이슈에서 상태명 → 카테고리 대응을 만든다.

    /rest/api/2/status 를 다시 부르지 않아도 되게 DB에서 뽑는다. 아직 본 적 없는
    상태는 merge_categories에서 'undefined'로 떨어진다. 워크플로우에서 이미 빠진
    상태(현재 어느 이슈도 갖고 있지 않은 상태)는 여기 없다 — derive_history의
    category_of 파라미터로 /rest/api/2/status 기반 맵을 넘기면 우회된다
    (Correction 3).
    """
    cur = conn.cursor()
    cur.execute(_SELECT_STATUS_CATEGORIES, instance_id=instance_id)
    return {name: cat for name, cat in cur.fetchall()}


def load_issue_states(conn, issue_ids: list[int]) -> dict[int, dict]:
    placeholders, binds = _binds(issue_ids)
    cur = conn.cursor()
    cur.execute(_SELECT_ISSUE_STATES.format(placeholders=placeholders), **binds)
    return {
        iid: {"created_at": created, "current_values": {"status": (status, None)}}
        for iid, created, status in cur.fetchall()
    }


def load_changes(conn, issue_ids: list[int]) -> dict[int, list[ChangelogItem]]:
    placeholders, binds = _binds(issue_ids)
    cur = conn.cursor()
    cur.execute(_SELECT_CHANGES.format(placeholders=placeholders), **binds)
    out: dict[int, list[ChangelogItem]] = {}
    for (iid, hid, seq, at, field_id, field_name,
         from_id, from_str, to_id, to_str) in cur.fetchall():
        out.setdefault(iid, []).append(ChangelogItem(
            history_id=hid, item_seq=seq, author_user_key=None,
            author_display_name=None, changed_at=at, field_name=field_name,
            field_id=field_id, from_id=from_id, from_str=from_str,
            to_id=to_id, to_str=to_str,
        ))
    return out


def replace_history(conn, issue_id: int, rows: list[dict]) -> None:
    """부분 갱신하지 않는다. 이슈 단위로 지우고 다시 넣는다 (spec §5.3)."""
    cur = conn.cursor()
    cur.execute(_DELETE_HISTORY, issue_id=issue_id)
    if rows:
        cur.executemany(_INSERT_HISTORY, rows, batcherrors=False)


def update_first_done_at(conn, issue_ids: list[int]) -> int:
    placeholders, binds = _binds(issue_ids)
    cur = conn.cursor()
    cur.execute(_MERGE_FIRST_DONE.format(placeholders=placeholders), **binds)
    return cur.rowcount
