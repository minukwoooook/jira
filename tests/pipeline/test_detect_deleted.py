import pytest

from jira_dashboard.pipeline import detect_deleted as mod
from tests.stubs import CONN, Recorder

WHITELIST = {"10000": 7, "10001": 8}


def test_missing_and_gone_is_deleted():
    assert mod.classify(None, WHITELIST) == "DELETED"


def test_missing_but_moved_inside_whitelist_is_moved_in():
    raw = {"key": "OTHER-9", "fields": {"project": {"id": "10001"}}}
    assert mod.classify(raw, WHITELIST) == "MOVED_IN"


def test_missing_and_moved_outside_whitelist_is_moved_out():
    """되살릴 수 있어야 하므로 DELETED와 구분한다 (spec 3.3.4)."""
    raw = {"key": "ARCHIVE-1", "fields": {"project": {"id": "99999"}}}
    assert mod.classify(raw, WHITELIST) == "MOVED_OUT"


def test_live_ids_advance_by_response_max_results(fake_jira):
    """A7: 서버가 maxResults를 줄일 수 있으므로 응답값으로 페이징한다."""
    ids = mod.live_issue_ids(fake_jira, "PROJ")
    total = fake_jira.search_issues("project = PROJ", 0, 1000, False).total
    assert len(ids) == total


def test_live_ids_with_shrunk_pages(fixture_dir):
    from jira_dashboard.jira.fake import FakeJiraClient

    client = FakeJiraClient(fixture_dir, server_max_results=2)
    ids = mod.live_issue_ids(client, "PROJ")
    total = client.search_issues("project = PROJ", 0, 1000, False).total
    assert len(ids) == total


def test_deleted_and_moved_out_are_marked(monkeypatch, fake_jira):
    r = Recorder()
    r.returns["enabled_projects"] = lambda conn, i: [(7, "10000", "PROJ")]
    r.returns["project_id_by_jira_id"] = lambda conn, i: WHITELIST
    r.returns["load_undeleted"] = lambda conn, p: {"1001": 501, "1002": 502}
    r.patch(monkeypatch, mod, "enabled_projects", "project_id_by_jira_id")
    monkeypatch.setattr(mod, "load_undeleted", r.stub("load_undeleted"))
    monkeypatch.setattr(mod, "mark_deleted", r.stub("mark_deleted"))
    monkeypatch.setattr(mod, "relocate_issue", r.stub("relocate_issue"))
    monkeypatch.setattr(CONN, "commit", lambda: None, raising=False)
    monkeypatch.setattr(mod, "live_issue_ids", lambda c, k: set())

    calls = {"1001": None,
             "1002": {"key": "OTHER-3", "fields": {"project": {"id": "10001"}}}}
    monkeypatch.setattr(fake_jira, "get_issue", lambda jid, fields: calls[jid])

    verdicts = mod.detect_deleted(CONN, fake_jira, 1)
    by_id = {v.jira_issue_id: v.reason for v in verdicts}
    assert by_id == {"1001": "DELETED", "1002": "MOVED_IN"}
    assert r.count("relocate_issue") == 1
    marked = r.first("mark_deleted")["args"][0]
    assert [m["reason"] for m in marked] == ["DELETED"]


def test_live_ids_stop_on_zero_progress(fake_jira):
    """Item 12: 여기서 페이징을 다시 구현했더니, 100줄 옆 sync_issues에 있던 무진행
    가드가 빠져 있었다 — max_results=0인데 issues가 비어있지 않으면 start_at이
    전진하지 못해 영원히 돈다. 이제 같은 iter_search_pages를 쓴다."""
    from jira_dashboard.jira.protocol import SearchPage

    real = fake_jira.search_issues("project = PROJ", 0, 1000, False)
    stuck = SearchPage(start_at=0, max_results=0, total=real.total,
                       issues=real.issues)

    class _ZeroProgressClient:
        def __init__(self):
            self.calls = 0

        def search_issues(self, jql, start_at, max_results, expand_changelog):
            self.calls += 1
            assert self.calls < 10, "무진행 루프에 빠졌다"
            return stuck

    client = _ZeroProgressClient()
    ids = mod.live_issue_ids(client, "PROJ")
    assert client.calls == 1
    assert len(ids) == len(real.issues)


def test_live_ids_do_not_request_changelog(fake_jira):
    """id만 필요하다 — expand=changelog를 켜면 응답이 수십 배가 된다."""
    seen = []

    class _Spy:
        def search_issues(self, jql, start_at, max_results, expand_changelog):
            seen.append(expand_changelog)
            return fake_jira.search_issues(jql, start_at, max_results,
                                           expand_changelog)

    mod.live_issue_ids(_Spy(), "PROJ")
    assert seen and not any(seen)
