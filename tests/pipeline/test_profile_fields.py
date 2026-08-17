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
