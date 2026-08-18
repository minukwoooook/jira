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
    steps_failed: int = 0
    issues_upserted: int = 0
    parse_failures: int = 0
    changelog_truncated: int = 0
    errors: dict[str, str] = field(default_factory=dict)


def run_instance(conn, client, instance_id: int, *,
                 dry_run: bool = False, daily: bool = False,
                 project: str | None = None) -> RunSummary:
    """인스턴스 내 프로젝트는 순차 처리한다. 병렬은 인스턴스 단위다 (spec §5.0).

    project를 주면 화이트리스트를 그 프로젝트 키 하나로 좁힌다 — 런북 11~14단계의
    단계적 롤아웃(테스트 프로젝트 하나로 dry-run → 실수집 → 멱등 확인)이 이걸
    전제한다 (spec §11.7).
    """
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

    all_projects = enabled_projects(conn, instance_id)

    for project_id in report.key_changed_projects:
        log.warning("project key changed (project_id=%s); full resync queued", project_id)
        sync_repo.request_full_resync(conn, project_id)
    if report.value_kind_changed:
        log.warning("value_kind changed for %s; full resync queued for all projects",
                    report.value_kind_changed)
        # 이번 실행에서 --project로 좁혔더라도 플래그는 전 프로젝트에 걸어야 한다 —
        # value_kind 변경의 영향은 지금 수집하는 프로젝트에 한정되지 않는다.
        for project_id, _, _ in all_projects:
            sync_repo.request_full_resync(conn, project_id)

    projects = all_projects
    if project is not None:
        projects = [p for p in all_projects if p[2] == project]
        if not projects:
            log.warning("--project %s is not an enabled project for this instance; "
                        "nothing to sync (enabled: %s)",
                        project, sorted(p[2] for p in all_projects))

    for project_id, _, project_key in projects:
        since, full_resync = sync_repo.read_watermark(conn, project_id)
        run_id = sync_repo.start_run(conn, instance_id, project_id, "ISSUES")
        try:
            result = sync_issues(conn, client, instance_id, project_id,
                                 project_key, since, dry_run=dry_run,
                                 full_resync=full_resync)
            summary.parse_failures += result.parse_failures
            summary.changelog_truncated += result.changelog_truncated
            if dry_run:
                conn.rollback()
                sync_repo.finish_run(conn, run_id, "SUCCESS", result.fetched, 0)
                summary.projects_ok += 1
                continue
            sync_repo.finish_run(conn, run_id, "SUCCESS",
                                 result.fetched, result.upserted)

            # HISTORY / FIRST_DONE도 TEST_SYNC_RUN.step 체크 제약이 허용하는 값이다.
            # 기록하지 않으면 이력 파생이 감사 테이블에서 아예 보이지 않는다 (Item 10).
            run_id = sync_repo.start_run(conn, instance_id, project_id, "HISTORY")
            derive_history(conn, instance_id, result.changed_issue_ids,
                           category_of=category_of)
            sync_repo.finish_run(conn, run_id, "SUCCESS")

            run_id = sync_repo.start_run(conn, instance_id, project_id, "FIRST_DONE")
            update_first_done_at(conn, result.changed_issue_ids)
            sync_repo.finish_run(conn, run_id, "SUCCESS")

            sync_repo.write_watermark(
                conn, project_id, next_watermark(result.max_updated, since), "SUCCESS"
            )
            if full_resync:
                sync_repo.clear_full_resync(conn, project_id)   # 성공 직후에만
            conn.commit()
            summary.projects_ok += 1
            summary.issues_upserted += result.upserted
        except JiraAuthError:
            conn.rollback()
            # 인증 실패도 이 프로젝트에 한해서는 일반 실패와 같다 — sync_issues가
            # 이미 페이지를 커밋했을 수 있으니 워터마크를 전진시키지 않고 전체
            # 재수집을 요청해야 다음 실행이 이력 파생 없는 SUCCESS로 조용히
            # 끝나지 않는다 (R31).
            sync_repo.write_watermark(conn, project_id, None, "FAILED")
            sync_repo.request_full_resync(conn, project_id)
            conn.commit()
            sync_repo.finish_run(conn, run_id, "FAILED", error="auth failed")
            raise
        except Exception as exc:
            conn.rollback()
            log.exception("project %s failed", project_key)
            sync_repo.write_watermark(conn, project_id, None, "FAILED")
            # 실패한 프로젝트는 다음 실행에서 해시를 우회해야 한다. sync_issues가
            # 페이지를 커밋한 뒤 derive_history가 터지면, 다음 실행은 해시가 같아
            # 전부 스킵하고 이력을 만들지 않은 채 SUCCESS를 보고한다 — 그리고
            # 워터마크만 비우는 전체 재수집으로는 절대 복구되지 않는다 (R31).
            sync_repo.request_full_resync(conn, project_id)
            conn.commit()
            sync_repo.finish_run(conn, run_id, "FAILED", error=repr(exc))
            summary.projects_failed += 1
            summary.errors[project_key] = repr(exc)

    if daily and not dry_run:
        # 프로젝트 단위 격리 규칙이 바로 위에 다섯 개나 있는데, 일일 단계에는 하나도
        # 없었다: profile_fields가 터지면 finish_run이 아예 호출되지 않아 RUNNING이
        # 6시간 남고, detect_deleted는 실행조차 되지 않았다 (Item 10).
        for step, run_step in (("PROFILE", lambda: profile_fields(conn, instance_id)),
                               ("DETECT_DELETED",
                                lambda: detect_deleted(conn, client, instance_id))):
            run_id = sync_repo.start_run(conn, instance_id, None, step)
            try:
                run_step()
                sync_repo.finish_run(conn, run_id, "SUCCESS")
            except Exception as exc:
                conn.rollback()
                log.exception("daily step %s failed", step)
                sync_repo.finish_run(conn, run_id, "FAILED", error=repr(exc))
                summary.steps_failed += 1
                summary.errors[step] = repr(exc)

    return summary
