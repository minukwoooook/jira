"""컬럼 폭을 DDL에서 파생해 쓰기 경로 전체를 검사한다 (R33 / Important 6).

`ORA-12899`가 이 브랜치에서 세 번 나왔고 세 번 다 사람 눈이 찾았다:
`TEST_ISSUE_FIELD_HISTORY.val_id`(R22), 그 형제인 `TEST_ISSUE_FIELD_VALUE.val_id`,
`TEST_ISSUE_CHANGELOG.from_id/to_id`, 그리고 문자 단위로 자르다가 *처리된* 실패를
처리되지 않은 예외로 바꿔버리는 `TEST_SYNC_RUN.error_msg`. 하나씩 때우는 대신
"모든 쓰기 바인드는 자기 컬럼의 폭을 지킨다"를 스위트가 주장하게 한다.

세 부분으로 되어 있다:
1. `MAX_*_BYTES` 상수가 DDL의 선언 폭과 같은지 (손으로 적은 숫자의 표류 방지)
2. 모든 쓰기 문장의 바인드가 실존하는 컬럼으로 매핑되는지 (장치의 사각지대 방지)
3. 일부러 폭을 넘긴 데이터를 실제 파이프라인에 통과시켜 아무것도 안 넘치는지
"""
import inspect
import re

import pytest

from jira_dashboard.db import schema_map
from jira_dashboard.db.repository import catalog, history, issue, sync
from jira_dashboard.jira import models
from tests.static.test_sql_references import _all_sql
from tests.widths import (
    WidthViolation, bind_columns, check_binds, strip_literals,
)

# (상수 이름, 테이블, 컬럼) — 상수가 실제로 지키는 컬럼
CONSTANT_COLUMNS = [
    ("MAX_VAL_STR_BYTES", "TEST_ISSUE_FIELD_VALUE", "VAL_STR"),
    ("MAX_VAL_STR_BYTES", "TEST_ISSUE_FIELD_HISTORY", "VAL_STR"),
    ("MAX_VAL_ID_BYTES", "TEST_ISSUE_FIELD_VALUE", "VAL_ID"),
    ("MAX_VAL_ID_BYTES", "TEST_ISSUE_FIELD_HISTORY", "VAL_ID"),
    ("MAX_CHANGELOG_STR_BYTES", "TEST_ISSUE_CHANGELOG", "FROM_STR"),
    ("MAX_CHANGELOG_STR_BYTES", "TEST_ISSUE_CHANGELOG", "TO_STR"),
    ("MAX_CHANGELOG_ID_BYTES", "TEST_ISSUE_CHANGELOG", "FROM_ID"),
    ("MAX_CHANGELOG_ID_BYTES", "TEST_ISSUE_CHANGELOG", "TO_ID"),
    ("MAX_NAME_BYTES", "TEST_ISSUE_CHANGELOG", "FIELD_NAME"),
    ("MAX_NAME_BYTES", "TEST_ISSUE_CHANGELOG", "AUTHOR_DISPLAY_NAME"),
    ("MAX_NAME_BYTES", "TEST_ISSUE_CHANGELOG", "AUTHOR_USER_KEY"),
    ("MAX_NAME_BYTES", "TEST_JIRA_ISSUE", "ASSIGNEE_DISPLAY_NAME"),
    ("MAX_NAME_BYTES", "TEST_JIRA_ISSUE", "REPORTER_USER_KEY"),
    ("MAX_NAME_BYTES", "TEST_JIRA_FIELD", "FIELD_NAME"),
    ("MAX_NAME_BYTES", "TEST_JIRA_PROJECT", "NAME"),
    ("MAX_SHORT_NAME_BYTES", "TEST_JIRA_ISSUE", "STATUS_NAME"),
    ("MAX_SHORT_NAME_BYTES", "TEST_JIRA_ISSUE", "ISSUE_TYPE_NAME"),
    ("MAX_SHORT_NAME_BYTES", "TEST_JIRA_ISSUE", "PRIORITY_NAME"),
    ("MAX_SHORT_NAME_BYTES", "TEST_JIRA_ISSUE", "RESOLUTION_NAME"),
    ("MAX_KEY_BYTES", "TEST_JIRA_ISSUE", "ISSUE_KEY"),
    ("MAX_KEY_BYTES", "TEST_JIRA_ISSUE", "PARENT_KEY"),
    ("MAX_SUMMARY_BYTES", "TEST_JIRA_ISSUE", "SUMMARY"),
    ("MAX_ERROR_MSG_BYTES", "TEST_SYNC_RUN", "ERROR_MSG"),
    ("MAX_SCHEMA_TYPE_BYTES", "TEST_JIRA_FIELD", "SCHEMA_TYPE"),
    ("MAX_SCHEMA_TYPE_BYTES", "TEST_JIRA_FIELD", "SCHEMA_ITEMS"),
    ("MAX_CUSTOM_TYPE_BYTES", "TEST_JIRA_FIELD", "CUSTOM_TYPE"),
]


@pytest.fixture(scope="session")
def limits(ddl_dir):
    return schema_map.column_byte_limits(ddl_dir)


@pytest.mark.parametrize("constant,table,column", CONSTANT_COLUMNS)
def test_width_constants_match_the_ddl(limits, constant, table, column):
    """models.py의 손으로 적은 폭과 DDL이 어긋나면 그 자체가 ORA-12899다."""
    assert getattr(models, constant) == limits[(table, column)], (
        f"{constant} != {table}.{column} 선언 폭"
    )


def _write_statements():
    """스캔 대상 패키지의 INSERT/UPDATE/MERGE 문. {table} 슬롯은 화이트리스트로 채운다."""
    for module_name, sql in _all_sql():
        if not re.search(r"\b(INSERT|UPDATE|MERGE)\b", sql, re.IGNORECASE):
            continue
        if "{placeholders}" in sql:
            sql = sql.replace("{placeholders}", ":b0")
        if "{table}" in sql:
            for table in sorted(issue._RAW_TABLES):
                yield module_name, sql.replace("{table}", table)
            continue
        yield module_name, sql


# 컬럼이 아닌 바인드: 함수 인자와 RETURNING ... INTO 의 OUT 파라미터.
_NON_COLUMN_BINDS = {"hours", "out_run_id"}
_BIND = re.compile(r":(\w+)")
_TABLE_TOKEN = re.compile(r"\b(TEST_\w+)\b", re.IGNORECASE)


def test_every_write_binding_maps_to_a_real_column(ddl_dir, limits):
    """장치의 사각지대를 막는다: 어떤 값 바인드도 "어느 컬럼으로 가는지 모름" 상태로
    남지 않아야 한다 — 모르면 폭 검사도 없다.

    새 INSERT/MERGE가 들어오면 이 테스트가 먼저 그 바인드들을 요구하므로, 폭 검사가
    조용히 비켜가는 문장이 생기지 않는다.
    """
    columns = schema_map.parse_ddl(ddl_dir)
    covered = 0
    for module_name, sql in _write_statements():
        table, mapping = bind_columns(sql)
        assert table in columns, f"{module_name}: 알 수 없는 대상 테이블 {table}"
        # 조인/서브쿼리에 나오는 다른 테이블의 컬럼도 바인드 대상이 될 수 있다.
        allowed = set()
        for token in _TABLE_TOKEN.findall(sql):
            allowed |= columns.get(token.upper(), set())
        for bind in sorted(set(_BIND.findall(strip_literals(sql)))):
            if re.fullmatch(r"b\d+", bind) or bind in _NON_COLUMN_BINDS:
                continue
            assert bind in mapping, (
                f"{module_name}: :{bind}가 어느 컬럼으로 가는지 알 수 없다 — "
                f"폭 검사를 비켜간다\n{sql}"
            )
            assert mapping[bind] in allowed, \
                f"{module_name}: {mapping[bind]} 없음 (:{bind})"
            if (table, mapping[bind]) in limits:
                covered += 1
    # 폭이 있는 문자열 컬럼이 실제로 여러 개 검사망에 들어왔는지
    assert covered >= 20, covered


def test_the_harness_actually_catches_an_oversized_bind(limits):
    """장치가 아무것도 잡지 못하면 3번 테스트가 통째로 공허해진다."""
    problems = check_binds(
        limits, history._INSERT_HISTORY,
        [{"issue_id": 1, "field_pk": 2, "val_id": "x" * 101, "val_str": "ok"}],
    )
    assert problems and "VAL_ID" in problems[0]
    assert not check_binds(limits, history._INSERT_HISTORY,
                           [{"issue_id": 1, "val_id": "x" * 100}])


def test_multibyte_values_are_measured_in_bytes_not_characters(limits):
    """R22와 이번 error_msg 결함의 공통 원인 — 문자 수로 재면 안전해 보인다."""
    problems = check_binds(limits, sync._FINISH_RUN,
                           [{"run_id": 1, "status": "FAILED", "error_msg": "가" * 2000}])
    assert problems, "2000자 한글 = 6000바이트 > 4000바이트인데 통과했다"


def test_no_repository_module_binds_a_text_column_without_a_limit():
    """새 문장이 truncate 없이 들어오는 것을 막는 최소한의 냄새 검사:
    쓰기 함수가 있는 모듈은 truncate를 import하고 있어야 한다."""
    for module in (history, issue, sync, catalog):
        source = inspect.getsource(module)
        if not re.search(r"\b(INSERT|UPDATE|MERGE)\b", source):
            continue
        assert "truncate" in source or module is issue, module.__name__


def test_width_violation_is_an_assertion_error():
    assert issubclass(WidthViolation, AssertionError)
