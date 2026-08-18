from dataclasses import dataclass
from datetime import date, datetime, timezone

SENTINEL = datetime(9999, 12, 31, tzinfo=timezone.utc)

# 컬럼 폭 상한. 값은 DDL에서 선언한 BYTE 폭과 같아야 하며, 그 일치는
# tests/static/test_column_widths.py가 schema_map.column_byte_limits로 대조한다 —
# 손으로 적은 숫자와 DDL이 어긋나는 것이 ORA-12899의 실제 원인이었다.
MAX_VAL_STR_BYTES = 1000        # TEST_ISSUE_FIELD_VALUE/HISTORY.val_str
MAX_VAL_ID_BYTES = 100          # 같은 두 테이블의 val_id
MAX_CHANGELOG_STR_BYTES = 4000  # TEST_ISSUE_CHANGELOG.from_str/to_str
MAX_CHANGELOG_ID_BYTES = 255    # 같은 테이블의 from_id/to_id — Jira는 여기에
                                # Sprint/다중선택 변경의 콤마 결합 id 목록을 넣는다
MAX_NAME_BYTES = 255            # field_name, author_display_name, *_user_key,
                                # *_display_name
MAX_SHORT_NAME_BYTES = 100      # issue_type_name/status_name/priority_name/
                                # resolution_name
MAX_KEY_BYTES = 50              # issue_key, jira_issue_id, parent_key
MAX_SUMMARY_BYTES = 1024        # TEST_JIRA_ISSUE.summary
MAX_ERROR_MSG_BYTES = 4000      # TEST_SYNC_RUN.error_msg
MAX_SCHEMA_TYPE_BYTES = 50      # TEST_JIRA_FIELD.schema_type/schema_items
MAX_CUSTOM_TYPE_BYTES = 200     # TEST_JIRA_FIELD.custom_type


@dataclass(frozen=True)
class FieldDef:
    field_id: str
    field_name: str
    is_custom: bool
    schema_type: str | None
    schema_items: str | None
    custom_type: str | None


@dataclass(frozen=True)
class FieldValue:
    field_id: str
    val_seq: int
    val_str: str | None
    val_num: float | None
    val_date: datetime | None
    val_id: str | None


@dataclass(frozen=True)
class ChangelogItem:
    history_id: str
    item_seq: int
    author_user_key: str | None
    author_display_name: str | None
    changed_at: datetime
    field_name: str
    field_id: str | None
    from_id: str | None
    from_str: str | None
    to_id: str | None
    to_str: str | None


@dataclass(frozen=True)
class ParsedIssue:
    jira_issue_id: str
    issue_key: str
    project_jira_id: str
    issue_type_name: str | None
    status_name: str | None
    status_category: str | None
    priority_name: str | None
    resolution_name: str | None
    assignee_user_key: str | None
    assignee_display_name: str | None
    reporter_user_key: str | None
    reporter_display_name: str | None
    parent_key: str | None
    summary: str | None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    due_date: date | None
    original_estimate_sec: int | None
    remaining_estimate_sec: int | None
    time_spent_sec: int | None
    custom_values: tuple[FieldValue, ...]
    changelog: tuple[ChangelogItem, ...]
    changelog_total: int
