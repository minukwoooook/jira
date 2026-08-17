import logging
from datetime import datetime, timezone

from jira_dashboard.jira.models import SENTINEL, ChangelogItem
from jira_dashboard.pipeline.derive_history import (
    Interval, build_intervals, merge_categories,
)

CREATED = datetime(2026, 1, 1, tzinfo=timezone.utc)
TRACKED = {"status"}


def _c(at, frm, to, seq=0, field_id="status"):
    return ChangelogItem(
        history_id=f"h{at.day}{seq}", item_seq=seq,
        author_user_key="u", author_display_name="U", changed_at=at,
        field_name="status", field_id=field_id,
        from_id=None, from_str=frm, to_id=None, to_str=to,
    )


def _shape(intervals):
    return [(i.valid_from, i.valid_to, i.val_str) for i in intervals]


def test_no_changes_produces_single_sentinel_interval():
    out = build_intervals(CREATED, {"status": ("완료", None)}, [], TRACKED)
    assert _shape(out) == [(CREATED, SENTINEL, "완료")]


def test_first_interval_uses_from_str_of_first_change():
    t1 = datetime(2026, 1, 5, tzinfo=timezone.utc)
    out = build_intervals(CREATED, {"status": ("개발중", None)},
                          [_c(t1, "To Do", "개발중")], TRACKED)
    assert _shape(out) == [(CREATED, t1, "To Do"), (t1, SENTINEL, "개발중")]


def test_zero_length_interval_is_dropped():
    """같은 changed_at에 같은 필드가 두 번 바뀌면 앞의 것은 길이 0이다.
    ck_ifh_range가 valid_from < valid_to를 강제하므로 그냥 넣으면 DB가 거부한다."""
    t = datetime(2026, 1, 5, tzinfo=timezone.utc)
    out = build_intervals(
        CREATED, {"status": ("C", None)},
        [_c(t, "A", "B", seq=0), _c(t, "B", "C", seq=1)], TRACKED,
    )
    assert _shape(out) == [(CREATED, t, "A"), (t, SENTINEL, "C")]
    assert all(i.valid_from < i.valid_to for i in out)


def test_cleared_value_produces_null_interval():
    t = datetime(2026, 1, 5, tzinfo=timezone.utc)
    out = build_intervals(CREATED, {"status": (None, None)},
                          [_c(t, "To Do", None)], TRACKED)
    assert out[-1].val_str is None


def test_first_from_str_none_stays_none():
    """현재값으로 채우지 않는다 — 그 값은 나중에 설정된 것이다."""
    t = datetime(2026, 1, 5, tzinfo=timezone.utc)
    out = build_intervals(CREATED, {"status": ("완료", None)},
                          [_c(t, None, "완료")], TRACKED)
    assert out[0].val_str is None


def test_change_before_created_is_clamped():
    early = datetime(2025, 12, 1, tzinfo=timezone.utc)
    out = build_intervals(CREATED, {"status": ("B", None)},
                          [_c(early, "A", "B")], TRACKED)
    assert all(i.valid_from >= CREATED for i in out)
    assert out[0].valid_from == CREATED


def test_history_endpoint_mismatch_is_overwritten_by_current_value(caplog):
    """이력이 유실된 경우. 현재 시점 값이 틀리는 것이 최악이므로 현재값을 신뢰한다."""
    t = datetime(2026, 1, 5, tzinfo=timezone.utc)
    with caplog.at_level(logging.WARNING, logger="jira_dashboard.pipeline.derive_history"):
        out = build_intervals(CREATED, {"status": ("완료", None)},
                              [_c(t, "To Do", "개발중")], TRACKED)
    assert out[-1].val_str == "완료"
    assert any("history endpoint mismatch" in r.message for r in caplog.records)


def test_no_warning_when_history_endpoint_matches_current_value(caplog):
    """실제로 어긋나지 않으면 경고를 남기지 않는다 — 남기면 정상 케이스마다
    로그가 터진다."""
    t = datetime(2026, 1, 5, tzinfo=timezone.utc)
    with caplog.at_level(logging.WARNING, logger="jira_dashboard.pipeline.derive_history"):
        build_intervals(CREATED, {"status": ("완료", None)},
                        [_c(t, "To Do", "완료")], TRACKED)
    assert caplog.records == []


def test_field_absent_from_current_values_is_not_flagged_as_mismatch(caplog):
    """current_values에 그 필드의 키 자체가 없으면(아직 현재값을 모델링하지 않는
    필드) '불일치'로 오인해선 안 된다 — 없음과 다름은 다르다. 이 경우 changelog의
    마지막 값을 그대로 신뢰하고, 지우지도 경고하지도 않는다."""
    t = datetime(2026, 1, 5, tzinfo=timezone.utc)
    with caplog.at_level(logging.WARNING, logger="jira_dashboard.pipeline.derive_history"):
        out = build_intervals(CREATED, {}, [_c(t, "To Do", "완료", field_id="duedate")],
                              {"duedate"})
    assert out[-1].val_str == "완료"
    assert caplog.records == []


def test_untracked_fields_are_ignored():
    t = datetime(2026, 1, 5, tzinfo=timezone.utc)
    out = build_intervals(CREATED, {"status": ("완료", None)},
                          [_c(t, "a", "b", field_id="summary")], TRACKED)
    assert {i.field_id for i in out} == {"status"}


def test_changes_without_field_id_are_skipped():
    """field_pk가 NULL인 changelog 행은 구간 테이블에 넣을 수 없다 (NOT NULL)."""
    t = datetime(2026, 1, 5, tzinfo=timezone.utc)
    out = build_intervals(CREATED, {"status": ("완료", None)},
                          [_c(t, "a", "b", field_id=None)], TRACKED)
    assert _shape(out) == [(CREATED, SENTINEL, "완료")]


def test_val_id_is_carried_through():
    t = datetime(2026, 1, 5, tzinfo=timezone.utc)
    change = ChangelogItem(
        history_id="h1", item_seq=0, author_user_key=None, author_display_name=None,
        changed_at=t, field_name="status", field_id="status",
        from_id="1", from_str="To Do", to_id="10", to_str="완료",
    )
    out = build_intervals(CREATED, {"status": ("완료", "10")}, [change], TRACKED)
    assert (out[0].val_id, out[1].val_id) == ("1", "10")


# --- Correction 1: ordering is the only authority, not arrival order ---

def test_ordering_by_changed_at_is_authoritative_not_arrival_order():
    """changelog의 도착 순서는 신뢰할 수 없다 — DC 10.3 문서는 침묵하고, Cloud는
    한때 expand=changelog를 내림차순으로 바꿨으며, Atlassian 지원 가이드도 created
    타임스탬프로 정렬하라고 한다. build_intervals에게 같은 변경 목록을 뒤집어
    넘겨도 changed_at 기준으로 정렬해 같은 결과를 내야 한다."""
    t1 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 3, tzinfo=timezone.utc)
    t3 = datetime(2026, 1, 4, tzinfo=timezone.utc)
    ascending = [
        _c(t1, "To Do", "개발중"),
        _c(t2, "개발중", "리뷰중"),
        _c(t3, "리뷰중", "완료"),
    ]
    current = {"status": ("완료", None)}
    out_ascending = build_intervals(CREATED, current, ascending, TRACKED)
    out_reversed = build_intervals(CREATED, current, list(reversed(ascending)), TRACKED)
    assert _shape(out_ascending) == _shape(out_reversed)
    assert _shape(out_ascending) == [
        (CREATED, t1, "To Do"), (t1, t2, "개발중"), (t2, t3, "리뷰중"), (t3, SENTINEL, "완료"),
    ]


# --- Correction 2: val_str truncates to 1000 bytes, not the changelog's 4000 ---

def test_val_str_truncates_to_1000_bytes_not_4000():
    """TEST_ISSUE_FIELD_HISTORY.val_str is VARCHAR2(1000 BYTE), narrower than
    TEST_ISSUE_CHANGELOG's 4000 (T7). A value in the 1001-4000 window survives T7's
    truncation and would overflow here (ORA-12899) if not truncated again."""
    t = datetime(2026, 1, 5, tzinfo=timezone.utc)
    long_value = "x" * 2000
    out = build_intervals(CREATED, {"status": (long_value, None)},
                          [_c(t, "To Do", long_value)], TRACKED)
    assert len(out[-1].val_str.encode("utf-8")) <= 1000


def test_val_str_truncation_applies_to_no_changes_path_too():
    long_value = "y" * 1500
    out = build_intervals(CREATED, {"status": (long_value, None)}, [], TRACKED)
    assert len(out[0].val_str.encode("utf-8")) <= 1000


# --- Item 3: val_id truncates to 100 bytes (TEST_ISSUE_FIELD_HISTORY.val_id is
# VARCHAR2(100 BYTE) while changelog from_id/to_id are VARCHAR2(255 BYTE)) ---

def test_val_id_truncates_to_100_bytes():
    """assignee user key나 플러그인 옵션 id는 255바이트까지 올 수 있다 — 그대로
    넣으면 val_id 컬럼에서 ORA-12899가 난다."""
    t = datetime(2026, 1, 5, tzinfo=timezone.utc)
    long_id = "i" * 200
    change = ChangelogItem(
        history_id="h1", item_seq=0, author_user_key=None, author_display_name=None,
        changed_at=t, field_name="status", field_id="status",
        from_id=None, from_str="To Do", to_id=long_id, to_str="완료",
    )
    out = build_intervals(CREATED, {"status": ("완료", long_id)}, [change], TRACKED)
    assert all(i.val_id is None or len(i.val_id.encode("utf-8")) <= 100 for i in out)


# --- status_category 병합 ---

def test_merge_categories_collapses_consecutive_same_category():
    """개발중 → 리뷰중은 둘 다 indeterminate이므로 구간 1개다."""
    t1 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 3, tzinfo=timezone.utc)
    t3 = datetime(2026, 1, 4, tzinfo=timezone.utc)
    status = build_intervals(
        CREATED, {"status": ("완료", None)},
        [_c(t1, "To Do", "개발중"), _c(t2, "개발중", "리뷰중"), _c(t3, "리뷰중", "완료")],
        TRACKED,
    )
    merged = merge_categories(status, {
        "To Do": "new", "개발중": "indeterminate",
        "리뷰중": "indeterminate", "완료": "done",
    })
    assert _shape(merged) == [
        (CREATED, t1, "new"),
        (t1, t3, "indeterminate"),
        (t3, SENTINEL, "done"),
    ]
    assert all(i.field_id == "status_category" for i in merged)


def test_merge_categories_maps_unknown_status_to_undefined():
    out = build_intervals(CREATED, {"status": ("Weird", None)}, [], TRACKED)
    assert merge_categories(out, {})[0].val_str == "undefined"


def test_merge_categories_ignores_non_status_fields():
    other = [Interval("customfield_1", CREATED, SENTINEL, "x", None)]
    assert merge_categories(other, {}) == []


def test_merge_categories_handles_reopened_issue():
    """완료 → 재오픈 → 완료. 구간이 셋으로 남아야 한다."""
    t1 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 3, tzinfo=timezone.utc)
    status = build_intervals(
        CREATED, {"status": ("완료", None)},
        [_c(t1, "To Do", "완료"), _c(t2, "완료", "개발중"),
         _c(datetime(2026, 1, 4, tzinfo=timezone.utc), "개발중", "완료")],
        TRACKED,
    )
    merged = merge_categories(status, {
        "To Do": "new", "개발중": "indeterminate", "완료": "done",
    })
    assert [i.val_str for i in merged] == ["new", "done", "indeterminate", "done"]
