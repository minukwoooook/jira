from datetime import datetime, timezone

import pytest

from jira_dashboard.jira.models import SENTINEL, ChangelogItem
from jira_dashboard.pipeline import derive_history as mod
from tests.stubs import CONN, Recorder

CREATED = datetime(2026, 1, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 1, 5, tzinfo=timezone.utc)
FIELD_PKS = {"status": 1, "status_category": 2}


@pytest.fixture
def rec(monkeypatch):
    r = Recorder()
    r.returns["status_category_map"] = lambda *a, **k: {
        "To Do": "new", "개발중": "indeterminate", "완료": "done",
    }
    r.returns["load_issue_states"] = lambda conn, ids: {
        i: {"created_at": CREATED, "current_values": {"status": ("완료", "10")}}
        for i in ids
    }
    r.returns["load_changes"] = lambda conn, ids: {
        ids[0]: [ChangelogItem("h1", 0, None, None, T1, "status", "status",
                               None, "To Do", None, "완료")]
    }
    r.returns["update_first_done_at"] = lambda *a, **k: 1
    r.patch(monkeypatch, mod.history_repo,
            "status_category_map", "load_issue_states", "load_changes",
            "replace_history", "update_first_done_at")
    monkeypatch.setattr(mod, "_tracked_fields", lambda conn, i: FIELD_PKS)
    monkeypatch.setattr(CONN, "commit", lambda: None, raising=False)
    return r


def _rows(rec, issue_id):
    for call in rec.args_of("replace_history"):
        if call["args"][0] == issue_id:
            return call["args"][1]
    raise AssertionError(f"replace_history not called for {issue_id}")


def test_writes_both_status_and_category_intervals(rec):
    """status 구간과 status_category 구간이 함께 생성되어야 한다 (spec 5.3)."""
    mod.derive_history(CONN, 1, [500])
    field_pks = {row["field_pk"] for row in _rows(rec, 500)}
    assert field_pks == {1, 2}


def test_replaces_instead_of_appending(rec):
    """이슈 단위로 DELETE 후 재생성한다. 두 번 돌려도 같은 결과여야 한다."""
    mod.derive_history(CONN, 1, [500])
    first = _rows(rec, 500)
    rec.calls.clear()
    mod.derive_history(CONN, 1, [500])
    assert _rows(rec, 500) == first


def test_sentinel_is_used_for_the_open_interval(rec):
    mod.derive_history(CONN, 1, [500])
    assert any(row["valid_to"] == SENTINEL for row in _rows(rec, 500))


def test_all_intervals_have_positive_length(rec):
    """ck_ifh_range를 위반하는 행을 만들면 사내에서 적재가 실패한다."""
    mod.derive_history(CONN, 1, [500])
    for row in _rows(rec, 500):
        assert row["valid_from"] < row["valid_to"]


def test_untracked_fields_are_not_written(rec, monkeypatch):
    monkeypatch.setattr(mod, "_tracked_fields", lambda conn, i: {"status": 1})
    mod.derive_history(CONN, 1, [500])
    assert {row["field_pk"] for row in _rows(rec, 500)} == {1}


def test_commits_per_batch(rec, monkeypatch):
    """전체 재수집 시 100만 이슈를 한 트랜잭션에 넣으면 UNDO가 터진다 (spec 5.3)."""
    commits = []
    monkeypatch.setattr(CONN, "commit", lambda: commits.append(1), raising=False)
    mod.derive_history(CONN, 1, list(range(500, 2600)), batch=1000)
    assert len(commits) == 3


def test_empty_issue_list_is_a_noop(rec):
    assert mod.derive_history(CONN, 1, []) == 0
    assert rec.names() == []


def test_first_done_at_batches(rec, monkeypatch):
    commits = []
    monkeypatch.setattr(CONN, "commit", lambda: commits.append(1), raising=False)
    mod.update_first_done_at(CONN, list(range(1, 2501)), batch=1000)
    assert rec.count("update_first_done_at") == 3


# --- Correction 2: val_str truncates to 1000 bytes, not the changelog's 4000 ---

def test_val_str_written_is_truncated_to_1000_bytes(rec):
    """이력 값은 TEST_ISSUE_FIELD_HISTORY.val_str (VARCHAR2(1000 BYTE))에 들어간다.
    T7이 이미 4000바이트까지 허용한 changelog to_str이 그대로 넘어오면 여기서
    ORA-12899가 난다. 1001-4000바이트 구간의 값으로 확인한다."""
    long_value = "z" * 2000
    rec.returns["load_issue_states"] = lambda conn, ids: {
        i: {"created_at": CREATED, "current_values": {"status": (long_value, None)}}
        for i in ids
    }
    rec.returns["load_changes"] = lambda conn, ids: {}
    mod.derive_history(CONN, 1, [500])
    status_row = next(r for r in _rows(rec, 500) if r["field_pk"] == 1)
    assert len(status_row["val_str"].encode("utf-8")) <= 1000


# --- Correction 3: derive_history takes an optional category_of override ---

def test_explicit_category_of_skips_the_db_lookup(rec):
    """호출자가 category_of를 주면 status_category_map을 다시 조회하지 않는다.

    Task 10의 러너는 /rest/api/2/status에서 뽑은, 워크플로우에서 이미 빠진 상태까지
    포함하는 맵을 넘긴다 — DB에서 다시 뽑으면 그 상태들이 유실된다."""
    mod.derive_history(CONN, 1, [500], category_of={"완료": "done", "To Do": "new"})
    assert rec.count("status_category_map") == 0


def test_missing_category_of_falls_back_to_db_derived_map(rec):
    """category_of를 생략하면 함수가 단독으로도 테스트 가능하도록 DB에서 유도한 맵으로
    대체한다."""
    mod.derive_history(CONN, 1, [500])
    assert rec.count("status_category_map") == 1
