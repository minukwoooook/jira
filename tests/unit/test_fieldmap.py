"""SYSTEM_FIELD_MAP의 손으로 적은 선언과 다른 출처가 일치하는지 대조한다 (T6).

세 개의 독립적인 진술이 있는데 서로 맞대본 적이 없었다:
1. `SYSTEM_FIELD_MAP[field_id].value_kind` — 손으로 적은 값 종류
2. `value_kind_of(schema_type, schema_items)` — Jira 스키마에서 계산한 값 종류
3. `COLUMN_FIELDS` — 그 매핑에서 뽑아 런타임에 SQL로 조립하는 (field_id, 컬럼)

3번이 특히 취약하다. 정적 SQL 게이트는 조립 *결과*를 보지 못하고 f-string의
`{counts}`/`{distincts}` 슬롯만 화이트리스트로 확인하므로, 컬럼 이름이 DDL에
없어도 게이트는 공허하게 통과한다 — ORA-00904는 런북 9단계에서야 나온다.
"""
from pathlib import Path

import pytest

from jira_dashboard.db import schema_map
from jira_dashboard.jira.fieldmap import (
    SYNTHETIC_FIELDS, SYSTEM_FIELD_MAP, value_kind_of,
)
from jira_dashboard.pipeline.profile_fields import COLUMN_FIELDS
from jira_dashboard.pipeline.sync_catalog import _SYNTHETIC_SCHEMA

DDL_DIR = Path(schema_map.__file__).parent / "ddl"

# Jira DC의 /rest/api/2/field 가 각 시스템 필드에 대해 주는 schema.type
# (합성 필드는 sync_catalog._SYNTHETIC_SCHEMA가 출처다). 픽스처에 있는 필드는
# 아래 test_fixture_schema_types_agree가 픽스처와도 대조한다.
JIRA_SCHEMA_TYPES: dict[str, tuple[str, str | None]] = {
    "issuetype":            ("issuetype", None),
    "status":               ("status", None),
    "priority":             ("priority", None),
    "resolution":           ("resolution", None),
    "assignee":             ("user", None),
    "reporter":             ("user", None),
    "parent":               ("issuelink", None),
    "summary":              ("string", None),
    "created":              ("datetime", None),
    "updated":              ("datetime", None),
    "resolutiondate":       ("datetime", None),
    "duedate":              ("date", None),
    "timeoriginalestimate": ("number", None),
    "timeestimate":         ("number", None),
    "timespent":            ("number", None),
}
JIRA_SCHEMA_TYPES.update({fid: (t, None) for fid, t in _SYNTHETIC_SCHEMA.items()})


def test_every_system_field_has_a_declared_schema_type():
    """새 시스템 필드를 매핑에 추가하면 스키마 타입도 함께 선언하게 만든다 —
    안 그러면 아래 일치 검사가 그 필드를 조용히 건너뛴다."""
    assert set(JIRA_SCHEMA_TYPES) == set(SYSTEM_FIELD_MAP)


@pytest.mark.parametrize("field_id", sorted(SYSTEM_FIELD_MAP))
def test_declared_value_kind_agrees_with_value_kind_of(field_id):
    """T6: 손으로 적은 value_kind와 계산된 value_kind가 어긋나면, 같은 필드가
    카탈로그에서는 DATE인데 쿼리에서는 STR로 취급되는 식으로 갈라진다."""
    schema_type, schema_items = JIRA_SCHEMA_TYPES[field_id]
    assert value_kind_of(schema_type, schema_items) == \
        SYSTEM_FIELD_MAP[field_id].value_kind


def test_fixture_schema_types_agree_with_the_declared_table(fake_jira):
    """선언한 스키마 타입이 픽스처(= A1~A12 가정의 구현)와도 맞는지 본다."""
    for f in fake_jira.get_fields():
        if f["id"] not in JIRA_SCHEMA_TYPES:
            continue
        schema = f.get("schema") or {}
        assert (schema.get("type"), schema.get("items")) == \
            JIRA_SCHEMA_TYPES[f["id"]], f["id"]


def test_synthetic_fields_are_declared_in_both_places():
    assert set(SYNTHETIC_FIELDS) == set(_SYNTHETIC_SCHEMA)
    assert set(SYNTHETIC_FIELDS) <= set(SYSTEM_FIELD_MAP)


def test_column_fields_reference_real_columns_of_the_issue_table():
    """런타임에 SQL로 조립되는 컬럼 이름이 실제로 TEST_JIRA_ISSUE에 있는지 —
    정적 게이트가 볼 수 없는 자리다."""
    columns = schema_map.parse_ddl(DDL_DIR)["TEST_JIRA_ISSUE"]
    for field_id, column in COLUMN_FIELDS:
        assert column.upper() in columns, f"{field_id} → {column}"


def test_label_columns_also_reference_real_columns():
    columns = schema_map.parse_ddl(DDL_DIR)["TEST_JIRA_ISSUE"]
    for field_id, spec in SYSTEM_FIELD_MAP.items():
        if spec.label_column_name:
            assert spec.label_column_name.upper() in columns, field_id


def test_multi_valued_fields_never_claim_a_fixed_column():
    """다중값은 고정 컬럼에 담을 수 없다 — MULTI가 매핑에 있으면 storage_for가
    EAV로 보내면서 column_name을 버려 CK 제약(test_ck_jira_field_col)과 싸운다."""
    for field_id, spec in SYSTEM_FIELD_MAP.items():
        assert spec.value_kind != "MULTI", field_id
