import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from jira_dashboard.db.repository import history as history_repo
from jira_dashboard.db.repository import issue as issue_repo
from jira_dashboard.db.repository.catalog import (
    field_pk_by_field_id, field_pk_by_field_name,
)
from jira_dashboard.jira.models import KST
from jira_dashboard.jira.parser import parse_field_defs, parse_issue
from jira_dashboard.jira.protocol import JiraClient

log = logging.getLogger(__name__)

EPOCH = datetime(1970, 1, 1, tzinfo=KST)
OVERLAP = timedelta(minutes=5)


@dataclass
class SyncResult:
    fetched: int = 0
    upserted: int = 0
    skipped: int = 0
    parse_failures: int = 0
    changelog_truncated: int = 0
    max_updated: datetime | None = None
    changed_issue_ids: list[int] = field(default_factory=list)


def build_jql(project_key: str, since: datetime | None) -> str:
    """프로젝트 키를 따옴표로 감싼다. Jira의 키 문자집합(대문자+숫자)에서는 없어도
    되지만, 식별자를 인용 없이 문장에 끼워 넣는 습관 자체를 남기지 않는다.

    JQL의 오프셋 없는 날짜/시각 리터럴은 Jira 서버에 설정된 기본 타임존으로
    해석된다 — 여기 찍히는 문자열은 KST 벽시계 값이므로, 이는 Jira 인스턴스의
    기본 타임존이 Asia/Seoul일 때만 올바르다 (spec §2.1, A3처럼 사내 확인이
    필요한 전제 — docs/api-verification.md A13, 자동 검사는 아니고 관리자 화면에서
    육안 확인한다)."""
    start = (since or EPOCH).strftime("%Y-%m-%d %H:%M")
    return (f'project = "{project_key}" AND updated >= "{start}" '
            "ORDER BY updated ASC")


def next_watermark(max_updated: datetime | None,
                   previous: datetime | None) -> datetime | None:
    """다음 시작점 = 이번 최대 updated - 5분 (의도적 중복 구간, spec §5.2)."""
    if max_updated is None:
        return previous
    return max_updated - OVERLAP


def iter_search_pages(client: JiraClient, jql: str, page_size: int,
                      *, expand_changelog: bool = True):
    """startAt 페이징의 유일한 구현. detect_deleted도 이걸 쓴다 — 두 번째 구현은
    무진행 가드를 빠뜨렸다 (Item 12)."""
    start_at = 0
    while True:
        page = client.search_issues(jql, start_at, page_size,
                                    expand_changelog)
        if not page.issues:
            return
        yield page
        if page.max_results <= 0:
            # max_results가 0인데 issues는 비어있지 않은 응답 — 전진할 수 없어
            # 같은 페이지를 영원히 재요청하게 된다. C2와 같은 무진행 루프 계열이다.
            log.warning("search_issues returned max_results=%d with issues present; "
                        "stopping paging to avoid a no-progress loop", page.max_results)
            return
        # 요청값이 아니라 서버가 응답한 max_results로 전진한다 (A7)
        start_at += page.max_results
        if start_at >= page.total:
            return


def _full_changelog(client: JiraClient, raw_issue: dict) -> tuple[list[dict], bool]:
    """Collect as much changelog as the API will give. Returns (histories, truncated).

    DC 10.3 offers no way to page changelog: /issue/{key} takes no startAt and there is
    no dedicated changelog endpoint. So this may legitimately return fewer than `total`,
    and the no-progress guard below is what keeps it from looping on a server that
    re-serves the same slice. (Correction 2)
    """
    cl = raw_issue.get("changelog") or {}
    histories = list(cl.get("histories") or [])
    total = int(cl.get("total", len(histories)))
    seen = {str(h["id"]) for h in histories}
    while len(histories) < total:
        page = client.get_issue_changelog(raw_issue["key"], len(histories))
        fresh = [h for h in page.histories if str(h["id"]) not in seen]
        if not fresh:
            break
        seen.update(str(h["id"]) for h in fresh)
        histories.extend(fresh)
    truncated = len(histories) < total
    if truncated:
        log.warning("changelog truncated for %s: collected %d of %d entries",
                    raw_issue["key"], len(histories), total)
    return histories, truncated


def sync_issues(conn, client: JiraClient, instance_id: int, project_id: int,
                project_key: str, since: datetime | None,
                *, page_size: int = 100, dry_run: bool = False,
                full_resync: bool = False) -> SyncResult:
    """dry_run=True면 커밋하지 않는다 — 쓰기는 그대로 실행하고 호출자가 롤백한다.

    full_resync=True면 payload_hash 비교를 건너뛴다. 해시 스킵은 "payload가 그대로니
    하위 단계도 다시 할 필요가 없다"는 주장인데, 그건 하위 단계(derive_history)가
    실제로 성공했을 때만 참이다. 프로젝트가 실패하면 러너가 full_resync_requested를
    'Y'로 올리고, 그 다음 실행이 이 플래그로 해시를 우회해 이력을 다시 만든다 (R31).
    """
    if page_size > 1000:
        # load_existing이 jira_issue_id IN (:b0...)로 페이지 전체를 한 번에 조회한다.
        # Oracle IN 리스트 상한이 표현식 1000개이므로(ORA-01795) 그 위는 즉시 깨진다.
        raise ValueError(
            f"page_size must be <= 1000 (Oracle IN-list limit); got {page_size}"
        )
    field_index = {fd.field_id: fd for fd in parse_field_defs(client.get_fields())}
    category_of = {s["name"]: s["statusCategory"]["key"] for s in client.get_statuses()}
    field_pks = field_pk_by_field_id(conn, instance_id)
    field_names = field_pk_by_field_name(conn, instance_id)

    result = SyncResult()
    jql = build_jql(project_key, since)
    unresolved_field_names: set[str] = set()

    for page in iter_search_pages(client, jql, page_size):
        result.fetched += len(page.issues)
        jira_ids = [str(i["id"]) for i in page.issues]

        # ① 기존 상태를 먼저 읽는다
        existing = issue_repo.load_existing(conn, instance_id, jira_ids)

        # ② 해시로 분류
        pending, unchanged_ids = [], []
        for raw in page.issues:
            jid = str(raw["id"])
            cl = raw.get("changelog") or {}
            histories, truncated = _full_changelog(client, raw)
            raw["changelog"] = {**cl, "histories": histories}
            if truncated:
                result.changelog_truncated += 1
            raw_bytes = issue_repo.canonical_json(raw)
            digest = issue_repo.sha256_hex(raw_bytes)
            payload = issue_repo.gzip_bytes(raw_bytes)
            prior = existing.get(jid)
            if (not full_resync and prior is not None
                    and prior.payload_hash == digest):
                unchanged_ids.append(prior.issue_id)
                continue
            pending.append((raw, payload, digest, prior))

        issue_repo.touch_synced_at(conn, unchanged_ids)
        result.skipped += len(unchanged_ids)

        # 파싱을 먼저 해서 실패한 것을 걸러낸 뒤 채번한다 (spec §5.8, Correction 4)
        parsed_rows = []
        for raw, payload, digest, prior in pending:
            try:
                parsed = parse_issue(raw, field_index, category_of)
            except Exception:
                log.exception("failed to parse issue %s; skipping", raw.get("key"))
                result.parse_failures += 1
                continue
            parsed_rows.append((raw, payload, digest, prior, parsed))

        new_count = sum(1 for *_, prior, _ in parsed_rows if prior is None)
        fresh_ids = iter(issue_repo.next_issue_ids(conn, new_count))

        issue_rows, raw_rows, cl_raw_rows = [], [], []
        eav_by_issue: dict[int, list[dict]] = {}
        changelog_by_issue: dict[int, list] = {}

        for raw, payload, digest, prior, parsed in parsed_rows:
            issue_id = prior.issue_id if prior is not None else next(fresh_ids)

            issue_rows.append({
                "issue_id": issue_id, "instance_id": instance_id,
                "project_id": project_id, "jira_issue_id": parsed.jira_issue_id,
                "issue_key": parsed.issue_key,
                "issue_type_name": parsed.issue_type_name,
                "status_name": parsed.status_name,
                "status_category": parsed.status_category,
                "priority_name": parsed.priority_name,
                "resolution_name": parsed.resolution_name,
                "assignee_user_key": parsed.assignee_user_key,
                "assignee_display_name": parsed.assignee_display_name,
                "reporter_user_key": parsed.reporter_user_key,
                "reporter_display_name": parsed.reporter_display_name,
                "parent_key": parsed.parent_key, "summary": parsed.summary,
                "created_at": parsed.created_at, "updated_at": parsed.updated_at,
                "resolved_at": parsed.resolved_at, "due_date": parsed.due_date,
                "original_estimate_sec": parsed.original_estimate_sec,
                "remaining_estimate_sec": parsed.remaining_estimate_sec,
                "time_spent_sec": parsed.time_spent_sec,
            })
            raw_rows.append({"issue_id": issue_id, "payload": payload,
                             "payload_hash": digest})
            cl_bytes = issue_repo.canonical_json(raw.get("changelog") or {})
            cl_raw_rows.append({
                "issue_id": issue_id, "payload": issue_repo.gzip_bytes(cl_bytes),
                "payload_hash": issue_repo.sha256_hex(cl_bytes),
            })
            eav_by_issue[issue_id] = [
                {"issue_id": issue_id, "field_pk": field_pks[v.field_id],
                 "val_seq": v.val_seq, "val_str": v.val_str, "val_num": v.val_num,
                 "val_date": v.val_date, "val_id": v.val_id}
                for v in parsed.custom_values if v.field_id in field_pks
            ]
            changelog_by_issue[issue_id] = list(parsed.changelog)

            if result.max_updated is None or parsed.updated_at > result.max_updated:
                result.max_updated = parsed.updated_at

        # ③ 이슈 → ④ raw → ⑤ EAV → ⑥ changelog. FK 때문에 순서를 바꿀 수 없다.
        if issue_rows:
            issue_repo.upsert_issues(conn, issue_rows)
            issue_repo.upsert_raw(conn, "test_issue_raw", raw_rows)
            issue_repo.upsert_raw(conn, "test_changelog_raw", cl_raw_rows)
        for issue_id, values in eav_by_issue.items():
            issue_repo.replace_field_values(conn, issue_id, values)
        for issue_id, items in changelog_by_issue.items():
            unresolved = history_repo.upsert_changelog(
                conn, issue_id, items, field_pks, field_names
            )
            unresolved_field_names |= (unresolved or set())

        result.upserted += len(issue_rows)
        result.changed_issue_ids.extend(r["issue_id"] for r in issue_rows)
        if not dry_run:
            conn.commit()

    if unresolved_field_names:
        # 개별 행마다 찍으면 이슈 10만 건에서 로그가 터진다 — 서로 다른 필드 이름만
        # 한 번 모아 찍는다. 온프레미스 첫 실행이 이 로그로 어떤 필드에 별칭 매핑이
        # 필요한지 정확히 알려준다 (Correction 3의 사후 가시성).
        log.warning(
            "changelog field_pk unresolved for %d distinct field name(s): %s",
            len(unresolved_field_names), sorted(unresolved_field_names),
        )

    return result
