from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def ddl_dir() -> Path:
    return Path(__file__).parents[2] / "jira_dashboard" / "db" / "ddl"
