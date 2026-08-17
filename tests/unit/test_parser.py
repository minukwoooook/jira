from datetime import datetime, timezone

from jira_dashboard.jira.fieldmap import SYSTEM_FIELD_MAP, value_kind_of
from jira_dashboard.jira.models import FieldDef
from jira_dashboard.jira import parser


def _fd(field_id, schema_type, items=None, custom=None):
    return FieldDef(field_id, "F", True, schema_type, items, custom)


def test_to_utc_converts_offset_to_utc():
    assert parser.to_utc("2026-05-01T09:00:00.000+0900") == datetime(
        2026, 5, 1, 0, 0, tzinfo=timezone.utc
    )


def test_to_utc_handles_none():
    assert parser.to_utc(None) is None


def test_string_value_is_stripped():
    vals = parser.extract_values("customfield_1", _fd("customfield_1", "string"), "  hi  ")
    assert (vals[0].val_str, vals[0].val_num, vals[0].val_id) == ("hi", None, None)


def test_string_is_truncated_to_1000_bytes():
    vals = parser.extract_values("customfield_1", _fd("customfield_1", "string"), "가" * 500)
    assert len(vals[0].val_str.encode("utf-8")) <= 1000


def test_number_value():
    vals = parser.extract_values("customfield_2", _fd("customfield_2", "number"), 3.5)
    assert (vals[0].val_num, vals[0].val_str) == (3.5, None)


def test_date_value_is_midnight_utc():
    vals = parser.extract_values("customfield_3", _fd("customfield_3", "date"), "2026-05-01")
    assert vals[0].val_date == datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)


def test_datetime_value_is_converted_to_utc():
    vals = parser.extract_values(
        "customfield_3", _fd("customfield_3", "datetime"), "2026-05-01T09:00:00.000+0900"
    )
    assert vals[0].val_date == datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)


def test_option_keeps_id_and_value():
    raw = {"value": "Regression", "id": "10100"}
    vals = parser.extract_values("customfield_4", _fd("customfield_4", "option"), raw)
    assert (vals[0].val_str, vals[0].val_id) == ("Regression", "10100")


def test_user_uses_display_name_and_key():
    raw = {"key": "jdoe", "name": "jdoe", "displayName": "Jane Doe"}
    vals = parser.extract_values("customfield_5", _fd("customfield_5", "user"), raw)
    assert (vals[0].val_str, vals[0].val_id) == ("Jane Doe", "jdoe")


def test_named_entity_uses_name():
    raw = {"name": "Blocker", "id": "1"}
    vals = parser.extract_values("priority", _fd("priority", "priority"), raw)
    assert (vals[0].val_str, vals[0].val_id) == ("Blocker", "1")


def test_array_produces_one_row_per_element_with_seq():
    raw = [{"value": "A", "id": "1"}, {"value": "B", "id": "2"}]
    fd = _fd("customfield_6", "array", items="option")
    vals = parser.extract_values("customfield_6", fd, raw)
    assert [(v.val_seq, v.val_str, v.val_id) for v in vals] == [
        (0, "A", "1"), (1, "B", "2")
    ]


def test_array_of_plain_strings():
    fd = _fd("labels", "array", items="string")
    vals = parser.extract_values("labels", fd, ["urgent", "ux"])
    assert [(v.val_seq, v.val_str) for v in vals] == [(0, "urgent"), (1, "ux")]


def test_empty_array_produces_no_rows():
    fd = _fd("labels", "array", items="string")
    assert parser.extract_values("labels", fd, []) == []


def test_null_value_produces_no_rows():
    assert parser.extract_values("customfield_1", _fd("customfield_1", "string"), None) == []


def test_unknown_plugin_type_falls_back_to_json_string():
    fd = _fd("customfield_9", "any", custom="com.example:weird")
    vals = parser.extract_values("customfield_9", fd, {"a": 1})
    assert vals[0].val_str == '{"a": 1}'


def test_value_kind_of():
    assert value_kind_of("number", None) == "NUM"
    assert value_kind_of("date", None) == "DATE"
    assert value_kind_of("datetime", None) == "DATE"
    assert value_kind_of("array", "option") == "MULTI"
    assert value_kind_of("option", None) == "STR"
    assert value_kind_of("string", None) == "STR"


def test_assignee_maps_to_user_key_with_display_label():
    """spec 4.1: 동명이인이 합쳐지지 않도록 그룹핑 키와 라벨을 분리한다."""
    spec = SYSTEM_FIELD_MAP["assignee"]
    assert spec.column_name == "assignee_user_key"
    assert spec.label_column_name == "assignee_display_name"


def test_summary_is_not_a_dimension():
    assert SYSTEM_FIELD_MAP["summary"].is_dimension is False


def test_parse_issue_stores_status_category_key(sample_issue, field_index):
    issue = parser.parse_issue(sample_issue, field_index, category_of={"완료": "done"})
    assert issue.status_category == "done"
    assert issue.status_name == "완료"


def test_parse_issue_falls_back_to_category_map(sample_issue, field_index):
    """응답에 statusCategory가 없으면 /status로 만든 사전을 쓴다."""
    sample_issue["fields"]["status"].pop("statusCategory", None)
    issue = parser.parse_issue(sample_issue, field_index, category_of={"완료": "done"})
    assert issue.status_category == "done"


def test_parse_issue_uses_undefined_for_unknown_status(sample_issue, field_index):
    sample_issue["fields"]["status"].pop("statusCategory", None)
    issue = parser.parse_issue(sample_issue, field_index, category_of={})
    assert issue.status_category == "undefined"


def test_parse_issue_excludes_system_fields_from_custom_values(sample_issue, field_index):
    issue = parser.parse_issue(sample_issue, field_index, category_of={"완료": "done"})
    custom_ids = {v.field_id for v in issue.custom_values}
    assert "summary" not in custom_ids
    assert "status" not in custom_ids


def test_parse_issue_includes_multi_value_system_fields_as_custom(sample_issue, field_index):
    """labels는 시스템 필드지만 다중값이라 EAV로 간다 (spec 4.1)."""
    issue = parser.parse_issue(sample_issue, field_index, category_of={"완료": "done"})
    assert "labels" in {v.field_id for v in issue.custom_values}


def test_parse_issue_reports_changelog_total(sample_issue, field_index):
    issue = parser.parse_issue(sample_issue, field_index, category_of={"완료": "done"})
    assert issue.changelog_total >= len(issue.changelog)


def test_parse_changelog_assigns_item_seq_within_history():
    raw = [{
        "id": "1001", "created": "2026-05-01T09:00:00.000+0900",
        "author": {"key": "jdoe", "displayName": "Jane"},
        "items": [
            {"field": "status", "fieldId": "status",
             "fromString": "To Do", "toString": "완료"},
            {"field": "resolution", "fieldId": "resolution",
             "fromString": None, "toString": "Done"},
        ],
    }]
    items = parser.parse_changelog(raw)
    assert [(i.history_id, i.item_seq, i.field_id) for i in items] == [
        ("1001", 0, "status"), ("1001", 1, "resolution")
    ]


def test_parse_changelog_keeps_field_id_none_when_absent():
    """A5가 거짓인 경우. field_pk 매칭은 이름으로 하되 모호하면 NULL이다."""
    raw = [{
        "id": "1", "created": "2026-05-01T09:00:00.000+0900", "author": {},
        "items": [{"field": "Link", "toString": "blocks ABC-1"}],
    }]
    assert parser.parse_changelog(raw)[0].field_id is None
