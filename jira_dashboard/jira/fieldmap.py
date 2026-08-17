from dataclasses import dataclass


@dataclass(frozen=True)
class SystemFieldSpec:
    column_name: str
    label_column_name: str | None
    value_kind: str
    is_dimension: bool
    is_measure: bool


# spec §4.1 매핑표. 여기 없는 필드는 전부 EAV로 간다.
# labels/components/fixVersions는 시스템 필드지만 다중값이라 의도적으로 제외했다.
SYSTEM_FIELD_MAP: dict[str, SystemFieldSpec] = {
    "issuetype":            SystemFieldSpec("issue_type_name", None, "STR", True, False),
    "status":               SystemFieldSpec("status_name", None, "STR", True, False),
    "status_category":      SystemFieldSpec("status_category", None, "STR", True, False),
    "priority":             SystemFieldSpec("priority_name", None, "STR", True, False),
    "resolution":           SystemFieldSpec("resolution_name", None, "STR", True, False),
    "assignee":             SystemFieldSpec("assignee_user_key",
                                            "assignee_display_name", "STR", True, False),
    "reporter":             SystemFieldSpec("reporter_user_key",
                                            "reporter_display_name", "STR", True, False),
    "parent":               SystemFieldSpec("parent_key", None, "STR", True, False),
    "summary":              SystemFieldSpec("summary", None, "STR", False, False),
    "created":              SystemFieldSpec("created_at", None, "DATE", True, False),
    "updated":              SystemFieldSpec("updated_at", None, "DATE", True, False),
    "resolutiondate":       SystemFieldSpec("resolved_at", None, "DATE", True, False),
    "duedate":              SystemFieldSpec("due_date", None, "DATE", True, False),
    "first_done_at":        SystemFieldSpec("first_done_at", None, "DATE", True, False),
    "timeoriginalestimate": SystemFieldSpec("original_estimate_sec", None,
                                            "NUM", False, True),
    "timeestimate":         SystemFieldSpec("remaining_estimate_sec", None,
                                            "NUM", False, True),
    "timespent":            SystemFieldSpec("time_spent_sec", None, "NUM", False, True),
}

# /rest/api/2/field 에 없어서 sync_catalog가 직접 만들어 넣는 합성 필드 (spec §4.1)
SYNTHETIC_FIELDS: dict[str, str] = {
    "status_category": "Status Category",
    "first_done_at": "First Done At",
}


def value_kind_of(schema_type: str | None, schema_items: str | None) -> str:
    if schema_type == "array":
        return "MULTI"
    if schema_type == "number":
        return "NUM"
    if schema_type in ("date", "datetime"):
        return "DATE"
    return "STR"
