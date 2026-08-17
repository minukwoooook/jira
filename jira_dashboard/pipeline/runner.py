import logging
from dataclasses import dataclass, field

from jira_dashboard.db.repository import sync as sync_repo
from jira_dashboard.db.repository.catalog import enabled_projects
from jira_dashboard.jira.protocol import JiraAuthError
from jira_dashboard.pipeline.derive_history import derive_history, update_first_done_at
from jira_dashboard.pipeline.detect_deleted import detect_deleted
from jira_dashboard.pipeline.profile_fields import profile_fields
from jira_dashboard.pipeline.sync_catalog import sync_catalog
from jira_dashboard.pipeline.sync_issues import next_watermark, sync_issues

log = logging.getLogger(__name__)


@dataclass
class RunSummary:
    projects_ok: int = 0
    projects_failed: int = 0
    issues_upserted: int = 0
    parse_failures: int = 0
    changelog_truncated: int = 0
    errors: dict[str, str] = field(default_factory=dict)


def run_instance(conn, client, instance_id: int, *,
                 dry_run: bool = False, daily: bool = False) -> RunSummary:
    """인스턴스 내 프로젝트는 순차 처리한다. 병렬은 인스턴스 단위다 (spec §5.0)."""
    sync_repo.reclaim_zombies(conn)
    summary = RunSummary()

    run_id = sync_repo.start_run(conn, instance_id, None, "CATALOG")
    try:
        report = sync_catalog(conn, client, instance_id)
        # /rest/api/2/status는 인스턴스에 정의된 모든 상태를 담는다 — DB의
        # status_name은 이슈가 "현재" 쓰고 있는 상태만 보므로, 워크플로우에서
        # 이미 빠진 상태가 있으면 이력의 과거 구간이 "undefined"로 잘못 칠해진다.
        category_of = {s["name"]: s["statusCategory"]["key"]
                       for s in client.get_statuses()}
        conn.commit()
        sync_repo.finish_run(conn, run_id, "SUCCESS")
    except Exception as exc:
        conn.rollback()
        sync_repo.finish_run(conn, run_id, "FAILED", error=repr(exc))
        raise

    projects = enabled_projects(conn, instance_id)

    for project_id in report.key_changed_projects:
        log.warning("project key changed (project_id=%s); full resync queued", project_id)
        sync_repo.request_full_resync(conn, project_id)
    if report.value_kind_changed:
        log.warning("value_kind changed for %s; full resync queued for all projects",
                    report.value_kind_changed)
        for project_id, _, _ in projects:
            sync_repo.request_full_resync(conn, project_id)

    for project_id, _, project_key in projects:
        since, full_resync = sync_repo.read_watermark(conn, project_id)
        run_id = sync_repo.start_run(conn, instance_id, project_id, "ISSUES")
        try:
            result = sync_issues(conn, client, instance_id, project_id,
                                 project_key, since)
            summary.parse_failures += result.parse_failures
            summary.changelog_truncated += result.changelog_truncated
            if dry_run:
                conn.rollback()
                sync_repo.finish_run(conn, run_id, "SUCCESS", result.fetched, 0)
                summary.projects_ok += 1
                continue

            derive_history(conn, instance_id, result.changed_issue_ids,
                           category_of=category_of)
            update_first_done_at(conn, result.changed_issue_ids)
            sync_repo.write_watermark(
                conn, project_id, next_watermark(result.max_updated, since), "SUCCESS"
            )
            if full_resync:
                sync_repo.clear_full_resync(conn, project_id)   # 성공 직후에만
            conn.commit()
            sync_repo.finish_run(conn, run_id, "SUCCESS",
                                 result.fetched, result.upserted)
            summary.projects_ok += 1
            summary.issues_upserted += result.upserted
        except JiraAuthError:
            conn.rollback()
            sync_repo.finish_run(conn, run_id, "FAILED", error="auth failed")
            raise
        except Exception as exc:
            conn.rollback()
            log.exception("project %s failed", project_key)
            sync_repo.write_watermark(conn, project_id, None, "FAILED")
            conn.commit()
            sync_repo.finish_run(conn, run_id, "FAILED", error=repr(exc))
            summary.projects_failed += 1
            summary.errors[project_key] = repr(exc)

    if daily and not dry_run:
        run_id = sync_repo.start_run(conn, instance_id, None, "PROFILE")
        profile_fields(conn, instance_id)
        sync_repo.finish_run(conn, run_id, "SUCCESS")

        run_id = sync_repo.start_run(conn, instance_id, None, "DETECT_DELETED")
        detect_deleted(conn, client, instance_id)
        sync_repo.finish_run(conn, run_id, "SUCCESS")

    return summary
