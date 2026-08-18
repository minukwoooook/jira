from datetime import datetime, timezone

from jira_dashboard.db.repository import history as history_repo
from jira_dashboard.jira.models import KST, ChangelogItem


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
    def __init__(self, captured=None, rows=None):
        self._captured = captured if captured is not None else {}
        self._rows = rows if rows is not None else []
        self.executed_with = None

    def execute(self, sql, **kwargs):
        self.executed_with = kwargs
        calls = self._captured.setdefault("execute_calls", [])
        calls.append((sql, kwargs))

    def executemany(self, sql, rows, batcherrors=False):
        self._captured["rows"] = rows
        self._captured["called"] = True

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, captured=None, rows=None):
        self._captured = captured if captured is not None else {}
        self._rows = rows if rows is not None else []

    def cursor(self):
        return _FakeCursor(self._captured, self._rows)


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


# --- Critical fix 1: Oracle TIMESTAMP columns come back naive; every timestamp in
# this pipeline is KST by convention (spec §2.1) and build_intervals compares them
# against the KST-aware SENTINEL. Read-path normalization must not be skipped. ---

def test_load_issue_states_normalizes_naive_timestamps_and_maps_columns():
    naive_created = datetime(2026, 1, 1)  # what oracledb actually returns
    conn = _FakeConn(rows=[
        (500, naive_created, "Bug", "완료", "High", "Fixed",
         "jdoe", "Jane Doe", "asmith", "Alice Smith", "PROJ-1"),
    ])
    states = history_repo.load_issue_states(conn, [500])
    state = states[500]

    assert state["created_at"] == datetime(2026, 1, 1, tzinfo=KST)
    assert state["created_at"].tzinfo is not None

    cv = state["current_values"]
    assert cv["issuetype"] == ("Bug", None)
    assert cv["status"] == ("완료", None)
    assert cv["priority"] == ("High", None)
    assert cv["resolution"] == ("Fixed", None)
    assert cv["assignee"] == ("Jane Doe", "jdoe")
    assert cv["reporter"] == ("Alice Smith", "asmith")
    assert cv["parent"] == ("PROJ-1", None)
    assert "status_category" not in cv  # merge_categories owns this field exclusively


def test_load_changes_normalizes_naive_timestamps_and_maps_columns_positionally():
    """load_changes가 10개 컬럼을 위치로 언패킹한다 — from_str/to_str이 뒤바뀌면
    이력의 모든 구간이 거꾸로 뒤집히는데 아무것도 알려주지 않는다."""
    naive_changed = datetime(2026, 1, 5)  # what oracledb actually returns
    conn = _FakeConn(rows=[
        (500, "h1", 0, naive_changed, "status", "status", "1", "To Do", "10", "완료"),
    ])
    changes = history_repo.load_changes(conn, [500])
    item = changes[500][0]

    assert item.changed_at == datetime(2026, 1, 5, tzinfo=KST)
    assert item.changed_at.tzinfo is not None
    assert item.history_id == "h1"
    assert item.item_seq == 0
    assert item.field_id == "status"
    assert item.field_name == "status"
    assert item.from_id == "1"
    assert item.from_str == "To Do"
    assert item.to_id == "10"
    assert item.to_str == "완료"


def test_naive_db_timestamps_do_not_break_build_intervals():
    """load_issue_states/load_changes가 (수정 전처럼) naive datetime을 그대로
    돌려주면 build_intervals가 SENTINEL과 `<` 비교에서 TypeError로 죽는다 — 실제
    changelog가 있는 첫 이슈에서 바로 재현된다. 정규화된 출력이 안전한지 여기서
    끝까지 확인한다."""
    from jira_dashboard.pipeline.derive_history import build_intervals

    issue_conn = _FakeConn(rows=[
        (500, datetime(2026, 1, 1), "Bug", "완료", None, None,
         None, None, None, None, None),
    ])
    state = history_repo.load_issue_states(issue_conn, [500])[500]

    changes_conn = _FakeConn(rows=[
        (500, "h1", 0, datetime(2026, 1, 5), "status", "status",
         None, "To Do", None, "완료"),
    ])
    changes = history_repo.load_changes(changes_conn, [500])[500]

    out = build_intervals(state["created_at"], state["current_values"],
                          changes, {"status"})
    assert out  # 예외 없이 완료됐다는 것 자체가 이 테스트의 요점이다


def test_load_current_eav_values_groups_by_issue_and_field_pk():
    conn = _FakeConn(rows=[
        (500, 7, "결함", "opt-1"),
        (500, 8, "라벨A", None),
        (501, 7, "버그", "opt-2"),
    ])
    out = history_repo.load_current_eav_values(conn, [500, 501])
    assert out[500] == {7: ("결함", "opt-1"), 8: ("라벨A", None)}
    assert out[501] == {7: ("버그", "opt-2")}


# --- Item 4: repository read/write functions each need at least one direct test ---

def test_replace_history_deletes_then_inserts_when_rows_present():
    captured = {}
    conn = _FakeConn(captured)
    rows = [{"issue_id": 500, "field_pk": 1,
             "valid_from": datetime(2026, 1, 1, tzinfo=timezone.utc),
             "valid_to": datetime(2026, 1, 5, tzinfo=timezone.utc),
             "val_str": "To Do", "val_id": None}]
    history_repo.replace_history(conn, 500, rows)

    kinds = [call[0] for call in captured["execute_calls"]]
    assert "DELETE FROM test_issue_field_history" in kinds[0]
    assert captured["execute_calls"][0][1] == {"issue_id": 500}
    assert captured["rows"] == rows  # executemany captured the insert rows


def test_replace_history_still_deletes_when_rows_are_empty():
    """빈 이슈(변경 이력 없음)라도 예전 구간을 지우는 DELETE는 건너뛰면 안 된다."""
    captured = {}
    conn = _FakeConn(captured)
    history_repo.replace_history(conn, 500, [])

    assert len(captured["execute_calls"]) == 1
    assert "called" not in captured  # executemany was never reached


# --- 형제 함수들과 같은 빈 리스트 가드 (IN ()는 ORA-00936) ---------------------

class _ExplodingConn:
    """커서를 만들려고 하면 터진다 — SQL을 아예 실행하지 않아야 통과한다."""

    def cursor(self):
        raise AssertionError("빈 issue_ids로 SQL을 실행하려 했다 (IN () → ORA-00936)")


def test_load_changes_with_no_issue_ids_runs_no_sql():
    assert history_repo.load_changes(_ExplodingConn(), []) == {}


def test_update_first_done_at_with_no_issue_ids_runs_no_sql():
    assert history_repo.update_first_done_at(_ExplodingConn(), []) == 0
