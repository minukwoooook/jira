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
safe to run against production before the first real sync. It is safe
because the connection handed to a dry run refuses to commit
(`db_conn(read_only=True)` → `ReadOnlyConnection`) and `sync_issues`
skips its per-page commit; both are needed, since either commit alone
would make the rollback a no-op over already-committed data. Combine it
with `--project` so the rolled-back transaction stays small. `--daily`
also runs field profiling and delete/move detection; run it once a day,
not every hour.

`sync --project KEY` limits the run to one enabled project — the staged
rollout in `docs/design.md` §11.7 (steps 8-11) depends on it.

`doctor` and `capture` also take a read-only connection, so the
read-only guarantee the spec states for them (§11.5, §11.6) is enforced
by the connection rather than by convention.

## Offline install (on-premise)

Transfer is one-way via git, so the wheel bundle must be **tracked**:
`vendor/` is in `.gitignore` (so stray local wheels are never committed
by accident) and committed with `git add -f vendor/`. A fresh on-premise
clone therefore needs nothing but the clone itself:

```
pip install --no-index --find-links vendor/ -r requirements-dev.txt
```

`requirements.txt` is runtime only; `requirements-dev.txt` adds `pytest`,
which runbook step 7 (`JIRA_FIXTURES=captured pytest`) requires. Rebuild
the bundle with `make vendor` and prove it installs offline with
`make verify-vendor`.

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
