import copy
import json
import re
from collections.abc import Callable
from pathlib import Path

from jira_dashboard.jira.protocol import ChangelogPage, JiraTransientError, SearchPage

_PROJECT_RE = re.compile(r'project\s*=\s*"?(\w+)"?', re.IGNORECASE)
_UPDATED_RE = re.compile(r'updated\s*>=\s*"([^"]+)"', re.IGNORECASE)


class FakeJiraClient:
    """픽스처 기반 JiraClient. JQL의 최소 부분집합만 해석한다.

    지원: `project = X` / `project = "X"`, `updated >= "..."`, `ORDER BY updated ASC`
    그 외 절은 무시한다. 실제 JQL 파서 동작은 사내에서만 검증된다 (spec §11.8).
    """

    def __init__(self, fixture_dir: Path, *, server_max_results: int = 100,
                 changelog_inline_limit: int = 100) -> None:
        self._dir = Path(fixture_dir)
        self._server_max_results = server_max_results
        self._inline_limit = changelog_inline_limit
        self._fields = self._load("fields.json")
        self._projects = self._load("projects.json")
        self._statuses = self._load("statuses.json")
        self._issues = {str(i["id"]): i for i in self._load("issues.json")}
        self._deleted: set[str] = set()
        self._call_count = 0
        self._failures: dict[int, int] = {}
        self._page_hooks: dict[int, Callable[[list[dict]], None]] = {}

    def _load(self, name: str):
        return json.loads((self._dir / name).read_text(encoding="utf-8"))

    # ---- 시나리오 훅 (spec §7.2) ------------------------------------
    def fail_on_call(self, call_number: int, status: int) -> None:
        self._failures[call_number] = status

    def mutate_before_page(self, call_number: int,
                           fn: Callable[[list[dict]], None]) -> None:
        self._page_hooks[call_number] = fn

    def move_issue(self, jira_issue_id: str, project_jira_id: str,
                   new_key: str, *, whitelisted: bool) -> None:
        issue = self._issues[jira_issue_id]
        issue["key"] = new_key
        issue["fields"]["project"] = {"id": project_jira_id,
                                      "key": new_key.split("-")[0]}
        issue["fields"]["updated"] = "2099-01-01T00:00:00.000+0900"
        issue["_whitelisted"] = whitelisted

    def delete_issue(self, jira_issue_id: str) -> None:
        self._deleted.add(jira_issue_id)

    def truncate_changelog(self, jira_issue_id: str, keep: int) -> None:
        cl = self._issues[jira_issue_id]["changelog"]
        cl["histories"] = cl["histories"][:keep]

    # ---- JiraClient ------------------------------------------------
    def _tick(self) -> int:
        self._call_count += 1
        status = self._failures.pop(self._call_count, None)
        if status is not None:
            raise JiraTransientError(status)
        return self._call_count

    def get_fields(self) -> list[dict]:
        self._tick()
        return copy.deepcopy(self._fields)

    def get_projects(self) -> list[dict]:
        self._tick()
        return copy.deepcopy(self._projects)

    def get_statuses(self) -> list[dict]:
        self._tick()
        return copy.deepcopy(self._statuses)

    def search_issues(self, jql: str, start_at: int, max_results: int,
                      expand_changelog: bool) -> SearchPage:
        call = self._tick()
        effective = min(max_results, self._server_max_results)

        m = _PROJECT_RE.search(jql)
        project_key = m.group(1) if m else None
        m = _UPDATED_RE.search(jql)
        since = m.group(1) if m else None

        rows = [
            i for i in self._issues.values()
            if str(i["id"]) not in self._deleted
            and (project_key is None or i["fields"]["project"]["key"] == project_key)
            and (since is None or i["fields"]["updated"][:len(since)] >= since)
        ]
        rows.sort(key=lambda i: (i["fields"]["updated"], str(i["id"])))

        hook = self._page_hooks.pop(call, None)
        if hook is not None:
            hook(rows)
            rows.sort(key=lambda i: (i["fields"]["updated"], str(i["id"])))

        window = copy.deepcopy(rows[start_at:start_at + effective])
        for issue in window:
            issue.pop("_whitelisted", None)
            if expand_changelog:
                cl = issue.get("changelog") or {"total": 0, "histories": []}
                histories = cl.get("histories", [])
                issue["changelog"] = {
                    "startAt": 0,
                    "maxResults": self._inline_limit,
                    "total": cl.get("total", len(histories)),
                    "histories": histories[:self._inline_limit],
                }
            else:
                issue.pop("changelog", None)
        return SearchPage(start_at, effective, len(rows), window)

    def get_issue_changelog(self, issue_key: str, start_at: int) -> ChangelogPage:
        self._tick()
        issue = next(i for i in self._issues.values() if i["key"] == issue_key)
        cl = issue.get("changelog") or {"total": 0, "histories": []}
        histories = cl.get("histories", [])
        total = cl.get("total", len(histories))
        window = histories[start_at:start_at + self._inline_limit]
        return ChangelogPage(start_at, self._inline_limit, total,
                             copy.deepcopy(window))

    def get_issue(self, jira_issue_id: str, fields: list[str]) -> dict | None:
        self._tick()
        if jira_issue_id in self._deleted:
            return None
        issue = self._issues.get(jira_issue_id)
        if issue is None:
            return None
        return copy.deepcopy({
            "id": issue["id"],
            "key": issue["key"],
            "fields": {k: v for k, v in issue["fields"].items() if k in fields},
        })
