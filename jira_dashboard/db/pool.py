import logging
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

import oracledb

from jira_dashboard.config.settings import get_settings

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_pool() -> oracledb.ConnectionPool:
    s = get_settings()
    return oracledb.create_pool(
        user=s.oracle_user, password=s.oracle_password, dsn=s.oracle_dsn,
        min=s.pool_min, max=s.pool_max, increment=1,
    )


class ReadOnlyConnection:
    """commit()을 삼키는 커넥션 래퍼. --dry-run 전용이다.

    dry-run이 "쓰기를 전부 롤백한다"고 README가 약속하지만, 그 약속을 지키는 코드가
    한 군데도 아니었다: sync_issues는 페이지마다, sync_repo.start_run/finish_run/
    request_full_resync/reclaim_zombies는 각 호출 끝에서 commit()을 부르고,
    db_conn까지 정상 종료 시 한 번 더 커밋했다. runner의 rollback()은 이미 커밋된
    데이터 위에서 no-op이었다.

    호출 지점마다 `if not dry_run:`을 흩뿌리면 앞으로 추가되는 커밋 하나가 다시
    약속을 깬다. 그래서 커넥션 자체가 커밋을 거부하게 만든다 — 어떤 경로로도
    커밋이 불가능하다. 트랜잭션은 db_conn 종료 시 롤백된다.
    """

    def __init__(self, conn) -> None:
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def commit(self) -> None:
        log.debug("commit suppressed (dry-run)")


@contextmanager
def db_conn(*, read_only: bool = False) -> Iterator[oracledb.Connection]:
    """정상 종료 시 commit, 예외 시 rollback. 사외에서는 실행되지 않는다.

    read_only=True면 정상 종료도 rollback이고, 넘겨주는 커넥션은 commit()을 삼키는
    ReadOnlyConnection이다 (--dry-run).
    """
    conn = get_pool().acquire()
    conn.call_timeout = get_settings().call_timeout_ms
    try:
        yield ReadOnlyConnection(conn) if read_only else conn
        if read_only:
            conn.rollback()
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        get_pool().release(conn)
