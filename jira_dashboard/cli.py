import argparse
import logging
import os
import sys


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jira_dashboard")
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="run the collection pipeline")
    sync.add_argument("--instance", required=True, help="instance_key")
    sync.add_argument("--project",
                      help="limit the run to one enabled project key "
                           "(staged rollout, spec 11.7 steps 11-14)")
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--daily", action="store_true",
                      help="also run profiling and delete detection")

    doctor = sub.add_parser("doctor", help="verify environment assumptions (read-only)")
    doctor.add_argument("--db", action="store_true")
    doctor.add_argument("--jira", action="store_true")
    doctor.add_argument("--skip-schema", action="store_true",
                        help="skip DDL/schema comparison (use before DDL is applied)")
    doctor.add_argument("--instance", help="instance_key (required with --jira)")
    doctor.add_argument("--project", help="probe project key (required with --jira)")

    cap = sub.add_parser("capture", help="save real API responses (on-prem only)")
    cap.add_argument("--instance", required=True)
    cap.add_argument("--project", required=True)
    cap.add_argument("--limit", type=int, default=200)
    cap.add_argument("--anonymize", action="store_true")
    cap.add_argument("--out", default="tests/fixtures/captured")

    instance = sub.add_parser("instance", help="manage TEST_JIRA_INSTANCE rows")
    instance_sub = instance.add_subparsers(dest="instance_cmd", required=True)

    inst_add = instance_sub.add_parser("add", help="register or update an instance")
    inst_add.add_argument("--key", required=True, help="instance_key")
    inst_add.add_argument("--base-url", required=True)
    inst_add.add_argument("--auth-type", required=True, choices=["PAT", "BASIC"])
    inst_add.add_argument("--secret-ref", required=True,
                          help="name of the environment variable that holds the "
                               "token at run time -- NOT the token itself; the "
                               "token is never stored in the database")

    instance_sub.add_parser("list", help="list registered instances")

    project = sub.add_parser("project", help="manage TEST_JIRA_PROJECT.is_enabled")
    project_sub = project.add_subparsers(dest="project_cmd", required=True)

    proj_list = project_sub.add_parser("list", help="list projects discovered by catalog sync")
    proj_list.add_argument("--instance", required=True, help="instance_key")

    proj_enable = project_sub.add_parser("enable", help="whitelist a project for sync")
    proj_enable.add_argument("--instance", required=True, help="instance_key")
    proj_enable.add_argument("--key", required=True, help="project_key")

    proj_disable = project_sub.add_parser("disable", help="remove a project from the whitelist")
    proj_disable.add_argument("--instance", required=True, help="instance_key")
    proj_disable.add_argument("--key", required=True, help="project_key")

    return parser


def _client_for(conn, instance_key: str):
    from jira_dashboard.db.repository.catalog import instance_config
    from jira_dashboard.jira.client import HttpJiraClient

    row = instance_config(conn, instance_key)
    if row is None:
        raise SystemExit(f"unknown instance: {instance_key}")
    instance_id, base_url, auth_type, secret_ref = row
    return instance_id, HttpJiraClient.from_config(base_url, auth_type, secret_ref)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = _build_parser().parse_args(argv)

    from jira_dashboard.db.pool import db_conn

    if args.command == "sync":
        from jira_dashboard.pipeline.runner import run_instance

        # --dry-run은 commit을 삼키는 커넥션을 받는다 (C1). 이 플래그가 없으면
        # db_conn이 종료 시 커밋해 "롤백한다"는 약속이 무의미해진다.
        with db_conn(read_only=args.dry_run) as conn:
            instance_id, client = _client_for(conn, args.instance)
            summary = run_instance(conn, client, instance_id,
                                   dry_run=args.dry_run, daily=args.daily,
                                   project=args.project)
        print(f"ok={summary.projects_ok} failed={summary.projects_failed} "
              f"upserted={summary.issues_upserted} "
              f"parse_failures={summary.parse_failures} "
              f"changelog_truncated={summary.changelog_truncated}")
        for key, err in summary.errors.items():
            print(f"  {key}: {err}")
        return 1 if (summary.projects_failed or summary.steps_failed) else 0

    if args.command == "doctor":
        from jira_dashboard.doctor.db_checks import format_report, run_db_checks

        # doctor와 capture는 spec §11.5/§11.6이 읽기 전용이라고 선언한 경로다 —
        # 같은 가드를 쓰면 그 선언이 구조가 된다 (커밋 자체가 불가능해진다).
        failed = False
        if args.db or not args.jira:
            with db_conn(read_only=True) as conn:
                results = run_db_checks(conn, skip_schema=args.skip_schema)
            print("=== DB ===")
            print(format_report(results))
            failed |= any(r.verdict == "FAIL" for r in results)
        if args.jira:
            from jira_dashboard.doctor.jira_checks import run_jira_checks

            with db_conn(read_only=True) as conn:
                _, client = _client_for(conn, args.instance)
            results = run_jira_checks(client, args.project)
            print("=== JIRA ===")
            print(format_report(results))
            failed |= any(r.verdict == "FAIL" for r in results)
        return 1 if failed else 0

    if args.command == "capture":
        from pathlib import Path

        from jira_dashboard.capture import capture_fixtures

        with db_conn(read_only=True) as conn:
            _, client = _client_for(conn, args.instance)
        counts = capture_fixtures(client, args.project, Path(args.out),
                                  limit=args.limit, anonymize=args.anonymize)
        print(counts)
        return 0

    if args.command == "instance":
        from jira_dashboard.db.repository.catalog import list_instances, upsert_instance

        # 부트스트랩 명령이라 쓰기 커넥션이 필요하다 (C1과 반대: 여기선 커밋이
        # 목적이다). read_only=True를 쓰면 MERGE가 조용히 롤백된다.
        with db_conn(read_only=False) as conn:
            if args.instance_cmd == "add":
                if args.secret_ref not in os.environ:
                    print(f"warning: environment variable {args.secret_ref} is not "
                          "currently set -- HttpJiraClient.from_config reads it at "
                          "sync time, so sync/doctor --jira will fail auth until "
                          "it is exported", file=sys.stderr)
                instance_id = upsert_instance(conn, args.key, args.base_url,
                                              args.auth_type, args.secret_ref)
                print(f"ok instance_id={instance_id} key={args.key}")
            elif args.instance_cmd == "list":
                for key, base_url, auth_type, secret_ref, is_active in list_instances(conn):
                    print(f"{key}\t{base_url}\t{auth_type}\t{secret_ref}\t{is_active}")
        return 0

    if args.command == "project":
        from jira_dashboard.db.repository.catalog import (
            instance_config, list_projects, set_project_enabled,
        )

        with db_conn(read_only=False) as conn:
            row = instance_config(conn, args.instance)
            if row is None:
                raise SystemExit(f"unknown instance: {args.instance}")
            instance_id = row[0]

            if args.project_cmd == "list":
                for project_key, name, is_enabled in list_projects(conn, instance_id):
                    print(f"{project_key}\t{name}\t{is_enabled}")
            else:
                enabled = args.project_cmd == "enable"
                affected = set_project_enabled(conn, instance_id, args.key, enabled)
                if affected == 0:
                    raise SystemExit(
                        f"no such project: {args.key} for instance {args.instance} "
                        f"-- run `sync --instance {args.instance}` once first so "
                        "catalog sync can discover it"
                    )
                print(f"ok {args.project_cmd}d {args.key}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
