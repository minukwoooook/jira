import json
from collections.abc import Mapping
from datetime import date, datetime, timezone

from jira_dashboard.jira.fieldmap import SYSTEM_FIELD_MAP
from jira_dashboard.jira.models import (
    MAX_NAME_BYTES, MAX_SHORT_NAME_BYTES, MAX_SUMMARY_BYTES, MAX_VAL_ID_BYTES,
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
        # val_id도 잘라야 한다 — TEST_ISSUE_FIELD_VALUE.val_id는 VARCHAR2(100 BYTE)다.
        # R22가 TEST_ISSUE_FIELD_HISTORY의 같은 폭 컬럼을 고칠 때 이 형제를 놓쳤다.
        # 사용자 key/name은 100바이트를 넘을 수 있고(디렉터리 통합 계정), 옵션 id는
        # 짧지만 여기서 규칙을 예외 없이 적용하는 편이 낫다.
        if t == "user" or "displayName" in raw:
            return FieldValue(field_id, seq, truncate(raw.get("displayName")),
                              None, None,
                              truncate(raw.get("key") or raw.get("name"),
                                       MAX_VAL_ID_BYTES))
        if t in _VALUE_TYPES or "value" in raw:
            return FieldValue(field_id, seq, truncate(raw.get("value")),
                              None, None, truncate(raw.get("id"), MAX_VAL_ID_BYTES))
        if t in _NAME_TYPES or "name" in raw:
            return FieldValue(field_id, seq, truncate(raw.get("name")),
                              None, None, truncate(raw.get("id"), MAX_VAL_ID_BYTES))
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
        # 고정 컬럼도 폭이 있다. 이슈 키/부모 키 같은 식별자는 일부러 자르지
        # 않는다 — 잘린 식별자는 조용히 잘못된 데이터가 되므로 ORA-12899로
        # 터지는 편이 옳다. 이름/표시명은 잘라도 의미가 보존된다.
        issue_type_name=truncate(_named(f.get("issuetype")), MAX_SHORT_NAME_BYTES),
        status_name=truncate(status_name, MAX_SHORT_NAME_BYTES),
        status_category=category,
        priority_name=truncate(_named(f.get("priority")), MAX_SHORT_NAME_BYTES),
        resolution_name=truncate(_named(f.get("resolution")), MAX_SHORT_NAME_BYTES),
        assignee_user_key=truncate(assignee.get("key") or assignee.get("name"),
                                   MAX_NAME_BYTES),
        assignee_display_name=truncate(assignee.get("displayName"), MAX_NAME_BYTES),
        reporter_user_key=truncate(reporter.get("key") or reporter.get("name"),
                                   MAX_NAME_BYTES),
        reporter_display_name=truncate(reporter.get("displayName"), MAX_NAME_BYTES),
        parent_key=parent.get("key"),
        # M17: 1000 "문자"로 자르던 것을 1024 "바이트"로 바꾼다. Jira가 summary를
        # 255자로 제한한다는 전제 덕분에 우연히 안전했을 뿐, 다른 곳은 전부 바이트
        # 기준인데 여기만 문자 기준이었다 (한글 1000자 = 3000바이트).
        summary=truncate(f.get("summary"), MAX_SUMMARY_BYTES) or None,
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
