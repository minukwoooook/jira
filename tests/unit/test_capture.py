import json

from jira_dashboard.capture import capture_fixtures


def test_writes_all_four_fixture_files(fake_jira, tmp_path):
    counts = capture_fixtures(fake_jira, "PROJ", tmp_path, limit=100)
    for name in ("fields.json", "projects.json", "statuses.json", "issues.json"):
        assert (tmp_path / name).exists()
    assert counts["issues"] > 0


def test_captured_fixtures_drive_the_same_fake_client(fake_jira, tmp_path):
    """사외 픽스처와 사내 픽스처에 같은 테스트를 돌릴 수 있어야 한다 (spec §11.3)."""
    from jira_dashboard.jira.fake import FakeJiraClient

    capture_fixtures(fake_jira, "PROJ", tmp_path, limit=100)
    replayed = FakeJiraClient(tmp_path)
    original = fake_jira.search_issues("project = PROJ ORDER BY updated ASC", 0, 100, True)
    copy = replayed.search_issues("project = PROJ ORDER BY updated ASC", 0, 100, True)
    assert copy.total == original.total


def test_includes_an_issue_with_oversized_changelog(fake_jira, tmp_path):
    """보충 호출 경로를 사내 픽스처로도 테스트할 수 있어야 한다."""
    capture_fixtures(fake_jira, "PROJ", tmp_path, limit=100)
    issues = json.loads((tmp_path / "issues.json").read_text(encoding="utf-8"))
    assert any(i["changelog"]["total"] > 100 for i in issues)


def test_anonymize_replaces_user_keys_and_summaries(fake_jira, tmp_path):
    capture_fixtures(fake_jira, "PROJ", tmp_path, limit=100, anonymize=True)
    issues = json.loads((tmp_path / "issues.json").read_text(encoding="utf-8"))
    for issue in issues:
        assert issue["fields"]["summary"].startswith("issue-")


def test_anonymize_is_off_by_default(fake_jira, tmp_path):
    """반출 금지 상황에서 기본 익명화는 '가져가도 되겠지'를 부른다 (spec §11.6)."""
    capture_fixtures(fake_jira, "PROJ", tmp_path, limit=100)
    issues = json.loads((tmp_path / "issues.json").read_text(encoding="utf-8"))
    assert not issues[0]["fields"]["summary"].startswith("issue-")


def test_respects_limit(fake_jira, tmp_path):
    counts = capture_fixtures(fake_jira, "PROJ", tmp_path, limit=3)
    assert counts["issues"] == 3


def test_warns_when_no_oversized_changelog_captured(fake_jira, tmp_path, caplog):
    """스펙상 위험한 보충 호출 경로가 실데이터로 검증되지 않으면 경고해야 한다."""
    import logging

    with caplog.at_level(logging.WARNING, logger="jira_dashboard.capture"):
        capture_fixtures(fake_jira, "PROJ", tmp_path, limit=3)
    assert any("changelog" in r.message for r in caplog.records)
