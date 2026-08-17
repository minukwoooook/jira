import json
from collections.abc import Mapping
from datetime import date, datetime, timezone

from jira_dashboard.jira.fieldmap import SYSTEM_FIELD_MAP
from jira_dashboard.jira.models import (
    MAX_VAL_STR_BYTES, ChangelogItem, FieldDef, FieldValue, ParsedIssue,
)

_VALUE_TYPES = {"option", "option-with-child"}
_NAME_TYPES = {"priority", "status", "resolution", "issuetype", "version", "component"}


def to_utc(text: str | None) -> datetime | None:
    if not text:
        return None
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def to_date(text: str | None) -> date | None:
    return date.fromisoformat(text) if text else None


def truncate(text: str | None, max_bytes: int = MAX_VAL_STR_BYTES) -> str | None:
    if text is None:
        return None
    text = text.strip()
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def parse_field_defs(raw: list[dict]) -> list[FieldDef]:
    out = []
    for f in raw:
        schema = f.get("schema") or {}
        out.append(FieldDef(
            field_id=f["id"],
            field_name=f.get("name") or f["id"],
            is_custom=bool(f.get("custom", False)),
            schema_type=schema.get("type"),
            schema_items=schema.get("items"),
            custom_type=schema.get("custom"),
        ))
    return out


def _scalar(field_id: str, seq: int, fd: FieldDef, raw) -> FieldValue | None:
    if raw is None:
        return None
    t = fd.schema_items if fd.schema_type == "array" else fd.schema_type

    if t == "number":
        return FieldValue(field_id, seq, None, float(raw), None, None)
    if t == "date":
        d = to_date(raw)
        return FieldValue(field_id, seq, None, None,
                          datetime(d.year, d.month, d.day, tzinfo=timezone.utc), None)
    if t == "datetime":
        return FieldValue(field_id, seq, None, None, to_utc(raw), None)
    if isinstance(raw, dict):
        if t == "user" or "displayName" in raw:
            return FieldValue(field_id, seq, truncate(raw.get("displayName")),
                              None, None, raw.get("key") or raw.get("name"))
        if t in _VALUE_TYPES or "value" in raw:
            return FieldValue(field_id, seq, truncate(raw.get("value")),
                              None, None, raw.get("id"))
        if t in _NAME_TYPES or "name" in raw:
            return FieldValue(field_id, seq, truncate(raw.get("name")),
                              None, None, raw.get("id"))
        return FieldValue(field_id, seq,
                          truncate(json.dumps(raw, ensure_ascii=False)),
                          None, None, None)
    return FieldValue(field_id, seq, truncate(str(raw)), None, None, None)


def extract_values(field_id: str, fd: FieldDef, raw) -> list[FieldValue]:
    if raw is None:
        return []
    if fd.schema_type == "array":
        if not isinstance(raw, list):
            return []
        out = []
        for i, element in enumerate(raw):
            v = _scalar(field_id, i, fd, element)
            if v is not None:
                out.append(v)
        return out
    v = _scalar(field_id, 0, fd, raw)
    return [v] if v is not None else []


def parse_changelog(raw_histories: list[dict]) -> list[ChangelogItem]:
    out = []
    for h in raw_histories:
        author = h.get("author") or {}
        changed_at = to_utc(h["created"])
        for seq, item in enumerate(h.get("items") or []):
            out.append(ChangelogItem(
                history_id=str(h["id"]),
                item_seq=seq,
                author_user_key=author.get("key") or author.get("name"),
                author_display_name=author.get("displayName"),
                changed_at=changed_at,
                field_name=item.get("field") or "",
                field_id=item.get("fieldId"),
                from_id=item.get("from"),
                from_str=item.get("fromString"),
                to_id=item.get("to"),
                to_str=item.get("toString"),
            ))
    return out


def _named(obj) -> str | None:
    return obj.get("name") if isinstance(obj, dict) else None


def _is_multi_value(fd: FieldDef) -> bool:
    return fd.schema_type == "array"


def parse_issue(
    raw: dict,
    field_index: Mapping[str, FieldDef],
    category_of: Mapping[str, str],
) -> ParsedIssue:
    f = raw["fields"]
    status = f.get("status") or {}
    status_name = status.get("name")
    # A9: statusCategory.key 우선, 없으면 /status 사전으로 폴백
    category = ((status.get("statusCategory") or {}).get("key")
                or category_of.get(status_name or "")
                or "undefined")
    assignee = f.get("assignee") or {}
    reporter = f.get("reporter") or {}
    parent = f.get("parent") or {}
    changelog = raw.get("changelog") or {}

    custom: list[FieldValue] = []
    for field_id, value in f.items():
        fd = field_index.get(field_id)
        if fd is None:
            continue
        # 고정 컬럼으로 가는 시스템 필드는 EAV에 넣지 않는다.
        # 단 다중값이면 고정 컬럼에 담을 수 없으므로 EAV로 보낸다 (spec §4.1).
        if field_id in SYSTEM_FIELD_MAP and not _is_multi_value(fd):
            continue
        custom.extend(extract_values(field_id, fd, value))

    return ParsedIssue(
        jira_issue_id=str(raw["id"]),
        issue_key=raw["key"],
        project_jira_id=str((f.get("project") or {})["id"]),
        issue_type_name=_named(f.get("issuetype")),
        status_name=status_name,
        status_category=category,
        priority_name=_named(f.get("priority")),
        resolution_name=_named(f.get("resolution")),
        assignee_user_key=assignee.get("key") or assignee.get("name"),
        assignee_display_name=assignee.get("displayName"),
        reporter_user_key=reporter.get("key") or reporter.get("name"),
        reporter_display_name=reporter.get("displayName"),
        parent_key=parent.get("key"),
        summary=(f.get("summary") or "")[:1000] or None,
        created_at=to_utc(f["created"]),
        updated_at=to_utc(f["updated"]),
        resolved_at=to_utc(f.get("resolutiondate")),
        due_date=to_date(f.get("duedate")),
        original_estimate_sec=f.get("timeoriginalestimate"),
        remaining_estimate_sec=f.get("timeestimate"),
        time_spent_sec=f.get("timespent"),
        custom_values=tuple(custom),
        changelog=tuple(parse_changelog(changelog.get("histories") or [])),
        changelog_total=int(changelog.get("total", 0)),
    )
