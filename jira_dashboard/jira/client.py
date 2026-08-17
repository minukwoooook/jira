"""사내 Jira Data Center 10.3에 실호출하는 JiraClient 구현체.

FakeJiraClient와 동일한 시그니처를 지킨다 (jira/protocol.py의 JiraClient) — doctor,
sync, capture 모두 클라이언트 구현을 몰라도 되게 하기 위해서다.
"""
import os
import time

import httpx

from jira_dashboard.jira.protocol import (
    ChangelogPage, JiraAuthError, JiraTransientError, SearchPage,
)

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 5


class HttpJiraClient:
    def __init__(self, base_url: str, token: str, *, auth_type: str = "PAT",
                 timeout: float = 60.0) -> None:
        scheme = "Bearer" if auth_type == "PAT" else "Basic"
        self._c = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"{scheme} {token}"},
            timeout=timeout,
        )

    def __repr__(self) -> str:
        # 토큰은 httpx.Client 헤더 안에만 있고, 여기서는 절대 노출하지 않는다.
        return f"HttpJiraClient(base_url={self._c.base_url!r})"

    @classmethod
    def from_config(cls, base_url: str, auth_type: str, secret_ref: str):
        token = os.environ.get(secret_ref)
        if not token:
            raise JiraAuthError(f"environment variable {secret_ref} is not set")
        return cls(base_url, token, auth_type=auth_type)

    def _request(self, method: str, path: str, **kw):
        for attempt in range(1, MAX_ATTEMPTS + 1):
            resp = self._c.request(method, path, **kw)
            if resp.status_code in (401, 403):
                raise JiraAuthError(f"HTTP {resp.status_code} on {path}")
            if resp.status_code == 404:
                return None
            if resp.status_code in RETRY_STATUSES:
                if attempt == MAX_ATTEMPTS:
                    raise JiraTransientError(resp.status_code, path)
                time.sleep(min(2 ** attempt, 30))
                continue
            resp.raise_for_status()
            return resp.json()
        raise JiraTransientError(0, "unreachable")

    def get_fields(self) -> list[dict]:
        return self._request("GET", "/rest/api/2/field") or []

    def get_projects(self) -> list[dict]:
        return self._request("GET", "/rest/api/2/project") or []

    def get_statuses(self) -> list[dict]:
        return self._request("GET", "/rest/api/2/status") or []

    def search_issues(self, jql: str, start_at: int, max_results: int,
                      expand_changelog: bool) -> SearchPage:
        body = {"jql": jql, "startAt": start_at, "maxResults": max_results,
                "fields": ["*all"]}
        if expand_changelog:
            body["expand"] = ["changelog"]   # renderedFields는 넣지 않는다 (응답이 배로 커짐)
        data = self._request("POST", "/rest/api/2/search", json=body) or {}
        return SearchPage(
            start_at=data.get("startAt", start_at),
            max_results=data.get("maxResults", max_results),   # 서버 응답값을 믿는다 (A7)
            total=data.get("total", 0),
            issues=data.get("issues", []),
        )

    def get_issue_changelog(self, issue_key: str, start_at: int) -> ChangelogPage:
        """DC 10.3에는 changelog 전용 엔드포인트도, `/issue/{key}`의 `startAt`
        파라미터도 없다 (공개 스펙으로 확인됨 — docs/api-verification.md A3). 그래서
        여기는 진짜 서버측 페이징이 아니다: 매번 전체 인라인 changelog를 다시 받아
        클라이언트에서 슬라이스할 뿐이다.

        인라인 상한이 실제로 몇 건인지, 그리고 `start_at`을 바꿔 같은 이슈를 다시
        불러도 서버가 매번 "처음 N건"만 주는지(그러면 2차 호출은 새 항목을 전혀
        못 준다) 아니면 다른 창을 주는지는 여기서 가정하지 않는다 — 그게 바로
        `doctor --jira`의 A3 체크(jira_dashboard/doctor/jira_checks.py)가 실측하는
        사실이다. A3가 FAIL(2차 호출이 새 항목을 안 줌)로 나온다면, changelog가
        100건을 넘는 이슈는 이 메서드로 나머지를 절대 가져올 수 없다는 뜻이고
        sync_issues의 보충 호출 로직(spec §5.2)은 데이터 유실을 전제로 재설계해야
        한다 — 이 메서드 자체를 고쳐서 될 문제가 아니다(전용 엔드포인트가 없으므로).
        """
        data = self._request(
            "GET", f"/rest/api/2/issue/{issue_key}",
            params={"expand": "changelog", "fields": "id"},
        ) or {}
        cl = data.get("changelog") or {}
        histories = cl.get("histories") or []
        return ChangelogPage(
            start_at=start_at,
            max_results=cl.get("maxResults", len(histories)),
            total=cl.get("total", len(histories)),
            histories=histories[start_at:],
        )

    def get_issue(self, jira_issue_id: str, fields: list[str]) -> dict | None:
        return self._request("GET", f"/rest/api/2/issue/{jira_issue_id}",
                             params={"fields": ",".join(fields)})
