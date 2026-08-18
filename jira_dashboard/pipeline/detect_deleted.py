import logging
from dataclasses import dataclass

from jira_dashboard.db.repository.catalog import enabled_projects, project_id_by_jira_id
from jira_dashboard.jira.protocol import JiraClient
from jira_dashboard.pipeline.sync_issues import build_jql, iter_search_pages

log = logging.getLogger(__name__)

SELECT_UNDELETED = """
SELECT jira_issue_id, issue_id FROM test_jira_issue
WHERE  project_id = :project_id AND deleted_at IS NULL
"""

MARK_DELETED = """
UPDATE test_jira_issue
SET    deleted_at = SYS_EXTRACT_UTC(SYSTIMESTAMP), delete_reason = :reason
WHERE  issue_id = :issue_id
"""

RELOCATE_ISSUE = """
UPDATE test_jira_issue
SET    project_id = :project_id, issue_key = :issue_key,
       deleted_at = NULL, delete_reason = NULL
WHERE  issue_id = :issue_id
"""


@dataclass(frozen=True)
class DeleteVerdict:
    jira_issue_id: str
    issue_id: int
    reason: str            # DELETED | MOVED_OUT | MOVED_IN


def live_issue_ids(client: JiraClient, project_key: str) -> set[str]:
    """fields=id 로 전체 id만 가볍게 훑는다. maxResults는 응답값을 믿는다 (A7).

    페이징은 sync_issues.iter_search_pages를 그대로 쓴다. 여기서 따로 구현했던
    루프에는 100줄 옆에 있던 무진행 가드가 빠져 있었다 — max_results=0인데 issues가
    비어있지 않은 응답이 오면 start_at이 전진하지 못해 영원히 돈다 (Item 12).
    """
    seen: set[str] = set()
    for page in iter_search_pages(client, build_jql(project_key, None), 1000,
                                  expand_changelog=False):
        seen.update(str(i["id"]) for i in page.issues)
    return seen


def classify(raw: dict | None, whitelist: dict[str, int]) -> str:
    """후보를 바로 지우지 않는다. 삭제와 이동은 대응이 다르다 (spec §3.3.4)."""
    if raw is None:
        return "DELETED"
    project_jira_id = str((raw.get("fields", {}).get("project") or {}).get("id", ""))
    return "MOVED_IN" if project_jira_id in whitelist else "MOVED_OUT"


def load_undeleted(conn, project_id: int) -> dict[str, int]:
    cur = conn.cursor()
    cur.execute(SELECT_UNDELETED, project_id=project_id)
    return {jid: iid for jid, iid in cur.fetchall()}


def mark_deleted(conn, rows: list[dict]) -> None:
    if rows:
        conn.cursor().executemany(MARK_DELETED, rows, batcherrors=False)


def relocate_issue(conn, issue_id: int, project_id: int, issue_key: str) -> None:
    conn.cursor().execute(RELOCATE_ISSUE, issue_id=issue_id,
                          project_id=project_id, issue_key=issue_key)


def detect_deleted(conn, client: JiraClient, instance_id: int) -> list[DeleteVerdict]:
    whitelist = project_id_by_jira_id(conn, instance_id)
    verdicts: list[DeleteVerdict] = []

    for project_id, _, project_key in enabled_projects(conn, instance_id):
        live = live_issue_ids(client, project_key)
        for jira_issue_id, issue_id in load_undeleted(conn, project_id).items():
            if jira_issue_id in live:
                continue
            raw = client.get_issue(jira_issue_id, ["project"])
            reason = classify(raw, whitelist)
            verdicts.append(DeleteVerdict(jira_issue_id, issue_id, reason))
            if reason == "MOVED_IN":
                # 이동 직후 양쪽 워터마크 틈에 빠진 이슈. 여기서 잡지 않으면
                # Jira에는 있는데 대시보드에서 사라진다 (spec §5.6)
                target = str((raw["fields"]["project"]).get("id"))
                relocate_issue(conn, issue_id, whitelist[target], raw["key"])

    mark_deleted(conn, [
        {"issue_id": v.issue_id, "reason": v.reason}
        for v in verdicts if v.reason in ("DELETED", "MOVED_OUT")
    ])
    conn.commit()
    return verdicts
