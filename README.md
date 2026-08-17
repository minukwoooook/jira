# jira_dashboard

Syncs a Jira Data Center 10.3 instance into Oracle 19c for dashboarding.
DDL under `jira_dashboard/db/ddl/0*.sql` is applied by hand — there is no
`cli db` subcommand (spec §11.1).

## Configuration

Settings come from environment variables / a `.env` file (see
`jira_dashboard/config/settings.py`): `ORACLE_DSN`, `ORACLE_USER`,
`ORACLE_PASSWORD`, plus optional `DISPLAY_TZ`, `POOL_MIN`, `POOL_MAX`,
`CALL_TIMEOUT_MS`.

## CLI

```
python -m jira_dashboard.cli sync --instance SITE_A [--dry-run] [--daily]
python -m jira_dashboard.cli doctor [--db] [--jira --instance SITE_A --project PROJ] [--skip-schema]
python -m jira_dashboard.cli capture --instance SITE_A --project PROJ [--limit N] [--anonymize]
```

`sync --dry-run` rolls back every write and never advances the watermark —
safe to run against production before the first real sync. `--daily` also
runs field profiling and delete/move detection; run it once a day, not
every hour.

## Cron registration

**Without `flock -n`, a run that takes longer than the schedule period
overlaps with the next one** (spec §5.10). The `test_sync_run` /
`test_sync_watermark` `RUNNING` status is display state only — it is never
used to prevent concurrent execution, because a process that is killed
leaves that status behind forever. `flock -n` is what actually prevents
overlap, because the lock is released automatically when the process
dies.

```cron
0 * * * *  flock -n /var/run/jira_sync.lock \
           /opt/jira_dashboard/.venv/bin/python -m jira_dashboard.cli sync --instance SITE_A
30 2 * * * flock -n /var/run/jira_sync.lock \
           /opt/jira_dashboard/.venv/bin/python -m jira_dashboard.cli sync --instance SITE_A --daily
```

Both lines share the same lock file so the daily run and an hourly run
never execute concurrently against the same instance.
