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
    assert rec.count("write_watermark") == 0


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
