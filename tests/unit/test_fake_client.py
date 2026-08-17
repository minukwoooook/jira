import pytest

from jira_dashboard.jira.fake import FakeJiraClient
from jira_dashboard.jira.protocol import JiraTransientError

JQL = "project = PROJ ORDER BY updated ASC"


def test_search_paginates_by_start_at(fake_jira):
    p1 = fake_jira.search_issues(JQL, 0, 3, True)
    p2 = fake_jira.search_issues(JQL, 3, 3, True)
    assert len(p1.issues) == 3
    assert p1.total == p2.total
    assert {i["id"] for i in p1.issues}.isdisjoint({i["id"] for i in p2.issues})


def test_search_orders_by_updated_ascending(fake_jira):
    page = fake_jira.search_issues(JQL, 0, 100, True)
    updated = [i["fields"]["updated"] for i in page.issues]
    assert updated == sorted(updated)


def test_search_filters_by_updated_watermark(fake_jira):
    everything = fake_jira.search_issues(JQL, 0, 100, True)
    cutoff = everything.issues[-1]["fields"]["updated"][:16]
    page = fake_jira.search_issues(
        f'project = PROJ AND updated >= "{cutoff}" ORDER BY updated ASC', 0, 100, True
    )
    assert 0 < len(page.issues) < everything.total


def test_server_may_shrink_max_results(fixture_dir):
    """A7: 요청 100인데 서버가 2로 줄여 응답할 수 있다."""
    client = FakeJiraClient(fixture_dir, server_max_results=2)
    page = client.search_issues(JQL, 0, 100, True)
    assert page.max_results == 2
    assert len(page.issues) <= 2


def test_changelog_over_limit_is_truncated_inline(fake_jira):
    page = fake_jira.search_issues(JQL, 0, 100, True)
    big = [i for i in page.issues
           if i["changelog"]["total"] > i["changelog"]["maxResults"]]
    assert big, "fixture must contain an issue with >100 changelog entries"
    assert len(big[0]["changelog"]["histories"]) == big[0]["changelog"]["maxResults"]


def test_get_issue_changelog_continues_from_start_at(fake_jira):
    page = fake_jira.search_issues(JQL, 0, 100, True)
    big = next(i for i in page.issues if i["changelog"]["total"] > 100)
    rest = fake_jira.get_issue_changelog(big["key"], start_at=100)
    assert rest.start_at == 100
    assert rest.total == big["changelog"]["total"]
    assert len(rest.histories) > 0


def test_mutate_before_page_hook_runs_once(fake_jira):
    seen = []

    def bump(issues):
        issues[0]["fields"]["updated"] = "2099-01-01T00:00:00.000+0900"
        seen.append(True)

    fake_jira.mutate_before_page(2, bump)
    fake_jira.search_issues(JQL, 0, 2, True)
    fake_jira.search_issues(JQL, 2, 2, True)
    assert seen == [True]


def test_fail_on_call_raises_transient(fake_jira):
    fake_jira.fail_on_call(1, 429)
    with pytest.raises(JiraTransientError) as exc:
        fake_jira.search_issues(JQL, 0, 2, True)
    assert exc.value.status == 429


def test_moved_issue_leaves_source_project(fake_jira):
    before = fake_jira.search_issues(JQL, 0, 100, True)
    target = before.issues[0]
    fake_jira.move_issue(str(target["id"]), "10001", "OTHER-99", whitelisted=True)
    after = fake_jira.search_issues(JQL, 0, 100, True)
    assert str(target["id"]) not in {str(i["id"]) for i in after.issues}
    moved = fake_jira.get_issue(str(target["id"]), ["project"])
    assert moved["fields"]["project"]["id"] == "10001"


def test_deleted_issue_returns_none(fake_jira):
    page = fake_jira.search_issues(JQL, 0, 100, True)
    victim = str(page.issues[0]["id"])
    fake_jira.delete_issue(victim)
    assert fake_jira.get_issue(victim, ["project"]) is None


def test_moved_out_issue_is_still_resolvable(fake_jira):
    page = fake_jira.search_issues(JQL, 0, 100, True)
    target = str(page.issues[0]["id"])
    fake_jira.move_issue(target, "99999", "ARCHIVE-1", whitelisted=False)
    assert fake_jira.get_issue(target, ["project"]) is not None
