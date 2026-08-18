"""db_conn의 트랜잭션 경계. 실 Oracle 없이 커넥션/풀만 흉내내 검증한다.

C1: dry-run이 "쓰기를 전부 롤백한다"고 README가 약속했지만, db_conn은 정상 종료 시
항상 커밋했다 — 러너의 rollback()보다 나중에 실행되는 커밋이라, 사실상 dry-run이
프로덕션에 쓰는 경로였다.
"""
import pytest

from jira_dashboard.db import pool


class FakeConn:
    def __init__(self):
        self.calls = []
        self.call_timeout = None

    def commit(self):
        self.calls.append("commit")

    def rollback(self):
        self.calls.append("rollback")

    def cursor(self):
        self.calls.append("cursor")
        return object()


class FakePool:
    def __init__(self, conn):
        self.conn = conn
        self.released = 0

    def acquire(self):
        return self.conn

    def release(self, conn):
        self.released += 1


class FakeSettings:
    call_timeout_ms = 1234


@pytest.fixture
def fake_conn(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(pool, "get_pool", lambda: FakePool(conn))
    monkeypatch.setattr(pool, "get_settings", lambda: FakeSettings())
    return conn


def test_normal_exit_commits(fake_conn):
    with pool.db_conn() as conn:
        conn.cursor()
    assert fake_conn.calls == ["cursor", "commit"]


def test_exception_rolls_back(fake_conn):
    with pytest.raises(RuntimeError):
        with pool.db_conn():
            raise RuntimeError("boom")
    assert fake_conn.calls == ["rollback"]


def test_read_only_exit_rolls_back_instead_of_committing(fake_conn):
    with pool.db_conn(read_only=True) as conn:
        conn.cursor()
    assert fake_conn.calls == ["cursor", "rollback"]
    assert "commit" not in fake_conn.calls


def test_read_only_connection_swallows_commit(fake_conn):
    """호출 지점마다 `if not dry_run:`을 흩뿌리는 대신 커넥션이 커밋을 거부한다 —
    앞으로 추가되는 commit() 한 줄이 다시 약속을 깨지 못하게."""
    with pool.db_conn(read_only=True) as conn:
        conn.commit()
        conn.commit()
    assert fake_conn.calls == ["rollback"]


def test_read_only_connection_delegates_everything_else(fake_conn):
    with pool.db_conn(read_only=True) as conn:
        conn.cursor()
        conn.rollback()
    assert fake_conn.calls == ["cursor", "rollback", "rollback"]
