from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SearchPage:
    start_at: int
    max_results: int       # 서버가 실제로 적용한 값 (요청값이 아니다 — A7)
    total: int
    issues: list[dict]


@dataclass(frozen=True)
class ChangelogPage:
    start_at: int
    max_results: int
    total: int
    histories: list[dict]


class JiraTransientError(RuntimeError):
    """429/503 등 재시도 가능한 오류."""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(f"HTTP {status} {message}".strip())
        self.status = status


class JiraAuthError(RuntimeError):
    """401/403 — 재시도하지 않고 즉시 중단한다."""


class JiraClient(Protocol):
    def get_fields(self) -> list[dict]: ...
    def get_projects(self) -> list[dict]: ...
    def get_statuses(self) -> list[dict]: ...
    def search_issues(self, jql: str, start_at: int, max_results: int,
                      expand_changelog: bool) -> SearchPage: ...
    def get_issue_changelog(self, issue_key: str, start_at: int) -> ChangelogPage: ...
    def get_issue(self, jira_issue_id: str, fields: list[str]) -> dict | None: ...
