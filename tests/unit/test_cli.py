from contextlib import contextmanager

from jira_dashboard import cli
from jira_dashboard.pipeline.runner import RunSummary


def test_sync_prints_parse_failures_and_changelog_truncated(monkeypatch, capsys):
    """spec 목표: parse_failures/changelog_truncated는 조용한 데이터 손실 지표이므로
    반드시 사람 눈에 보이는 출력까지 도달해야 한다. DB나 서버 없이 검증한다."""

    @contextmanager
    def fake_db_conn(*, read_only=False):
        yield object()

    def fake_client_for(conn, instance_key):
        return 1, object()

    def fake_run_instance(conn, client, instance_id, *, dry_run=False, daily=False,
                          project=None):
        return RunSummary(
            projects_ok=1,
            projects_failed=0,
            issues_upserted=5,
            parse_failures=7,
            changelog_truncated=3,
        )

    # db_conn is imported inside cli.main() via `from jira_dashboard.db.pool import
    # db_conn`, so it must be patched at its source module — patching
    # jira_dashboard.cli.db_conn would have no effect (the name is never bound at
    # cli module scope).
    monkeypatch.setattr("jira_dashboard.db.pool.db_conn", fake_db_conn)
    monkeypatch.setattr(cli, "_client_for", fake_client_for)
    # Likewise run_instance is imported locally inside the "sync" branch.
    monkeypatch.setattr(
        "jira_dashboard.pipeline.runner.run_instance", fake_run_instance
    )

    exit_code = cli.main(["sync", "--instance", "SITE_A"])

    out = capsys.readouterr().out
    assert "parse_failures=7" in out
    assert "changelog_truncated=3" in out
    assert exit_code == 0


def _patched_cli(monkeypatch, seen):
    """cli.main을 DB/서버 없이 돌리고 db_conn/run_instance에 전달된 인자를 모은다."""

    @contextmanager
    def fake_db_conn(*, read_only=False):
        seen["read_only"] = read_only
        yield object()

    def fake_run_instance(conn, client, instance_id, **kwargs):
        seen.update(kwargs)
        return RunSummary(projects_ok=1)

    monkeypatch.setattr("jira_dashboard.db.pool.db_conn", fake_db_conn)
    monkeypatch.setattr(cli, "_client_for", lambda conn, key: (1, object()))
    monkeypatch.setattr("jira_dashboard.pipeline.runner.run_instance",
                        fake_run_instance)


def test_dry_run_asks_for_a_read_only_connection(monkeypatch):
    """C1: db_conn이 종료 시 커밋하면 러너의 rollback()은 아무 의미가 없다."""
    seen = {}
    _patched_cli(monkeypatch, seen)
    assert cli.main(["sync", "--instance", "SITE_A", "--dry-run"]) == 0
    assert seen["read_only"] is True
    assert seen["dry_run"] is True


def test_normal_sync_uses_a_committing_connection(monkeypatch):
    seen = {}
    _patched_cli(monkeypatch, seen)
    cli.main(["sync", "--instance", "SITE_A"])
    assert seen["read_only"] is False


def test_project_option_reaches_the_runner(monkeypatch):
    """C2: 런북 8~11단계가 `sync --project TEST`를 쓴다."""
    seen = {}
    _patched_cli(monkeypatch, seen)
    cli.main(["sync", "--instance", "SITE_A", "--project", "TEST"])
    assert seen["project"] == "TEST"


def test_failed_daily_step_makes_the_exit_code_nonzero(monkeypatch):
    """일일 단계 실패가 종료 코드에 나타나지 않으면 cron이 성공으로 착각한다."""

    @contextmanager
    def fake_db_conn(*, read_only=False):
        yield object()

    monkeypatch.setattr("jira_dashboard.db.pool.db_conn", fake_db_conn)
    monkeypatch.setattr(cli, "_client_for", lambda conn, key: (1, object()))
    monkeypatch.setattr(
        "jira_dashboard.pipeline.runner.run_instance",
        lambda *a, **k: RunSummary(projects_ok=1, steps_failed=1,
                                   errors={"PROFILE": "boom"}),
    )
    assert cli.main(["sync", "--instance", "SITE_A", "--daily"]) == 1
