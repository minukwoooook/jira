from datetime import datetime, timezone
from pathlib import Path

import pytest

from jira_dashboard.doctor.db_checks import run_db_checks


@pytest.fixture(scope="session")
def ddl_dir() -> Path:
    from jira_dashboard.db import schema_map
    return Path(schema_map.__file__).parent / "ddl"


class FakeCursor:
    def __init__(self, answers): self._answers, self._rows = answers, []
    def execute(self, sql, **binds):
        for key, rows in self._answers.items():
            if key in sql:
                self._rows = rows
                return
        self._rows = []
    def fetchone(self): return self._rows[0] if self._rows else None
    def fetchall(self): return self._rows


class FakeConn:
    def __init__(self, answers): self._answers = answers
    def cursor(self): return FakeCursor(self._answers)


# DB8 왕복 검사 기본값: 바인드한 그대로(오프셋 변환 없이) 되돌아온 것처럼 보이는
# naive datetime들. 실제 Oracle이 오프셋을 변환해버리면 이 값과 달라져야 FAIL이 된다.
_PROBE_INSTANT = datetime(2020, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
_SENTINEL = datetime(9999, 12, 31, tzinfo=timezone.utc)
_ROUNDTRIP_OK = [(
    _PROBE_INSTANT.replace(tzinfo=None),
    _SENTINEL.replace(tzinfo=None),
    _SENTINEL.replace(tzinfo=None),
)]


def _conn(**overrides):
    answers = {
        "banner_full": [("Oracle Database 19c Enterprise Edition",)],
        "max_string_size": [("STANDARD",)],
        "v$timezone_names": [(1,)],
        "NLS_CHARACTERSET": [("AL32UTF8",)],
        "db_block_size": [("8192",)],
        "user_sys_privs": [("CREATE TABLE",), ("CREATE SEQUENCE",), ("CREATE VIEW",)],
        "user_tables": [(16,)],
        "roundtrip_probe": _ROUNDTRIP_OK,
    }
    answers.update(overrides)
    return FakeConn(answers)


def _by_id(results): return {r.id: r for r in results}


def test_passes_on_19c():
    r = _by_id(run_db_checks(_conn(), skip_schema=True))
    assert r["DB1"].verdict == "PASS"


def test_fails_on_23ai():
    conn = _conn(banner_full=[("Oracle Database 23ai Free",)])
    r = _by_id(run_db_checks(conn, skip_schema=True))
    assert r["DB1"].verdict == "FAIL"


def test_warns_on_extended_string_size():
    conn = _conn(max_string_size=[("EXTENDED",)])
    r = _by_id(run_db_checks(conn, skip_schema=True))
    assert r["DB2"].verdict == "WARN"


def test_fails_when_seoul_timezone_missing():
    r = _by_id(run_db_checks(_conn(**{"v$timezone_names": [(0,)]}), skip_schema=True))
    assert r["DB3"].verdict == "FAIL"


def test_fails_on_missing_ddl_privileges():
    conn = _conn(user_sys_privs=[("CREATE TABLE",)])
    r = _by_id(run_db_checks(conn, skip_schema=True))
    assert r["DB5"].verdict == "FAIL"
    assert "CREATE SEQUENCE" in r["DB5"].impact


def test_skip_schema_omits_the_schema_check():
    ids = {r.id for r in run_db_checks(_conn(), skip_schema=True)}
    assert "DB7" not in ids


# --- DB7: 이름만이 아니라 폭·인덱스·제약·뷰·시퀀스까지 (R33) -------------------
# DB7은 손으로 적용한 DDL이 실제로 들어갔는지 확인하는 유일한 자동 검사이고, 런북
# 4단계는 그걸 근거로 "스키마 일치"를 주장한다. 컬럼 이름만 보면 인덱스 하나가
# 빠지거나 폭이 다른 것을 통과시킨다.

def _schema_answers(ddl_dir, **overrides):
    """실제 DDL에서 만든 "완벽히 일치하는 데이터 딕셔너리" 응답."""
    from jira_dashboard.db import schema_map

    rows = []
    for table, columns in schema_map.parse_columns(ddl_dir).items():
        for column in columns.values():
            # NUMBER/TIMESTAMP의 data_length는 내부 표현 크기다 — DB7은 무시해야 한다.
            length = column.byte_length if column.byte_length is not None else 22
            rows.append((table, column.name, column.data_type, length))
    answers = {
        "user_tab_columns": rows,
        "user_indexes": [(n,) for n in sorted(schema_map.parse_indexes(ddl_dir))],
        "user_constraints": [(n,) for n in
                             sorted(schema_map.parse_constraints(ddl_dir))],
        "user_views": [(n,) for n in sorted(schema_map.parse_views(ddl_dir))],
        "user_sequences": [(n,) for n in sorted(schema_map.parse_sequences(ddl_dir))],
    }
    answers.update(overrides)
    return answers


def test_schema_check_reports_missing_tables(ddl_dir):
    """DDL을 아직 안 돌렸으면 FAIL이어야 한다 — 런북 4단계의 게이트."""
    r = _by_id(run_db_checks(_conn(**_schema_answers(ddl_dir, user_tab_columns=[]))))
    assert r["DB7"].verdict == "FAIL"


def test_schema_check_passes_when_actual_matches_real_ddl(ddl_dir):
    """DB7이 비교하는 두 쪽(파싱된 DDL, 실제 데이터 딕셔너리)이 정확히 일치하면
    PASS여야 한다. 정적 게이트는 doctor를 스캔하지 않으므로 이 테스트가 유일한
    검증이다."""
    r = _by_id(run_db_checks(_conn(**_schema_answers(ddl_dir))))
    assert r["DB7"].verdict == "PASS", r["DB7"].observed
    for label in ("tables", "indexes", "constraints", "views", "sequences"):
        assert label in r["DB7"].observed


def test_schema_check_reports_a_missing_index(ddl_dir):
    """인덱스 하나가 빠져도 컬럼 이름 대조는 통과한다 — 그게 R33의 요지다."""
    answers = _schema_answers(ddl_dir)
    dropped = answers["user_indexes"][0][0]
    answers["user_indexes"] = answers["user_indexes"][1:]
    r = _by_id(run_db_checks(_conn(**answers)))
    assert r["DB7"].verdict == "FAIL"
    assert dropped in r["DB7"].observed


def test_schema_check_reports_a_missing_view(ddl_dir):
    answers = _schema_answers(ddl_dir, user_views=[])
    r = _by_id(run_db_checks(_conn(**answers)))
    assert r["DB7"].verdict == "FAIL"
    assert "TEST_V_UNIFY_CANDIDATE" in r["DB7"].observed


def test_schema_check_reports_a_missing_sequence(ddl_dir):
    """시퀀스가 없으면 next_issue_ids가 9단계에서 즉시 죽는다."""
    answers = _schema_answers(ddl_dir, user_sequences=[])
    r = _by_id(run_db_checks(_conn(**answers)))
    assert r["DB7"].verdict == "FAIL"
    assert "TEST_SEQ_ISSUE_ID" in r["DB7"].observed


def test_schema_check_reports_a_missing_constraint(ddl_dir):
    answers = _schema_answers(ddl_dir)
    dropped = answers["user_constraints"][0][0]
    answers["user_constraints"] = answers["user_constraints"][1:]
    r = _by_id(run_db_checks(_conn(**answers)))
    assert r["DB7"].verdict == "FAIL"
    assert dropped in r["DB7"].observed


def test_schema_check_reports_a_wrong_column_width(ddl_dir):
    """폭이 다르면 코드의 truncate 상한과 어긋난 것이므로 ORA-12899가 예정돼 있다."""
    answers = _schema_answers(ddl_dir)
    answers["user_tab_columns"] = [
        (t, c, dt, 40 if (t, c) == ("TEST_ISSUE_CHANGELOG", "FROM_ID") else n)
        for t, c, dt, n in answers["user_tab_columns"]
    ]
    r = _by_id(run_db_checks(_conn(**answers)))
    assert r["DB7"].verdict == "FAIL"
    assert "FROM_ID width 40" in r["DB7"].observed


def test_schema_check_ignores_number_and_timestamp_lengths(ddl_dir):
    """NUMBER(12)의 data_length는 22다 — 폭 대조에 쓰면 전부 FAIL이 된다."""
    answers = _schema_answers(ddl_dir)
    answers["user_tab_columns"] = [
        (t, c, dt, 11 if dt in ("NUMBER", "TIMESTAMP", "BLOB", "DATE") else n)
        for t, c, dt, n in answers["user_tab_columns"]
    ]
    r = _by_id(run_db_checks(_conn(**answers)))
    assert r["DB7"].verdict == "PASS", r["DB7"].observed


def test_every_result_carries_impact_text():
    """FAIL일 때 무엇을 고쳐야 하는지 알려주지 않으면 도구가 아니다."""
    for result in run_db_checks(_conn(), skip_schema=True):
        assert result.impact, result.id


# --- Addition 1: DB8, TIMESTAMP 바인드 왕복 실측 -----------------------------
# 이전 작업에서 Oracle TIMESTAMP 컬럼이 naive datetime으로 돌아온다는 건 확인했지만,
# "바인드" 방향(aware datetime을 naive 컬럼에 넣을 때 oracledb가 오프셋을 잘라내는지,
# 아니면 변환해버리는지)은 추론만 했지 실측한 적이 없다. 변환해버리면 저장되는 모든
# 타임스탬프가 조용히 밀린다.

def test_db8_passes_when_bind_is_truncated_not_converted():
    r = _by_id(run_db_checks(_conn(), skip_schema=True))
    assert r["DB8"].verdict == "PASS"
    # 관찰값 자체가 보여야 한다 — 디버깅 시 bare PASS보다 훨씬 유용하다.
    assert "2020-03-04" in r["DB8"].observed
    assert "9999-12-31" in r["DB8"].observed


def test_db8_fails_when_probe_instant_comes_back_shifted():
    """드라이버가 세션 타임존(예: +09:00)으로 변환해버리는 상황을 흉내낸다."""
    shifted = _PROBE_INSTANT.replace(tzinfo=None).replace(hour=14)  # +9h 변환된 것처럼
    conn = _conn(roundtrip_probe=[(shifted, _SENTINEL.replace(tzinfo=None),
                                   _SENTINEL.replace(tzinfo=None))])
    r = _by_id(run_db_checks(conn, skip_schema=True))
    assert r["DB8"].verdict == "FAIL"


def test_db8_fails_when_bound_sentinel_disagrees_with_ddl_literal():
    """valid_to DEFAULT의 리터럴과 바인드한 SENTINEL이 다른 값으로 저장되면
    asof 조회가 조용히 행을 놓친다 — 반드시 FAIL이어야 한다."""
    disagreeing_sentinel = datetime(9999, 12, 31, 9, 0, 0)  # 리터럴과 다른 시각
    conn = _conn(roundtrip_probe=[(_PROBE_INSTANT.replace(tzinfo=None),
                                   disagreeing_sentinel,
                                   _SENTINEL.replace(tzinfo=None))])
    r = _by_id(run_db_checks(conn, skip_schema=True))
    assert r["DB8"].verdict == "FAIL"


def test_db8_impact_is_meaningful():
    r = _by_id(run_db_checks(_conn(), skip_schema=True))
    assert r["DB8"].impact


# --- R34: db_checks도 같은 규칙을 따른다 ----------------------------------------
# DB4/DB6은 관측 결과와 무관하게 PASS를 찍고 있었다. DB4는 특히 모든 truncate()가
# 깔고 있는 "VARCHAR2 폭은 바이트다"라는 전제의 유일한 근거인데, 두 조회가 아무것도
# 돌려주지 않아도 PASS였다.

def test_db4_warns_when_it_observed_nothing():
    conn = _conn(**{"NLS_CHARACTERSET": [], "db_block_size": []})
    r = _by_id(run_db_checks(conn, skip_schema=True))
    assert r["DB4"].verdict == "WARN"
    assert "NLS_CHARACTERSET" in r["DB4"].impact


def test_db4_passes_when_both_facts_are_observed():
    r = _by_id(run_db_checks(_conn(), skip_schema=True))
    assert r["DB4"].verdict == "PASS"
    assert "AL32UTF8" in r["DB4"].observed


def test_db6_warns_when_no_test_tables_exist():
    """런북 4단계에서 0개가 보이면 DDL이 적용되지 않은 것이다 — PASS일 수 없다."""
    r = _by_id(run_db_checks(_conn(user_tables=[(0,)]), skip_schema=True))
    assert r["DB6"].verdict == "WARN"


def test_db6_warns_when_the_count_query_returned_nothing():
    r = _by_id(run_db_checks(_conn(user_tables=[]), skip_schema=True))
    assert r["DB6"].verdict == "WARN"
    assert "행을 돌려주지 않았다" in r["DB6"].observed


def test_db6_passes_when_tables_are_present():
    r = _by_id(run_db_checks(_conn(), skip_schema=True))
    assert r["DB6"].verdict == "PASS"
