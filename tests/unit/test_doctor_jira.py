from jira_dashboard.doctor.jira_checks import run_jira_checks


def _by_id(results): return {r.id: r for r in results}


def test_fake_client_passes_every_assumption(fake_jira):
    """Fake는 A1~A12 가정을 그대로 구현했으므로 전부 PASS여야 한다.
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


def test_a3_fails_when_second_call_reveals_nothing_new(fake_jira, monkeypatch):
    """서버가 매번 같은 첫 N건만 주는 상황(실제 DC 10.3이 이럴 가능성) — 이 경우
    changelog 100건 초과 이슈는 이 클라이언트로 절대 완전히 못 가져오므로 FAIL이어야
    한다."""
    original = fake_jira.get_issue_changelog

    def _always_first_window(issue_key, start_at):
        return original(issue_key, 0)

    monkeypatch.setattr(fake_jira, "get_issue_changelog", _always_first_window)
    r = _by_id(run_jira_checks(fake_jira, "PROJ"))["A3"]
    assert r.verdict == "FAIL"


def test_every_result_carries_impact_text(fake_jira):
    for result in run_jira_checks(fake_jira, "PROJ"):
        assert result.impact, result.id
