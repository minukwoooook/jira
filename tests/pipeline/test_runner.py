from datetime import datetime, timezone

import pytest

from jira_dashboard.db.repository.catalog import FieldChangeReport
from jira_dashboard.jira.protocol import JiraAuthError, JiraTransientError
from jira_dashboard.pipeline import runner as mod
from jira_dashboard.pipeline.sync_issues import SyncResult
from tests.stubs import CONN, Recorder

MAX_UPDATED = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


class FakeClient:
    """client.get_statuses()만 진짜로 호출된다 — 나머지 단계는 리포지토리
    스텁으로 대체되므로 실제 client 메서드를 요구하지 않는다."""

    def get_statuses(self):
        return [
            {"name": "Done", "statusCategory": {"key": "done"}},
            {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
        ]


@pytest.fixture
def rec(monkeypatch):
    r = Recorder()
    r.returns["reclaim_zombies"] = lambda *a, **k: 0
    r.returns["start_run"] = lambda *a, **k: 1
    r.returns["read_watermark"] = lambda conn, p: (None, False)
    r.returns["sync_catalog"] = lambda *a, **k: FieldChangeReport()
    r.returns["enabled_projects"] = lambda conn, i: [(7, "10000", "PROJ")]
    r.returns["sync_issues"] = lambda *a, **k: SyncResult(
        fetched=3, upserted=3, max_updated=MAX_UPDATED, changed_issue_ids=[1, 2, 3]
    )
    r.returns["derive_history"] = lambda *a, **k: 9
    r.returns["update_first_done_at"] = lambda *a, **k: 3
    r.patch(monkeypatch, mod, "sync_catalog", "enabled_projects", "sync_issues",
            "derive_history", "update_first_done_at", "profile_fields",
            "detect_deleted")
    r.patch(monkeypatch, mod.sync_repo, "reclaim_zombies", "start_run", "finish_run",
            "read_watermark", "write_watermark", "request_full_resync",
            "clear_full_resync")
    monkeypatch.setattr(CONN, "commit", lambda: None, raising=False)
    monkeypatch.setattr(CONN, "rollback", lambda: None, raising=False)
    return r


def test_reclaims_zombies_before_anything_else(rec):
    mod.run_instance(CONN, FakeClient(), 1)
    assert rec.names()[0] == "reclaim_zombies"


def test_catalog_runs_before_issues(rec):
    mod.run_instance(CONN, FakeClient(), 1)
    i_cat, i_iss = rec.order_of("sync_catalog", "sync_issues")
    assert i_cat < i_iss


def test_history_runs_after_issues(rec):
    mod.run_instance(CONN, FakeClient(), 1)
    i_iss, i_hist, i_done = rec.order_of(
        "sync_issues", "derive_history", "update_first_done_at"
    )
    assert i_iss < i_hist < i_done


def test_watermark_is_max_updated_minus_overlap(rec):
    from jira_dashboard.pipeline.sync_issues import OVERLAP

    mod.run_instance(CONN, FakeClient(), 1)
    payload = rec.first("write_watermark")
    assert payload["args"][1] == MAX_UPDATED - OVERLAP


def test_successful_run_reports_counts(rec):
    summary = mod.run_instance(CONN, FakeClient(), 1)
    assert (summary.projects_ok, summary.projects_failed) == (1, 0)
    assert summary.issues_upserted == 3


def test_project_failure_is_isolated(monkeypatch, rec):
    rec.returns["enabled_projects"] = lambda conn, i: [
        (7, "10000", "PROJ"), (8, "10001", "OTHER")
    ]
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise JiraTransientError(503)
        return SyncResult(fetched=1, upserted=1, max_updated=MAX_UPDATED,
                          changed_issue_ids=[9])

    monkeypatch.setattr(mod, "sync_issues", flaky)
    summary = mod.run_instance(CONN, FakeClient(), 1)
    assert (summary.projects_ok, summary.projects_failed) == (1, 1)
    assert "PROJ" in summary.errors


def test_failed_project_does_not_advance_watermark(monkeypatch, rec):
    monkeypatch.setattr(
        mod, "sync_issues", lambda *a, **k: (_ for _ in ()).throw(JiraTransientError(503))
    )
    mod.run_instance(CONN, FakeClient(), 1)
    payload = rec.first("write_watermark")
    assert payload["args"][1] is None      # since=None → NVL로 기존값 유지
    assert payload["args"][2] == "FAILED"


def test_auth_error_aborts_the_whole_instance(monkeypatch, rec):
    monkeypatch.setattr(
        mod, "sync_issues", lambda *a, **k: (_ for _ in ()).throw(JiraAuthError("401"))
    )
    with pytest.raises(JiraAuthError):
        mod.run_instance(CONN, FakeClient(), 1)
    payload = rec.first("write_watermark")
    assert payload["args"][1] is None      # since=None → NVL로 기존값 유지
    assert payload["args"][2] == "FAILED"


def test_auth_error_queues_full_resync(monkeypatch, rec):
    monkeypatch.setattr(
        mod, "sync_issues", lambda *a, **k: (_ for _ in ()).throw(JiraAuthError("401"))
    )
    with pytest.raises(JiraAuthError):
        mod.run_instance(CONN, FakeClient(), 1)
    assert rec.count("request_full_resync") == 1


def test_full_resync_flag_cleared_only_on_success(rec):
    rec.returns["read_watermark"] = lambda conn, p: (None, True)
    mod.run_instance(CONN, FakeClient(), 1)
    assert rec.count("clear_full_resync") == 1


def test_full_resync_flag_survives_failure(monkeypatch, rec):
    rec.returns["read_watermark"] = lambda conn, p: (None, True)
    monkeypatch.setattr(
        mod, "sync_issues", lambda *a, **k: (_ for _ in ()).throw(JiraTransientError(503))
    )
    mod.run_instance(CONN, FakeClient(), 1)
    assert rec.count("clear_full_resync") == 0


def test_project_key_change_requests_full_resync(rec):
    rec.returns["sync_catalog"] = lambda *a, **k: FieldChangeReport(
        key_changed_projects=[7]
    )
    mod.run_instance(CONN, FakeClient(), 1)
    assert rec.count("request_full_resync") >= 1


def test_value_kind_change_requests_full_resync_for_all_projects(rec):
    rec.returns["sync_catalog"] = lambda *a, **k: FieldChangeReport(
        value_kind_changed=["customfield_10002"]
    )
    mod.run_instance(CONN, FakeClient(), 1)
    assert rec.count("request_full_resync") >= 1


def test_dry_run_rolls_back_and_skips_history(monkeypatch, rec):
    rolled = []
    monkeypatch.setattr(CONN, "rollback", lambda: rolled.append(1), raising=False)
    mod.run_instance(CONN, FakeClient(), 1, dry_run=True)
    assert rolled
    assert rec.count("derive_history") == 0
    assert rec.count("write_watermark") == 0


def test_daily_flag_runs_profiling_and_delete_detection(rec):
    mod.run_instance(CONN, FakeClient(), 1, daily=True)
    assert rec.count("profile_fields") == 1
    assert rec.count("detect_deleted") == 1


def test_daily_steps_are_skipped_by_default(rec):
    mod.run_instance(CONN, FakeClient(), 1)
    assert rec.count("profile_fields") == 0


def test_dry_run_skips_daily_steps(rec):
    mod.run_instance(CONN, FakeClient(), 1, dry_run=True, daily=True)
    assert rec.count("detect_deleted") == 0


# --- Task 10 additions -----------------------------------------------------

def test_derive_history_receives_category_of_built_from_client_statuses(rec):
    """derive_history의 폴백(현재 이슈의 status_name)은 워크플로우에서 이미 빠진
    상태를 놓친다. 러너는 client.get_statuses()에서 뽑은, 인스턴스에 정의된
    모든 상태의 맵을 넘겨야 한다 — None을 넘기면 폴백으로 되돌아간다."""
    mod.run_instance(CONN, FakeClient(), 1)
    payload = rec.first("derive_history")
    category_of = payload["kwargs"].get("category_of")
    assert category_of is not None
    assert category_of == {"Done": "done", "In Progress": "indeterminate"}


def test_parse_failures_and_changelog_truncated_propagate_into_summary(rec):
    rec.returns["sync_issues"] = lambda *a, **k: SyncResult(
        fetched=3, upserted=3, max_updated=MAX_UPDATED, changed_issue_ids=[1, 2, 3],
        parse_failures=2, changelog_truncated=1,
    )
    summary = mod.run_instance(CONN, FakeClient(), 1)
    assert summary.parse_failures == 2
    assert summary.changelog_truncated == 1


# --- Final review fixes ----------------------------------------------------

def test_dry_run_is_threaded_into_sync_issues(rec):
    """C1: 러너의 rollback()만으로는 아무것도 되돌리지 못한다 — sync_issues가 페이지마다
    이미 커밋했기 때문이다. 플래그가 실제로 내려가는지 여기서 고정한다."""
    mod.run_instance(CONN, FakeClient(), 1, dry_run=True)
    assert rec.first("sync_issues")["kwargs"]["dry_run"] is True


def test_normal_run_does_not_pass_dry_run(rec):
    mod.run_instance(CONN, FakeClient(), 1)
    assert rec.first("sync_issues")["kwargs"]["dry_run"] is False


def test_project_filter_limits_the_run_to_one_project(rec):
    """C2: 런북 8~11단계가 전부 `sync --project TEST`다. 필터가 없으면 9단계가
    화이트리스트 전체를 한꺼번에 실수집한다."""
    rec.returns["enabled_projects"] = lambda conn, i: [
        (7, "10000", "PROJ"), (8, "10001", "OTHER")
    ]
    mod.run_instance(CONN, FakeClient(), 1, project="OTHER")
    keys = [call["args"][3] for call in rec.args_of("sync_issues")]
    assert keys == ["OTHER"]


def test_project_filter_matching_nothing_syncs_nothing(rec):
    rec.returns["enabled_projects"] = lambda conn, i: [(7, "10000", "PROJ")]
    summary = mod.run_instance(CONN, FakeClient(), 1, project="NOPE")
    assert rec.count("sync_issues") == 0
    assert (summary.projects_ok, summary.projects_failed) == (0, 0)


def test_value_kind_change_flags_every_project_even_when_filtered(rec):
    """--project로 좁혀도 value_kind 변경의 영향 범위는 좁아지지 않는다."""
    rec.returns["enabled_projects"] = lambda conn, i: [
        (7, "10000", "PROJ"), (8, "10001", "OTHER")
    ]
    rec.returns["sync_catalog"] = lambda *a, **k: FieldChangeReport(
        value_kind_changed=["customfield_10002"]
    )
    mod.run_instance(CONN, FakeClient(), 1, project="PROJ")
    assert rec.count("request_full_resync") == 2


def test_project_failure_requests_a_full_resync(monkeypatch, rec):
    """C4/R31: sync_issues가 페이지를 커밋한 뒤 derive_history가 터지면, 다음 실행은
    해시가 같아 전부 스킵하고 이력을 만들지 않은 채 SUCCESS를 보고한다. 실패한
    프로젝트에 플래그를 걸어야 다음 실행이 해시를 우회한다."""
    monkeypatch.setattr(
        mod, "derive_history",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("history exploded")),
    )
    summary = mod.run_instance(CONN, FakeClient(), 1)
    assert summary.projects_failed == 1
    requested = [call["args"][0] for call in rec.args_of("request_full_resync")]
    assert requested == [7]


def test_full_resync_flag_is_threaded_into_sync_issues(rec):
    """워터마크만 비우면 해시 스킵이 그대로 살아 있어 재수집이 아무것도 바꾸지 못한다."""
    rec.returns["read_watermark"] = lambda conn, p: (None, True)
    mod.run_instance(CONN, FakeClient(), 1)
    assert rec.first("sync_issues")["kwargs"]["full_resync"] is True


def test_history_and_first_done_are_recorded_as_run_steps(rec):
    """Item 10: TEST_SYNC_RUN.step은 'HISTORY'와 'FIRST_DONE'을 허용하는데 아무도
    쓰지 않아 이력 파생이 감사 테이블에서 보이지 않았다."""
    mod.run_instance(CONN, FakeClient(), 1)
    steps = [call["args"][2] for call in rec.args_of("start_run")]
    assert steps == ["CATALOG", "ISSUES", "HISTORY", "FIRST_DONE"]


def test_profile_failure_does_not_prevent_delete_detection(monkeypatch, rec):
    """Item 10: finish_run(SUCCESS)이 무조건 호출돼서, profile_fields 예외는 실행 행을
    6시간 RUNNING으로 남기고 detect_deleted를 아예 막았다."""
    monkeypatch.setattr(
        mod, "profile_fields",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("profile exploded")),
    )
    summary = mod.run_instance(CONN, FakeClient(), 1, daily=True)
    assert rec.count("detect_deleted") == 1
    assert summary.steps_failed == 1
    assert "PROFILE" in summary.errors
    finished = [call["args"] for call in rec.args_of("finish_run")]
    assert any(a[1] == "FAILED" for a in finished), finished


def test_delete_detection_failure_is_isolated_too(monkeypatch, rec):
    monkeypatch.setattr(
        mod, "detect_deleted",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("detect exploded")),
    )
    summary = mod.run_instance(CONN, FakeClient(), 1, daily=True)
    assert summary.steps_failed == 1
    assert "DETECT_DELETED" in summary.errors
