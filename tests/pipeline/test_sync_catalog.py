import pytest

from jira_dashboard.db.repository import catalog
from jira_dashboard.jira.models import FieldDef
from jira_dashboard.pipeline import sync_catalog as mod
from tests.stubs import CONN, Recorder


def _defs_by_id(recorder) -> dict[str, FieldDef]:
    payload = recorder.first("upsert_fields")
    defs = payload["args"][1] if len(payload["args"]) > 1 else payload["kwargs"]["defs"]
    return {fd.field_id: fd for fd in defs}


@pytest.fixture
def rec(monkeypatch):
    r = Recorder()
    r.returns["upsert_fields"] = lambda *a, **k: []
    r.returns["upsert_projects"] = lambda *a, **k: []
    r.patch(monkeypatch, mod, "upsert_fields", "upsert_projects")
    return r


def test_calls_fields_before_projects(rec, fake_jira):
    mod.sync_catalog(CONN, fake_jira, 1)
    assert rec.names() == ["upsert_fields", "upsert_projects"]


def test_synthetic_fields_are_appended(rec, fake_jira):
    """/field 응답에 없는 status_category와 first_done_at을 직접 넣는다 (spec 4.1)."""
    mod.sync_catalog(CONN, fake_jira, 1)
    defs = _defs_by_id(rec)
    assert "status_category" in defs
    assert "first_done_at" in defs
    assert defs["status_category"].is_custom is False


def test_report_carries_both_change_lists(monkeypatch, fake_jira):
    r = Recorder()
    r.returns["upsert_fields"] = lambda *a, **k: ["customfield_10002"]
    r.returns["upsert_projects"] = lambda *a, **k: [42]
    r.patch(monkeypatch, mod, "upsert_fields", "upsert_projects")
    report = mod.sync_catalog(CONN, fake_jira, 1)
    assert report.value_kind_changed == ["customfield_10002"]
    assert report.key_changed_projects == [42]


# --- storage_for 분류 (순수 함수, 완전 검증) ---

def _fd(field_id, schema_type, items=None):
    return FieldDef(field_id, "F", not field_id.isalpha(), schema_type, items, None)


def test_system_field_gets_column_storage():
    storage, column, label, kind, dim, msr = catalog.storage_for(_fd("status", "status"))
    assert (storage, column, kind, dim) == ("COLUMN", "status_name", "STR", "Y")


def test_custom_field_gets_eav_storage():
    storage, column, label, kind, dim, msr = catalog.storage_for(
        _fd("customfield_10001", "option")
    )
    assert (storage, column, label) == ("EAV", None, None)


def test_multi_value_system_field_goes_to_eav():
    """labels는 시스템 필드지만 다중값이라 고정 컬럼에 담을 수 없다 (spec 4.1)."""
    storage, column, label, kind, dim, msr = catalog.storage_for(
        _fd("labels", "array", "string")
    )
    assert (storage, column, kind) == ("EAV", None, "MULTI")


def test_assignee_gets_label_column():
    storage, column, label, kind, dim, msr = catalog.storage_for(_fd("assignee", "user"))
    assert (column, label) == ("assignee_user_key", "assignee_display_name")


def test_measure_fields_are_not_dimensions():
    storage, column, label, kind, dim, msr = catalog.storage_for(
        _fd("timespent", "number")
    )
    assert (kind, dim, msr) == ("NUM", "N", "Y")


def test_summary_is_column_but_not_dimension():
    storage, column, label, kind, dim, msr = catalog.storage_for(_fd("summary", "string"))
    assert (storage, dim) == ("COLUMN", "N")


def test_column_and_eav_invariant_holds_for_every_field(fake_jira):
    """ck_jira_field_col: COLUMN이면 컬럼명이 있고 EAV면 없다. DB 제약과 같은 규칙."""
    from jira_dashboard.jira.parser import parse_field_defs

    for fd in parse_field_defs(fake_jira.get_fields()):
        storage, column, _, _, _, _ = catalog.storage_for(fd)
        assert (storage == "COLUMN") == (column is not None), fd.field_id


# --- field_pk_by_field_name — 동명 필드는 모호하므로 결과에서 제외한다 (spec §4.2) ---

class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed_with = None

    def execute(self, sql, **kwargs):
        self.executed_with = kwargs

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)


def test_field_pk_by_field_name_resolves_unique_name():
    conn = _FakeConn([("Summary", 10), ("Story Points", 11)])
    result = catalog.field_pk_by_field_name(conn, 1)
    assert result == {"Summary": 10, "Story Points": 11}


def test_field_pk_by_field_name_excludes_duplicated_name():
    """한 인스턴스 안에 동명 커스텀 필드가 둘이면 모호하므로 결과에서 통째로 뺀다."""
    conn = _FakeConn([("결함원인", 20), ("결함원인", 21), ("Summary", 10)])
    result = catalog.field_pk_by_field_name(conn, 1)
    assert result == {"Summary": 10}
    assert "결함원인" not in result


# --- instance/project 관리자 명령용 리포지토리 함수 ---

def test_list_instances_returns_rows():
    conn = _FakeConn([("SITE_A", "https://jira.internal", "PAT",
                       "JIRA_SITE_A_TOKEN", "Y")])
    assert catalog.list_instances(conn) == [
        ("SITE_A", "https://jira.internal", "PAT", "JIRA_SITE_A_TOKEN", "Y")
    ]


def test_list_projects_returns_rows():
    conn = _FakeConn([("TEST", "Test Project", "N")])
    assert catalog.list_projects(conn, 1) == [("TEST", "Test Project", "N")]


class _FakeEnableCursor:
    def __init__(self, rowcount):
        self.rowcount = rowcount
        self.executed_with = None

    def execute(self, sql, **kwargs):
        self.executed_with = kwargs


class _FakeEnableConn:
    def __init__(self, rowcount):
        self._rowcount = rowcount
        self.cur = None

    def cursor(self):
        self.cur = _FakeEnableCursor(self._rowcount)
        return self.cur


def test_set_project_enabled_binds_y_and_returns_rowcount():
    """CHECK 제약(test_ck_jira_project_en)은 'Y'/'N'만 받는다 — Python bool을
    그대로 보내면 안 된다."""
    conn = _FakeEnableConn(rowcount=1)
    affected = catalog.set_project_enabled(conn, 1, "TEST", True)
    assert affected == 1
    assert conn.cur.executed_with == {
        "instance_id": 1, "project_key": "TEST", "enabled": "Y",
    }


def test_set_project_enabled_binds_n_for_disable():
    conn = _FakeEnableConn(rowcount=1)
    catalog.set_project_enabled(conn, 1, "TEST", False)
    assert conn.cur.executed_with["enabled"] == "N"


def test_set_project_enabled_missing_key_reports_zero_rows_affected():
    """존재하지 않는 project_key는 조용히 "성공"하면 안 된다 — 호출자가 rows
    affected로 구분해야 한다."""
    conn = _FakeEnableConn(rowcount=0)
    assert catalog.set_project_enabled(conn, 1, "NOPE", True) == 0
