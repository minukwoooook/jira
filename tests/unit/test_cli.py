from contextlib import contextmanager

from jira_dashboard import cli
from jira_dashboard.pipeline.runner import RunSummary


def test_sync_prints_parse_failures_and_changelog_truncated(monkeypatch, capsys):
    """spec 목표: parse_failures/changelog_truncated는 조용한 데이터 손실 지표이므로
    반드시 사람 눈에 보이는 출력까지 도달해야 한다. DB나 서버 없이 검증한다."""

    @contextmanager
    def fake_db_conn():
        yield object()

    def fake_client_for(conn, instance_key):
        return 1, object()

    def fake_run_instance(conn, client, instance_id, *, dry_run=False, daily=False):
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
