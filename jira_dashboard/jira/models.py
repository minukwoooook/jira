from dataclasses import dataclass
from datetime import date, datetime

SENTINEL = datetime(9999, 12, 31)
MAX_VAL_STR_BYTES = 1000


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
