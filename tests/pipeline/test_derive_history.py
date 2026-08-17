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
    r.returns["load_current_eav_values"] = lambda *a, **k: {}
    r.patch(monkeypatch, mod.history_repo,
            "status_category_map", "load_issue_states", "load_changes",
            "replace_history", "update_first_done_at", "load_current_eav_values")
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


def test_category_of_override_value_actually_lands_in_the_written_row(rec):
    """status_category_map을 다시 안 부른다는 것만으로는 부족하다 — 넘긴 맵의
    효과가 실제로 기록된 status_category val_str에 반영돼야 한다. 워크플로우에서
    이미 빠진(현재 어느 이슈도 안 쓰는, 그래서 DB 파생 맵에는 없는) 상태 이름으로
    확인한다."""
    rec.returns["load_issue_states"] = lambda conn, ids: {
        i: {"created_at": CREATED, "current_values": {"status": ("퇴역상태", None)}}
        for i in ids
    }
    rec.returns["load_changes"] = lambda conn, ids: {}
    mod.derive_history(CONN, 1, [500], category_of={"퇴역상태": "done"})
    cat_row = next(r for r in _rows(rec, 500) if r["field_pk"] == 2)
    assert cat_row["val_str"] == "done"


# --- Critical fix 2: current_values must cover every tracked field, not just status ---

def test_non_status_tracked_field_uses_current_value_not_nulled(rec, monkeypatch):
    """이전에는 load_issue_states가 status만 채워서, priority 등 다른 tracked
    필드는 매번 '현재값 없음(None)'으로 취급되어 이력 종점 불일치로 잘못 지워지고
    이슈마다 경고가 났다. priority도 현재값이 실려 있으면 changelog의 마지막 값이
    그대로 유지돼야 한다."""
    monkeypatch.setattr(mod, "_tracked_fields",
                        lambda conn, i: {**FIELD_PKS, "priority": 3})
    rec.returns["load_issue_states"] = lambda conn, ids: {
        i: {"created_at": CREATED, "current_values": {
            "status": ("완료", "10"), "priority": ("High", None),
        }} for i in ids
    }
    rec.returns["load_changes"] = lambda conn, ids: {
        ids[0]: [ChangelogItem("h2", 0, None, None, T1, "priority", "priority",
                               None, "Low", None, "High")]
    }
    mod.derive_history(CONN, 1, [500])
    priority_row = next(r for r in _rows(rec, 500)
                        if r["field_pk"] == 3 and r["valid_to"] == SENTINEL)
    assert priority_row["val_str"] == "High"


def test_eav_current_values_are_merged_by_field_pk(rec, monkeypatch):
    """커스텀(EAV) dimension 필드의 현재값은 load_current_eav_values가 field_pk로
    돌려주므로, field_id로 뒤집어 병합돼야 changelog 종점과 올바르게 비교된다."""
    monkeypatch.setattr(mod, "_tracked_fields",
                        lambda conn, i: {**FIELD_PKS, "customfield_10001": 7})
    rec.returns["load_current_eav_values"] = lambda conn, ids: {
        ids[0]: {7: ("결함", "opt-1")}
    }
    rec.returns["load_changes"] = lambda conn, ids: {
        ids[0]: [ChangelogItem("h3", 0, None, None, T1, "Defect Cause",
                               "customfield_10001", "opt-0", "버그", "opt-1", "결함")]
    }
    mod.derive_history(CONN, 1, [500])
    custom_row = next(r for r in _rows(rec, 500)
                      if r["field_pk"] == 7 and r["valid_to"] == SENTINEL)
    assert custom_row["val_str"] == "결함"


# --- Item 6: batch must stay within Oracle's IN-list ceiling ---

def test_batch_over_1000_is_rejected():
    """_binds가 batch 크기 그대로 issue_id IN (...)을 만든다. 1000을 넘기면
    ORA-01795 (IN 리스트 표현식 1000개 상한)에 걸린다 — 호출 시점에 막는다."""
    with pytest.raises(ValueError):
        mod.derive_history(CONN, 1, [1], batch=1001)


def test_first_done_at_batch_over_1000_is_rejected():
    with pytest.raises(ValueError):
        mod.update_first_done_at(CONN, [1], batch=1001)
