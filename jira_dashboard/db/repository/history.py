import logging

from jira_dashboard.jira.models import MAX_CHANGELOG_STR_BYTES
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
