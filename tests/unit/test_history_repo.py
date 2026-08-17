from datetime import datetime, timezone

from jira_dashboard.db.repository import history as history_repo
from jira_dashboard.jira.models import ChangelogItem


def _item(**overrides):
    base = dict(
        history_id="1", item_seq=0, author_user_key="jdoe",
        author_display_name="Jane Doe",
        changed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        field_name="status", field_id=None,
        from_id=None, from_str=None, to_id=None, to_str=None,
    )
    base.update(overrides)
    return ChangelogItem(**base)


# --- Correction 3: field_pk 해석은 fieldId -> field_id -> 이름 -> None 순서 ---

def test_resolve_prefers_field_id_when_present():
    item = _item(field_id="customfield_10001", field_name="Story Points")
    pk = history_repo._resolve_field_pk(
        item, {"customfield_10001": 101}, {"Story Points": 999}
    )
    assert pk == 101


def test_system_field_with_no_field_id_resolves_via_name_as_system_id():
    """10.3의 ChangeItemBean에는 fieldId가 없다. field 문자열 자체가 시스템
    필드의 id인 경우(status 등)는 field_pks에서 바로 찾을 수 있어야 한다."""
    item = _item(field_id=None, field_name="status")
    pk = history_repo._resolve_field_pk(item, {"status": 5}, {})
    assert pk == 5


def test_custom_field_with_no_field_id_resolves_via_display_name():
    item = _item(field_id=None, field_name="Story Points")
    pk = history_repo._resolve_field_pk(item, {}, {"Story Points": 202})
    assert pk == 202


def test_unknown_field_name_resolves_to_none():
    item = _item(field_id=None, field_name="Something Unmapped")
    pk = history_repo._resolve_field_pk(
        item, {"status": 5}, {"Story Points": 202}
    )
    assert pk is None


class _FakeCursor:
    def __init__(self, captured):
        self._captured = captured

    def executemany(self, sql, rows, batcherrors=False):
        self._captured["rows"] = rows
        self._captured["called"] = True


class _FakeConn:
    def __init__(self, captured):
        self._captured = captured

    def cursor(self):
        return _FakeCursor(self._captured)


def test_upsert_changelog_applies_three_tier_resolution_per_row():
    captured = {}
    items = [
        _item(history_id="h1", item_seq=0, field_id=None, field_name="status"),
        _item(history_id="h1", item_seq=1, field_id=None, field_name="Story Points"),
        _item(history_id="h1", item_seq=2, field_id=None, field_name="Unmapped"),
    ]
    history_repo.upsert_changelog(
        _FakeConn(captured), 42, items, {"status": 5}, {"Story Points": 202}
    )

    rows = captured["rows"]
    assert rows[0]["field_pk"] == 5
    assert rows[1]["field_pk"] == 202
    assert rows[2]["field_pk"] is None


# --- Correction 3 사후 가시성: 해석 실패한 이름을 집합으로 돌려줘야 sync_issues가
# 한 번만 모아 로그할 수 있다 ---

def test_upsert_changelog_returns_distinct_unresolved_field_names():
    captured = {}
    items = [
        _item(history_id="h1", item_seq=0, field_id=None, field_name="Fix Version"),
        _item(history_id="h1", item_seq=1, field_id=None, field_name="Fix Version"),
        _item(history_id="h1", item_seq=2, field_id=None, field_name="status"),
    ]
    unresolved = history_repo.upsert_changelog(
        _FakeConn(captured), 42, items, {"status": 5}, {}
    )
    assert unresolved == {"Fix Version"}


# --- Item 7: 실제 Oracle 오류 두 건 ---

def test_item_with_empty_field_name_is_skipped_not_written():
    """field_name이 빈 문자열이면 TEST_ISSUE_CHANGELOG.field_name NOT NULL을 어기고
    (Oracle은 빈 문자열을 NULL로 저장하므로) ORA-01400이 난다. 행 자체를 건너뛴다."""
    captured = {}
    items = [
        _item(history_id="h1", item_seq=0, field_id=None, field_name=""),
        _item(history_id="h1", item_seq=1, field_id=None, field_name="status"),
    ]
    history_repo.upsert_changelog(_FakeConn(captured), 42, items, {"status": 5}, {})

    rows = captured["rows"]
    assert len(rows) == 1
    assert rows[0]["field_name"] == "status"


def test_all_items_with_empty_field_name_results_in_no_write():
    captured = {}
    items = [_item(history_id="h1", item_seq=0, field_id=None, field_name="")]
    history_repo.upsert_changelog(_FakeConn(captured), 42, items, {}, {})
    assert "called" not in captured


def test_from_str_and_to_str_are_truncated_to_4000_bytes():
    """description 변경은 4000바이트를 쉽게 넘는다 — VARCHAR2(4000 BYTE)에 그대로
    넣으면 ORA-12899다."""
    captured = {}
    long_text = "x" * 5000
    items = [_item(history_id="h1", item_seq=0, field_id=None, field_name="status",
                   from_str=long_text, to_str=long_text)]
    history_repo.upsert_changelog(_FakeConn(captured), 42, items, {"status": 5}, {})

    row = captured["rows"][0]
    assert len(row["from_str"].encode("utf-8")) <= 4000
    assert len(row["to_str"].encode("utf-8")) <= 4000
