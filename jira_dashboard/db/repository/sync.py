from datetime import datetime

_SELECT_WATERMARK = """
SELECT last_synced_updated_at, full_resync_requested
FROM   test_sync_watermark WHERE project_id = :project_id
"""

_MERGE_WATERMARK = """
MERGE INTO test_sync_watermark t
USING (SELECT :project_id AS project_id FROM dual) s
ON (t.project_id = s.project_id)
WHEN MATCHED THEN UPDATE SET
  t.last_synced_updated_at = NVL(:since, t.last_synced_updated_at),
  t.last_run_at = SYS_EXTRACT_UTC(SYSTIMESTAMP), t.last_status = :last_status
WHEN NOT MATCHED THEN
  INSERT (project_id, last_synced_updated_at, last_run_at, last_status)
  VALUES (:project_id, :since, SYS_EXTRACT_UTC(SYSTIMESTAMP), :last_status)
"""

_MERGE_REQUEST_RESYNC = """
MERGE INTO test_sync_watermark t
USING (SELECT :project_id AS project_id FROM dual) s
ON (t.project_id = s.project_id)
WHEN MATCHED THEN UPDATE SET t.full_resync_requested = 'Y'
WHEN NOT MATCHED THEN INSERT (project_id, full_resync_requested)
                      VALUES (:project_id, 'Y')
"""

_CLEAR_RESYNC = """
UPDATE test_sync_watermark SET full_resync_requested = 'N' WHERE project_id = :project_id
"""

_INSERT_RUN = """
INSERT INTO test_sync_run (instance_id, project_id, step)
VALUES (:instance_id, :project_id, :step) RETURNING run_id INTO :out_run_id
"""

_FINISH_RUN = """
UPDATE test_sync_run
SET    finished_at = SYS_EXTRACT_UTC(SYSTIMESTAMP), status = :status,
       issues_fetched = :issues_fetched, issues_upserted = :issues_upserted,
       error_msg = :error_msg
WHERE  run_id = :run_id
"""

_RECLAIM_RUNS = """
UPDATE test_sync_run
SET    status = 'FAILED', finished_at = SYS_EXTRACT_UTC(SYSTIMESTAMP),
       error_msg = 'reclaimed: process died while RUNNING'
WHERE  status = 'RUNNING'
AND    started_at < SYS_EXTRACT_UTC(SYSTIMESTAMP) - NUMTODSINTERVAL(:hours, 'HOUR')
"""

_RECLAIM_WATERMARKS = """
UPDATE test_sync_watermark SET last_status = 'FAILED' WHERE last_status = 'RUNNING'
"""


def read_watermark(conn, project_id: int) -> tuple[datetime | None, bool]:
    """신규 프로젝트는 행이 없다 → (None, False)로 전체 수집 (spec §5.10)."""
    cur = conn.cursor()
    cur.execute(_SELECT_WATERMARK, project_id=project_id)
    row = cur.fetchone()
    if row is None:
        return None, False
    since, full = row
    return (None if full == "Y" else since), full == "Y"


def write_watermark(conn, project_id: int, since: datetime | None, status: str) -> None:
    """since=None이면 NVL이 기존 값을 유지한다 — 실패한 프로젝트는 전진하지 않는다."""
    conn.cursor().execute(_MERGE_WATERMARK, project_id=project_id,
                          since=since, last_status=status)


def request_full_resync(conn, project_id: int) -> None:
    conn.cursor().execute(_MERGE_REQUEST_RESYNC, project_id=project_id)
    conn.commit()


def clear_full_resync(conn, project_id: int) -> None:
    """성공 직후에만 부른다. 실패 시 그대로 두어 다음 배치가 다시 시도한다."""
    conn.cursor().execute(_CLEAR_RESYNC, project_id=project_id)


def start_run(conn, instance_id: int, project_id: int | None, step: str) -> int:
    cur = conn.cursor()
    out = cur.var(int)
    cur.execute(_INSERT_RUN, instance_id=instance_id, project_id=project_id,
                step=step, out_run_id=out)
    conn.commit()
    return out.getvalue()[0]


def finish_run(conn, run_id: int, status: str, fetched: int = 0,
               upserted: int = 0, error: str | None = None) -> None:
    conn.cursor().execute(
        _FINISH_RUN, run_id=run_id, status=status, issues_fetched=fetched,
        issues_upserted=upserted, error_msg=(error or "")[:4000] or None,
    )
    conn.commit()


def reclaim_zombies(conn, older_than_hours: int = 6) -> int:
    """프로세스가 죽으면 RUNNING이 영원히 남는다. 표시만 정리하고 실행 제어에는
    쓰지 않는다 — 중복 실행 방지는 cron의 flock이 한다 (spec §5.10)."""
    cur = conn.cursor()
    cur.execute(_RECLAIM_RUNS, hours=older_than_hours)
    reclaimed = cur.rowcount
    cur.execute(_RECLAIM_WATERMARKS)
    conn.commit()
    return reclaimed
