import logging
import re
from pathlib import Path

import pytest

from jira_dashboard.jira.fieldmap import SYSTEM_FIELD_MAP
from jira_dashboard.pipeline import profile_fields as mod


def test_column_fields_cover_every_system_field():
    assert {f for f, _ in mod.COLUMN_FIELDS} == set(SYSTEM_FIELD_MAP)


def test_column_names_come_from_the_map_only():
    """식별자를 SQL에 조립하므로, 출처가 화이트리스트임이 보장되어야 한다."""
    allowed = {spec.column_name for spec in SYSTEM_FIELD_MAP.values()}
    assert {c for _, c in mod.COLUMN_FIELDS} <= allowed


def test_column_names_are_safe_identifiers():
    for _, column in mod.COLUMN_FIELDS:
        assert re.fullmatch(r"[a-z_][a-z0-9_]*", column), column


def test_eav_profile_sql_resets_before_merge():
    """값을 비운 필드의 옛 카운트가 남으면 축 후보에 계속 뜬다 (spec 5.5)."""
    assert "issue_count = 0" in mod.RESET_COUNTS
    assert "MERGE INTO test_jira_project_field" in mod.MERGE_EAV_COUNTS


def test_eav_profile_is_a_single_group_by():
    """필드마다 COUNT를 돌리면 15,000 쿼리가 된다 (spec 5.5)."""
    assert mod.MERGE_EAV_COUNTS.upper().count("SELECT") <= 2
    assert "GROUP  BY" in mod.MERGE_EAV_COUNTS or "GROUP BY" in mod.MERGE_EAV_COUNTS


def test_distinct_count_pins_explicit_conversion_masks():
    """TO_CHAR without a mask is session-dependent (NLS_NUMERIC_CHARACTERS /
    NLS_TIMESTAMP_FORMAT) — distinct genuinely-different values can silently
    collapse into one string and undercount. A string check is the honest
    limit here: without a live DB there's no way to demonstrate the NLS
    behaviour itself, only that the mask is present in the SQL we send."""
    assert "TO_CHAR(v.val_num, 'TM')" in mod.MERGE_EAV_COUNTS
    assert "TO_CHAR(v.val_date, 'YYYY-MM-DD HH24:MI:SS.FF6')" in mod.MERGE_EAV_COUNTS


@pytest.fixture(scope="session")
def ddl_dir() -> Path:
    from jira_dashboard.db import schema_map
    return Path(schema_map.__file__).parent / "ddl"


def test_every_profiled_column_exists_in_the_ddl(ddl_dir):
    """_column_scan_sql builds its SELECT list at runtime, so the static gate
    cannot see these column names. Verify them against the parsed DDL directly."""
    from jira_dashboard.db import schema_map
    columns = schema_map.parse_ddl(ddl_dir)["TEST_JIRA_ISSUE"]
    for field_id, column in mod.COLUMN_FIELDS:
        assert column.upper() in columns, f"{field_id} → {column} not in TEST_JIRA_ISSUE"


# --- functional verification: single scan + reset-before-merge actually happen ---

class _FakeCursor:
    def __init__(self, field_pk_rows, scan_rows):
        self._field_pk_rows = field_pk_rows
        self._scan_rows = scan_rows
        self.executed: list[str] = []
        self.executemany_calls: list[tuple[str, list]] = []
        self.rowcount = 3
        self._last_sql = ""

    def execute(self, sql, **kwargs):
        self.executed.append(sql)
        self._last_sql = sql

    def executemany(self, sql, rows, **kwargs):
        self.executemany_calls.append((sql, list(rows)))

    def fetchall(self):
        if "test_jira_field" in self._last_sql:
            return self._field_pk_rows
        if "FROM   test_jira_issue" in self._last_sql:
            return self._scan_rows
        return []


class _FakeConn:
    def __init__(self, field_pk_rows, scan_rows):
        self._cur = _FakeCursor(field_pk_rows, scan_rows)
        self.committed = False

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed = True


def _fake_conn():
    field_pk_rows = [(field_id, i + 1) for i, (field_id, _) in enumerate(mod.COLUMN_FIELDS)]
    n = len(mod.COLUMN_FIELDS)
    scan_rows = [(100,) + tuple(range(n)) + tuple(range(n))]
    return _FakeConn(field_pk_rows, scan_rows)


def test_reset_counts_runs_before_the_merge():
    """RESET_COUNTS가 없거나 순서가 바뀌면 이 테스트가 깨진다 (spec 5.5)."""
    conn = _fake_conn()
    mod.profile_fields(conn, instance_id=1)
    executed = conn._cur.executed
    reset_idx = executed.index(mod.RESET_COUNTS)
    merge_idx = executed.index(mod.MERGE_EAV_COUNTS)
    assert reset_idx < merge_idx


def test_column_scan_hits_test_jira_issue_exactly_once():
    """필드마다 스캔하면 15,000 쿼리가 된다 — 컬럼 스캔은 프로젝트별 1회 SELECT여야 한다."""
    conn = _fake_conn()
    mod.profile_fields(conn, instance_id=1)
    scans = [s for s in conn._cur.executed if "FROM   test_jira_issue" in s]
    assert len(scans) == 1


def test_no_time_tracking_log_when_nothing_was_observed(caplog):
    """all()은 빈 generator에서 True다 — 프로젝트가 하나도 없거나 시간 필드가
    payload에 없으면 "Time Tracking이 꺼졌을 수 있다"는 로그가 근거 없이 나왔다."""
    conn = _FakeConn([], [])          # 프로젝트도, field_pk도 없다
    with caplog.at_level(logging.INFO, logger=mod.log.name):
        mod.profile_fields(conn, instance_id=1)
    assert not [r for r in caplog.records if "Time Tracking" in r.getMessage()]


def test_time_tracking_log_still_fires_when_counts_are_all_zero(caplog):
    """관측한 행이 있고 그게 전부 0일 때는 로그가 나와야 한다 — 위 가드가 진짜
    신호까지 죽이면 안 된다."""
    field_pk_rows = [(field_id, i + 1)
                     for i, (field_id, _) in enumerate(mod.COLUMN_FIELDS)]
    n = len(mod.COLUMN_FIELDS)
    scan_rows = [(100,) + (0,) * n + (0,) * n]
    conn = _FakeConn(field_pk_rows, scan_rows)
    with caplog.at_level(logging.INFO, logger=mod.log.name):
        mod.profile_fields(conn, instance_id=1)
    messages = [r.getMessage() for r in caplog.records if "Time Tracking" in r.getMessage()]
    assert len(messages) == 3, messages
