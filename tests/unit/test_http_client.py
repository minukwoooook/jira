import httpx
import pytest

from jira_dashboard.jira.client import HttpJiraClient
from jira_dashboard.jira.protocol import JiraAuthError, JiraTransientError


def _client(handler, monkeypatch) -> HttpJiraClient:
    monkeypatch.setattr("jira_dashboard.jira.client.time.sleep", lambda _: None)
    c = HttpJiraClient("https://jira.example.com", "sekret-token")
    c._c = httpx.Client(base_url="https://jira.example.com",
                        headers=c._c.headers,
                        transport=httpx.MockTransport(handler))
    return c


def test_401_raises_auth_error_without_retrying(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401)

    c = _client(handler, monkeypatch)
    with pytest.raises(JiraAuthError):
        c.get_fields()
    assert len(calls) == 1  # no retry on auth failure


def test_403_raises_auth_error(monkeypatch):
    def handler(request):
        return httpx.Response(403)

    c = _client(handler, monkeypatch)
    with pytest.raises(JiraAuthError):
        c.get_projects()


def test_404_maps_to_none_for_get_issue(monkeypatch):
    def handler(request):
        return httpx.Response(404)

    c = _client(handler, monkeypatch)
    assert c.get_issue("10001", ["summary"]) is None


def test_transient_status_is_retried_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json=[{"id": "1"}])

    c = _client(handler, monkeypatch)
    assert c.get_fields() == [{"id": "1"}]
    assert calls["n"] == 3


def test_transient_status_raises_after_exhausting_retries(monkeypatch):
    def handler(request):
        return httpx.Response(429)

    c = _client(handler, monkeypatch)
    with pytest.raises(JiraTransientError):
        c.get_fields()


def test_search_issues_reports_max_results_from_response_not_request():
    """A7: 서버가 요청보다 적게 줄 수 있다 — 응답값을 신뢰해야 페이징이 안 깨진다."""
    def handler(request):
        return httpx.Response(200, json={
            "startAt": 0, "maxResults": 50, "total": 500, "issues": [],
        })

    c = HttpJiraClient.__new__(HttpJiraClient)
    c._c = httpx.Client(base_url="https://jira.example.com",
                        transport=httpx.MockTransport(handler))
    page = c.search_issues("project = PROJ", 0, 1000, False)
    assert page.max_results == 50
    assert page.max_results != 1000


def test_get_issue_changelog_slices_client_side_by_start_at():
    def handler(request):
        return httpx.Response(200, json={
            "changelog": {
                "maxResults": 100, "total": 3,
                "histories": [{"id": "h1"}, {"id": "h2"}, {"id": "h3"}],
            },
        })

    c = HttpJiraClient.__new__(HttpJiraClient)
    c._c = httpx.Client(base_url="https://jira.example.com",
                        transport=httpx.MockTransport(handler))
    page = c.get_issue_changelog("PROJ-1", 1)
    assert [h["id"] for h in page.histories] == ["h2", "h3"]


def test_from_config_raises_when_secret_env_var_missing(monkeypatch):
    monkeypatch.delenv("JIRA_TEST_TOKEN_MISSING", raising=False)
    with pytest.raises(JiraAuthError):
        HttpJiraClient.from_config("https://jira.example.com", "PAT",
                                   "JIRA_TEST_TOKEN_MISSING")


def test_token_never_appears_in_repr():
    c = HttpJiraClient("https://jira.example.com", "super-secret-token")
    assert "super-secret-token" not in repr(c)
