from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

import oracledb

from jira_dashboard.config.settings import get_settings


@lru_cache(maxsize=1)
def get_pool() -> oracledb.ConnectionPool:
    s = get_settings()
    return oracledb.create_pool(
        user=s.oracle_user, password=s.oracle_password, dsn=s.oracle_dsn,
        min=s.pool_min, max=s.pool_max, increment=1,
    )


@contextmanager
def db_conn() -> Iterator[oracledb.Connection]:
    """정상 종료 시 commit, 예외 시 rollback. 사외에서는 실행되지 않는다."""
    conn = get_pool().acquire()
    conn.call_timeout = get_settings().call_timeout_ms
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        get_pool().release(conn)
