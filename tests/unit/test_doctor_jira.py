from jira_dashboard.doctor.jira_checks import run_jira_checks
from jira_dashboard.jira.protocol import SearchPage


def _by_id(results): return {r.id: r for r in results}


def test_fake_client_passes_every_assumption(fake_jira):
    """Fake는 A1~A12 가정을 그대로 구현했으므로 FAIL은 하나도 없어야 한다 —
    A1/A10처럼 이 작은 synthetic fixture로는 실측이 안 되는 항목은 정직하게
    WARN을 내는 게 맞고(증거 없이 PASS를 주지 않는다), FAIL은 절대 안 된다.
    사내에서 FAIL이 나오면 그것이 정확히 가정이 깨진 지점이다."""
    failures = [r for r in run_jira_checks(fake_jira, "PROJ") if r.verdict == "FAIL"]
    assert failures == [], [(r.id, r.observed) for r in failures]


def test_detects_descending_changelog(fake_jira):
    """A4가 뒤집힌 상황. 구간 테이블이 통째로 틀리는 케이스다."""
    for issue in fake_jira._issues.values():
        cl = issue.get("changelog") or {}
        cl["histories"] = list(reversed(cl.get("histories") or []))
    assert _by_id(run_jira_checks(fake_jira, "PROJ"))["A4"].verdict == "FAIL"


def test_detects_missing_status_category_key(fake_jira):
    """A9가 깨지면 인스턴스 간 교차라는 설계 전제가 사라진다."""
    for s in fake_jira._statuses:
        s["statusCategory"].pop("key", None)
    assert _by_id(run_jira_checks(fake_jira, "PROJ"))["A9"].verdict == "FAIL"


def test_detects_missing_schema_in_field_response(fake_jira):
    for f in fake_jira._fields:
        f.pop("schema", None)
    assert _by_id(run_jira_checks(fake_jira, "PROJ"))["A8"].verdict == "FAIL"


def test_warns_on_shrunk_max_results(fixture_dir):
    from jira_dashboard.jira.fake import FakeJiraClient

    r = _by_id(run_jira_checks(FakeJiraClient(fixture_dir, server_max_results=50), "PROJ"))
    assert (r["A7"].observed, r["A7"].verdict) == ("50", "WARN")


def test_warns_when_field_id_is_absent_from_changelog(fake_jira):
    for issue in fake_jira._issues.values():
        for h in (issue.get("changelog") or {}).get("histories") or []:
            for item in h.get("items") or []:
                item.pop("fieldId", None)
    assert _by_id(run_jira_checks(fake_jira, "PROJ"))["A5"].verdict == "WARN"


# --- Addition 2: A3는 "필드가 있는지"가 아니라 "실측"이어야 한다 -----------------
# PROJ-7 fixture는 changelog.total=150인데 FakeJiraClient의 search_issues가
# maxResults를 changelog_inline_limit(기본 100)으로 다시 쓰기 때문에, search 응답
# 안에서 total(150) > maxResults(100)가 되는 유일한 이슈다 — A3 실험의 자연스러운
# 대상이다.

def test_a3_measures_total_maxresults_and_second_call_on_overflowing_issue(fake_jira):
    r = _by_id(run_jira_checks(fake_jira, "PROJ"))["A3"]
    assert r.verdict == "PASS"
    assert "PROJ-7" in r.observed
    assert "total=150" in r.observed
    assert "maxResults=100" in r.observed
    assert "second_call_new_entries=50" in r.observed


def test_a3_warns_plainly_when_no_project_issue_exceeds_the_limit(fake_jira):
    """A3를 실측할 대상이 없으면 조용히 PASS를 내면 안 된다 — 검증되지 않은 가정을
    통과로 보고하는 게 가장 나쁜 결과다."""
    for issue in fake_jira._issues.values():
        cl = issue.get("changelog")
        if cl:
            cl["total"] = min(cl.get("total", 0), 100)
    r = _by_id(run_jira_checks(fake_jira, "PROJ"))["A3"]
    assert r.verdict == "WARN"
    assert r.verdict != "PASS"


def test_a3_warns_when_second_call_reveals_nothing_new(fake_jira, monkeypatch):
    """서버가 매번 같은 첫 N건만 주는 상황 — 조사 결과(docs/api-verification.md A3)가
    예상한 바로 그 결과이고, 파이프라인은 이미 no-progress 가드 +
    changelog_truncated 카운터로 이를 처리하도록 설계돼 있다. 아무것도 고장나지
    않았으므로 FAIL이 아니라 WARN이어야 한다."""
    original = fake_jira.get_issue_changelog

    def _always_first_window(issue_key, start_at):
        return original(issue_key, 0)

    monkeypatch.setattr(fake_jira, "get_issue_changelog", _always_first_window)
    r = _by_id(run_jira_checks(fake_jira, "PROJ"))["A3"]
    assert r.verdict == "WARN"


# --- Fix round 1, item 3: A1은 "startAt=0이 행을 돌려주는지"가 아니라 "startAt이
# 실제로 다음 페이지로 전진하는지"를 봐야 한다. -------------------------------

def test_a1_warns_when_probe_project_fits_in_one_page(fake_jira):
    """synthetic fixture는 8개 이슈뿐이라 한 페이지(100)에 다 들어간다 — 페이징
    자체는 실측되지 않았으므로 PASS가 아니라 WARN이어야 한다."""
    r = _by_id(run_jira_checks(fake_jira, "PROJ"))["A1"]
    assert r.verdict == "WARN"


def test_a1_passes_when_second_page_is_disjoint_from_first(fixture_dir):
    from jira_dashboard.jira.fake import FakeJiraClient

    client = FakeJiraClient(fixture_dir, server_max_results=3)
    r = _by_id(run_jira_checks(client, "PROJ"))["A1"]
    assert r.verdict == "PASS"
    assert "disjoint=True" in r.observed


def test_a1_fails_when_server_ignores_start_at(fixture_dir, monkeypatch):
    """startAt을 무시하고 매번 같은 첫 페이지를 주는 서버를 흉내낸다 — 두 번째
    페이지가 첫 페이지와 완전히 겹치므로 FAIL이어야 한다."""
    from jira_dashboard.jira.fake import FakeJiraClient

    client = FakeJiraClient(fixture_dir, server_max_results=3)
    original = client.search_issues

    def _ignore_start_at(jql, start_at, max_results, expand_changelog):
        return original(jql, 0, max_results, expand_changelog)

    monkeypatch.setattr(client, "search_issues", _ignore_start_at)
    r = _by_id(run_jira_checks(client, "PROJ"))["A1"]
    assert r.verdict == "FAIL"


# --- Fix round 1, item 2: A10은 관측된 게 없으면 PASS를 주면 안 된다. ---------

def test_a10_warns_when_no_issue_has_a_reporter(fake_jira):
    """synthetic fixture의 어떤 이슈도 reporter를 채우지 않는다 — 관측 자체가
    없으므로 WARN이어야 한다 (예전 로직은 이걸 PASS로 잘못 판정했다)."""
    r = _by_id(run_jira_checks(fake_jira, "PROJ"))["A10"]
    assert r.verdict == "WARN"


def test_a10_passes_when_a_key_shaped_reporter_is_observed(fake_jira):
    some_issue = next(iter(fake_jira._issues.values()))
    some_issue["fields"]["reporter"] = {
        "key": "jdoe", "name": "jdoe", "displayName": "Jane Doe",
    }
    r = _by_id(run_jira_checks(fake_jira, "PROJ"))["A10"]
    assert r.verdict == "PASS"


def test_a10_warns_when_observed_reporter_looks_like_cloud_account_id(fake_jira):
    for issue in fake_jira._issues.values():
        issue["fields"]["reporter"] = {"accountId": "abc123"}
    r = _by_id(run_jira_checks(fake_jira, "PROJ"))["A10"]
    assert r.verdict == "WARN"


# --- Fix round 1, item 4: A12은 실패할 수 있어야 무언가를 증명한 것이다. --------

def test_a12_fails_and_short_circuits_when_credentials_are_rejected():
    """자격증명이 거부되면 A1~A11은 같은 이유로 전부 실패할 것이므로 실행을
    생략해야 한다 — 결과 목록에는 A12 FAIL 하나만 있어야 한다."""
    from jira_dashboard.jira.protocol import JiraAuthError

    class _RejectsCredentials:
        secret_ref = "JIRA_TEST_TOKEN"

        def get_fields(self):
            raise JiraAuthError("HTTP 401 on /rest/api/2/field")

        def get_statuses(self):
            raise AssertionError("A12 FAIL 이후 나머지 체크가 실행되면 안 된다")

        def get_projects(self):
            raise AssertionError("A12 FAIL 이후 나머지 체크가 실행되면 안 된다")

        def search_issues(self, *a, **k):
            raise AssertionError("A12 FAIL 이후 나머지 체크가 실행되면 안 된다")

        def get_issue_changelog(self, *a, **k):
            raise AssertionError("A12 FAIL 이후 나머지 체크가 실행되면 안 된다")

    results = run_jira_checks(_RejectsCredentials(), "PROJ")
    assert [r.id for r in results] == ["A12"]
    assert results[0].verdict == "FAIL"
    assert "JIRA_TEST_TOKEN" in results[0].impact


def test_every_result_carries_impact_text(fake_jira):
    for result in run_jira_checks(fake_jira, "PROJ"):
        assert result.impact, result.id


# --- R34: 관측하지 못한 것에는 PASS도 FAIL도 주지 않는다 -----------------------

class _NoIssuesClient:
    """카탈로그는 정상이지만 프로브 프로젝트에 이슈가 하나도 없는 인스턴스."""

    def get_fields(self):
        return [{"id": "summary", "name": "Summary", "schema": {"type": "string"}}]

    def get_projects(self):
        return [{"id": "10000", "key": "PROJ"}]

    def get_statuses(self):
        return [{"name": "Done", "statusCategory": {"key": "done"}}]

    def search_issues(self, jql, start_at, max_results, expand_changelog):
        return SearchPage(start_at=0, max_results=100, total=0, issues=[])

    def get_issue_changelog(self, key, start_at):
        raise AssertionError("불려서는 안 된다")

    def get_issue(self, jira_issue_id, fields):
        return None


def test_a4_warns_when_ordering_cannot_be_measured(fake_jira):
    """changelog 타임스탬프가 하나뿐이면 정렬은 판정할 수 없다 — FAIL이 아니라 WARN.
    FAIL로 매핑하면 "고칠 것이 없는데 고치라고" 말하는 셈이고, 런북 5단계에서
    사외로 돌려보낸다."""
    for issue in fake_jira._issues.values():
        histories = (issue.get("changelog") or {}).get("histories") or []
        for h in histories:
            h["created"] = "2026-01-05T09:00:00.000+0900"     # 전부 같은 시각
    r = _by_id(run_jira_checks(fake_jira, "PROJ"))
    assert r["A4"].verdict == "WARN"
    assert "측정" in r["A4"].observed


def test_a4_still_fails_when_descending_order_is_observed(fake_jira):
    """관측한 FAIL은 그대로 FAIL이어야 한다 — WARN 규칙이 진짜 결함을 덮으면 안 된다."""
    for issue in fake_jira._issues.values():
        histories = (issue.get("changelog") or {}).get("histories") or []
        if len(histories) >= 2:
            histories.sort(key=lambda h: h["created"], reverse=True)
    r = _by_id(run_jira_checks(fake_jira, "PROJ"))
    assert r["A4"].verdict == "FAIL"


def test_a2_and_a6_warn_instead_of_failing_when_no_issues_were_returned():
    """이슈가 0건이면 A1이 이미 그 사실을 FAIL로 보고한다. A2/A6까지 FAIL을 찍으면
    관측하지 않은 것에 판정을 준 것이고, 실패 3개가 원인 1개를 가린다."""
    r = _by_id(run_jira_checks(_NoIssuesClient(), "PROJ"))
    assert r["A1"].verdict == "FAIL"
    assert r["A2"].verdict == "WARN"
    assert r["A6"].verdict == "WARN"
    assert r["A4"].verdict == "WARN"
