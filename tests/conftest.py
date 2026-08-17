import os
from pathlib import Path

import pytest

from jira_dashboard.jira import parser
from jira_dashboard.jira.fake import FakeJiraClient

# 사내에서는 JIRA_FIXTURES=captured 로 같은 스위트를 실데이터에 돌린다 (spec §11.3)
_NAME = os.environ.get("JIRA_FIXTURES", "synthetic")


@pytest.fixture(scope="session")
def fixture_dir() -> Path:
    return Path(__file__).parent / "fixtures" / _NAME


@pytest.fixture
def fake_jira(fixture_dir) -> FakeJiraClient:
    return FakeJiraClient(fixture_dir)


@pytest.fixture
def field_index(fake_jira):
    return {fd.field_id: fd for fd in parser.parse_field_defs(fake_jira.get_fields())}


@pytest.fixture
def category_of(fake_jira):
    return {s["name"]: s["statusCategory"]["key"] for s in fake_jira.get_statuses()}


@pytest.fixture
def sample_issue(fake_jira):
    page = fake_jira.search_issues("project = PROJ ORDER BY updated ASC", 0, 100, True)
    return page.issues[0]
