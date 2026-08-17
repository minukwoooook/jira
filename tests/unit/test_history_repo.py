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


def test_upsert_changelog_applies_three_tier_resolution_per_row():
    captured = {}

    class FakeCursor:
        def executemany(self, sql, rows, batcherrors=False):
            captured["rows"] = rows

    class FakeConn:
        def cursor(self):
            return FakeCursor()

    items = [
        _item(history_id="h1", item_seq=0, field_id=None, field_name="status"),
        _item(history_id="h1", item_seq=1, field_id=None, field_name="Story Points"),
        _item(history_id="h1", item_seq=2, field_id=None, field_name="Unmapped"),
    ]
    history_repo.upsert_changelog(
        FakeConn(), 42, items, {"status": 5}, {"Story Points": 202}
    )

    rows = captured["rows"]
    assert rows[0]["field_pk"] == 5
    assert rows[1]["field_pk"] == 202
    assert rows[2]["field_pk"] is None
