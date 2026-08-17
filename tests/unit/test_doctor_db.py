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


def test_schema_check_reports_missing_tables(monkeypatch):
    """DDL을 아직 안 돌렸으면 FAIL이어야 한다 — 런북 4단계의 게이트."""
    from jira_dashboard.doctor import db_checks

    monkeypatch.setattr(db_checks, "_actual_schema", lambda conn: {})
    r = _by_id(db_checks.run_db_checks(_conn()))
    assert r["DB7"].verdict == "FAIL"


def test_schema_check_passes_when_actual_matches_real_ddl(ddl_dir, monkeypatch):
    """DB7이 비교하는 두 쪽(파싱된 DDL, 실제 스키마)이 정확히 일치하면 PASS여야
    한다 — Task 9가 했던 것처럼, 런타임 비교 로직을 schema_map.parse_ddl의 실제
    출력과 맞대본다 (정적 게이트는 doctor를 스캔하지 않으므로 이 테스트가
    유일한 검증이다)."""
    from jira_dashboard.db import schema_map
    from jira_dashboard.doctor import db_checks

    expected = schema_map.parse_ddl(ddl_dir)
    monkeypatch.setattr(db_checks, "_actual_schema", lambda conn: expected)
    r = _by_id(db_checks.run_db_checks(_conn()))
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
