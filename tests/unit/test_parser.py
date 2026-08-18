from datetime import datetime, timezone

from jira_dashboard.jira.fieldmap import SYSTEM_FIELD_MAP, SystemFieldSpec, value_kind_of
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
    """tier 1(응답의 statusCategory.key)이 tier 2(category_of 사전)보다 우선해야 한다.
    두 값을 일부러 다르게 줘서 tier 1이 실제로 이겼는지 구별한다."""
    issue = parser.parse_issue(sample_issue, field_index, category_of={"완료": "indeterminate"})
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


def test_multi_value_system_field_guard_still_routes_to_custom_values(
    monkeypatch, sample_issue, field_index
):
    """방어적 가드: SYSTEM_FIELD_MAP에 다중값 필드가 들어와도 고정 컬럼이 아니라
    EAV로 가야 한다 (spec 4.1). 이 테스트는 parse_issue의
    `not _is_multi_value(fd)` conjunct를 지우면 실패해야 한다."""
    monkeypatch.setitem(
        SYSTEM_FIELD_MAP, "fake_multi",
        SystemFieldSpec("fake_multi_col", None, "MULTI", True, False),
    )
    field_index = dict(field_index)
    field_index["fake_multi"] = FieldDef(
        "fake_multi", "Fake Multi", False, "array", "string", None
    )
    sample_issue["fields"]["fake_multi"] = ["a", "b"]

    issue = parser.parse_issue(sample_issue, field_index, category_of={"완료": "done"})
    assert "fake_multi" in {v.field_id for v in issue.custom_values}


def test_no_system_field_map_entry_is_array_typed():
    """SYSTEM_FIELD_MAP에 다중값(MULTI) 항목이 없다는 불변식을 문서화한다.
    이게 지금 parse_issue의 다중값 가드가 죽은 코드처럼 보이는 이유다.
    누군가 (예: components) 다중값 필드를 매핑표에 추가하면 이 테스트가 실패해
    parse_issue의 `not _is_multi_value(fd)` 가드를 확인하라는 신호를 준다."""
    for field_id, spec in SYSTEM_FIELD_MAP.items():
        assert spec.value_kind != "MULTI", (
            f"{field_id} is array-typed now — verify parse_issue's multi-value "
            "guard is actually exercised by a test"
        )


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


# --- Important 5: EAV val_id도 100바이트에서 잘려야 한다 -------------------------
# R22가 TEST_ISSUE_FIELD_HISTORY.val_id(100 BYTE)를 고칠 때 같은 폭인 형제 컬럼
# TEST_ISSUE_FIELD_VALUE.val_id를 놓쳤다. 아래 세 테스트는
# tests/unit/test_derive_history.py::test_val_id_truncates_to_100_bytes의 EAV 쪽
# 쌍둥이다.

def test_option_id_truncates_to_100_bytes():
    raw = {"value": "Regression", "id": "1" * 200}
    vals = parser.extract_values("customfield_4", _fd("customfield_4", "option"), raw)
    assert len(vals[0].val_id.encode("utf-8")) <= 100


def test_user_key_truncates_to_100_bytes():
    """디렉터리 통합 계정의 key는 100바이트를 넘을 수 있다."""
    raw = {"key": "가" * 60, "name": "n", "displayName": "Jane Doe"}
    vals = parser.extract_values("customfield_5", _fd("customfield_5", "user"), raw)
    assert len(vals[0].val_id.encode("utf-8")) <= 100


def test_named_entity_id_truncates_to_100_bytes():
    raw = {"name": "Blocker", "id": "9" * 300}
    vals = parser.extract_values("priority", _fd("priority", "priority"), raw)
    assert len(vals[0].val_id.encode("utf-8")) <= 100


def test_multibyte_val_id_is_not_cut_mid_character():
    """바이트로 자르면서 UTF-8 시퀀스를 반토막 내면 안 된다."""
    raw = {"value": "v", "id": "한" * 100}
    vals = parser.extract_values("customfield_4", _fd("customfield_4", "option"), raw)
    assert vals[0].val_id == "한" * 33          # 33 * 3바이트 = 99바이트


# --- M17: summary는 문자가 아니라 바이트로 자른다 -------------------------------

def test_summary_truncates_by_bytes_not_characters(field_index, category_of,
                                                   sample_issue):
    """TEST_JIRA_ISSUE.summary는 VARCHAR2(1024 BYTE)다. 1000 "문자"로 자르면
    한글 요약이 최대 3000바이트가 되어 ORA-12899다 — 다른 모든 자리는 바이트
    기준인데 여기만 문자 기준이었다."""
    sample_issue["fields"]["summary"] = "요" * 1000
    parsed = parser.parse_issue(sample_issue, field_index, category_of)
    assert len(parsed.summary.encode("utf-8")) <= 1024
