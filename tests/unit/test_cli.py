from contextlib import contextmanager

import pytest

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


def test_doctor_and_capture_use_read_only_connections(monkeypatch):
    """spec §11.5/§11.6은 doctor와 capture가 읽기 전용이라고 선언한다 — 선언을
    커넥션이 강제하게 한다."""
    seen = []

    @contextmanager
    def fake_db_conn(*, read_only=False):
        seen.append(read_only)
        yield object()

    monkeypatch.setattr("jira_dashboard.db.pool.db_conn", fake_db_conn)
    monkeypatch.setattr(cli, "_client_for", lambda conn, key: (1, object()))
    monkeypatch.setattr("jira_dashboard.doctor.db_checks.run_db_checks",
                        lambda conn, skip_schema=False: [])
    monkeypatch.setattr("jira_dashboard.capture.capture_fixtures",
                        lambda *a, **k: {})
    cli.main(["doctor", "--db", "--skip-schema"])
    cli.main(["capture", "--instance", "SITE_A", "--project", "PROJ"])
    assert seen == [True, True]


# --- instance/project bootstrap commands ---

def _fake_db_conn_capturing(seen):
    @contextmanager
    def fake_db_conn(*, read_only=False):
        seen.append(read_only)
        yield object()
    return fake_db_conn


def test_instance_add_uses_a_committing_connection(monkeypatch):
    """이 명령의 존재 이유가 MERGE를 커밋하는 것이다 — read_only=True면 조용히
    아무 일도 안 일어난다."""
    seen = []
    monkeypatch.setattr("jira_dashboard.db.pool.db_conn", _fake_db_conn_capturing(seen))
    monkeypatch.setattr("jira_dashboard.db.repository.catalog.upsert_instance",
                        lambda *a, **k: 1)
    exit_code = cli.main(["instance", "add", "--key", "SITE_A",
                          "--base-url", "https://jira.internal",
                          "--auth-type", "PAT", "--secret-ref", "JIRA_SITE_A_TOKEN"])
    assert exit_code == 0
    assert seen == [False]


def test_instance_add_rejects_a_bad_auth_type():
    """DDL의 test_ck_jira_instance_auth 위반을 Oracle 대신 argparse가 먼저 잡는다."""
    with pytest.raises(SystemExit):
        cli.main(["instance", "add", "--key", "SITE_A",
                 "--base-url", "https://jira.internal",
                 "--auth-type", "BOGUS", "--secret-ref", "JIRA_SITE_A_TOKEN"])


def test_instance_add_warns_when_secret_ref_env_var_is_unset(monkeypatch, capsys):
    seen = []
    monkeypatch.delenv("JIRA_SITE_A_TOKEN", raising=False)
    monkeypatch.setattr("jira_dashboard.db.pool.db_conn", _fake_db_conn_capturing(seen))
    monkeypatch.setattr("jira_dashboard.db.repository.catalog.upsert_instance",
                        lambda *a, **k: 1)
    cli.main(["instance", "add", "--key", "SITE_A",
             "--base-url", "https://jira.internal",
             "--auth-type", "PAT", "--secret-ref", "JIRA_SITE_A_TOKEN"])
    assert "JIRA_SITE_A_TOKEN" in capsys.readouterr().err


def test_instance_list_uses_a_committing_connection(monkeypatch):
    seen = []
    monkeypatch.setattr("jira_dashboard.db.pool.db_conn", _fake_db_conn_capturing(seen))
    monkeypatch.setattr("jira_dashboard.db.repository.catalog.list_instances",
                        lambda conn: [])
    cli.main(["instance", "list"])
    assert seen == [False]


def test_project_enable_uses_a_committing_connection(monkeypatch):
    seen = []
    monkeypatch.setattr("jira_dashboard.db.pool.db_conn", _fake_db_conn_capturing(seen))
    monkeypatch.setattr("jira_dashboard.db.repository.catalog.instance_config",
                        lambda conn, key: (1, "https://x", "PAT", "TOK"))
    monkeypatch.setattr("jira_dashboard.db.repository.catalog.set_project_enabled",
                        lambda *a, **k: 1)
    exit_code = cli.main(["project", "enable", "--instance", "SITE_A", "--key", "TEST"])
    assert exit_code == 0
    assert seen == [False]


def test_project_enable_missing_key_reports_not_found_not_success(monkeypatch):
    """rows affected == 0이면 "성공"을 출력하지 않고, 먼저 sync를 돌리라고
    알려준다 (catalog sync가 프로젝트를 발견하기 전에는 enable할 대상이 없다)."""
    seen = []
    monkeypatch.setattr("jira_dashboard.db.pool.db_conn", _fake_db_conn_capturing(seen))
    monkeypatch.setattr("jira_dashboard.db.repository.catalog.instance_config",
                        lambda conn, key: (1, "https://x", "PAT", "TOK"))
    monkeypatch.setattr("jira_dashboard.db.repository.catalog.set_project_enabled",
                        lambda *a, **k: 0)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["project", "enable", "--instance", "SITE_A", "--key", "NOPE"])
    message = str(exc_info.value)
    assert "no such project" in message
    assert "sync" in message


def test_project_disable_uses_a_committing_connection(monkeypatch):
    seen = []
    monkeypatch.setattr("jira_dashboard.db.pool.db_conn", _fake_db_conn_capturing(seen))
    monkeypatch.setattr("jira_dashboard.db.repository.catalog.instance_config",
                        lambda conn, key: (1, "https://x", "PAT", "TOK"))
    monkeypatch.setattr("jira_dashboard.db.repository.catalog.set_project_enabled",
                        lambda *a, **k: 1)
    exit_code = cli.main(["project", "disable", "--instance", "SITE_A", "--key", "TEST"])
    assert exit_code == 0
    assert seen == [False]


def test_project_list_reports_unknown_instance(monkeypatch):
    seen = []
    monkeypatch.setattr("jira_dashboard.db.pool.db_conn", _fake_db_conn_capturing(seen))
    monkeypatch.setattr("jira_dashboard.db.repository.catalog.instance_config",
                        lambda conn, key: None)
    with pytest.raises(SystemExit):
        cli.main(["project", "list", "--instance", "NOPE"])
