import hashlib
import json
import logging
from pathlib import Path

from jira_dashboard.jira.protocol import JiraClient

log = logging.getLogger(__name__)


def _pseudonym(prefix: str, value: str | None) -> str | None:
    if value is None:
        return None
    return f"{prefix}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:8]}"


def _anonymize_issue(issue: dict) -> dict:
    fields = issue.get("fields") or {}
    fields["summary"] = _pseudonym("issue", issue.get("key"))
    for role in ("assignee", "reporter", "creator"):
        user = fields.get(role)
        if isinstance(user, dict):
            key = user.get("key") or user.get("name")
            user["displayName"] = _pseudonym("user", key)
            user["key"] = _pseudonym("key", key)
            user["name"] = user["key"]
    for history in (issue.get("changelog") or {}).get("histories") or []:
        author = history.get("author")
        if isinstance(author, dict):
            key = author.get("key") or author.get("name")
            author["displayName"] = _pseudonym("user", key)
            author["key"] = _pseudonym("key", key)
    return issue


def capture_fixtures(client: JiraClient, project_key: str, out_dir: Path,
                     *, limit: int = 200, anonymize: bool = False) -> dict[str, int]:
    """읽기 전용. 사내에서 실행하고 사내에만 남긴다.

    anonymize는 기본값이 꺼져 있다 — 반출이 금지된 상황에서 익명화 옵션이 켜져 있으면
    "익명화했으니 가져가도 되겠지"라는 판단을 부른다. 이 옵션은 사내 보관 데이터의
    가독성 조절용이지 반출 허가가 아니다 (spec §11.6).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def dump(name: str, payload) -> int:
        (out_dir / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return len(payload)

    counts = {
        "fields": dump("fields.json", client.get_fields()),
        "projects": dump("projects.json", client.get_projects()),
        "statuses": dump("statuses.json", client.get_statuses()),
    }

    jql = f"project = {project_key} ORDER BY updated ASC"
    issues, start_at = [], 0
    while len(issues) < limit:
        page = client.search_issues(jql, start_at, 100, True)
        if not page.issues:
            break
        for issue in page.issues:
            issues.append(_anonymize_issue(issue) if anonymize else issue)
        start_at += page.max_results
        if start_at >= page.total:
            break

    issues = issues[:limit]

    oversized = any(
        (issue.get("changelog") or {}).get("total", 0)
        > (issue.get("changelog") or {}).get("maxResults", 100)
        for issue in issues
    )
    if not oversized:
        log.warning(
            "no captured issue has changelog.total > maxResults — "
            "the supplemental-fetch path stays untested against real data"
        )

    counts["issues"] = dump("issues.json", issues)
    return counts
