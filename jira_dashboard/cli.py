import argparse
import logging
import sys


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jira_dashboard")
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="run the collection pipeline")
    sync.add_argument("--instance", required=True, help="instance_key")
    sync.add_argument("--project",
                      help="limit the run to one enabled project key "
                           "(staged rollout, spec 11.7 steps 8-11)")
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

        failed = False
        if args.db or not args.jira:
            with db_conn() as conn:
                results = run_db_checks(conn, skip_schema=args.skip_schema)
            print("=== DB ===")
            print(format_report(results))
            failed |= any(r.verdict == "FAIL" for r in results)
        if args.jira:
            from jira_dashboard.doctor.jira_checks import run_jira_checks

            with db_conn() as conn:
                _, client = _client_for(conn, args.instance)
            results = run_jira_checks(client, args.project)
            print("=== JIRA ===")
            print(format_report(results))
            failed |= any(r.verdict == "FAIL" for r in results)
        return 1 if failed else 0

    if args.command == "capture":
        from pathlib import Path

        from jira_dashboard.capture import capture_fixtures

        with db_conn() as conn:
            _, client = _client_for(conn, args.instance)
        counts = capture_fixtures(client, args.project, Path(args.out),
                                  limit=args.limit, anonymize=args.anonymize)
        print(counts)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
