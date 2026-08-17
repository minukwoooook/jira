from jira_dashboard.config.settings import Settings


def test_loads_from_environment(monkeypatch):
    monkeypatch.setenv("ORACLE_DSN", "host:1521/SVC")
    monkeypatch.setenv("ORACLE_USER", "u")
    monkeypatch.setenv("ORACLE_PASSWORD", "p")
    s = Settings(_env_file=None)
    assert s.oracle_dsn == "host:1521/SVC"
    assert s.display_tz == "Asia/Seoul"


def test_display_tz_is_overridable(monkeypatch):
    monkeypatch.setenv("ORACLE_DSN", "h:1521/S")
    monkeypatch.setenv("ORACLE_USER", "u")
    monkeypatch.setenv("ORACLE_PASSWORD", "p")
    monkeypatch.setenv("DISPLAY_TZ", "UTC")
    assert Settings(_env_file=None).display_tz == "UTC"
