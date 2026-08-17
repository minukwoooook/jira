"""spec §2.4의 Oracle DB 전제를 실행 가능한 검사로 바꾼다. 읽기 전용이다.

유일한 예외는 DB8(타임스탬프 바인드 왕복) — 이것도 `SELECT ... FROM dual`로만
확인하며, 테이블을 만들거나 쓰지 않는다.

주의: 이 모듈의 SQL 문자열은 정적 게이트(tests/static/test_sql_references.py의
SCANNED_PACKAGES = ["jira_dashboard.db.repository", "jira_dashboard.pipeline"])의
스캔 대상이 아니다 — doctor는 두 패키지 어디에도 속하지 않는다. 그래서 여기 SQL의
테이블/컬럼 참조는 정적 게이트가 아니라 아래 테스트(tests/unit/test_doctor_db.py)와
DB7의 런타임 스키마 대조로만 검증된다.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from jira_dashboard.db import schema_map
from jira_dashboard.jira.models import SENTINEL

DDL_DIR = Path(__file__).parents[1] / "db" / "ddl"

_SCHEMA_COLUMNS = """
SELECT table_name, column_name FROM user_tab_columns
WHERE  table_name LIKE 'TEST\\_%' ESCAPE '\\'
"""

# DB8: 알려진 UTC 시각. 자정/자정 언저리가 아닌 시각을 골라 오프셋 변환 버그가
# 자정 근처 우연한 일치로 숨는 것을 막는다.
_PROBE_INSTANT = datetime(2020, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
_ROUNDTRIP_SQL = """
SELECT :probe_ts AS roundtrip_probe,
       :sentinel_ts AS roundtrip_sentinel,
       TIMESTAMP '9999-12-31 00:00:00' AS roundtrip_literal
FROM   dual
"""


@dataclass(frozen=True)
class CheckResult:
    id: str
    title: str
    verdict: str        # PASS | FAIL | WARN
    observed: str
    impact: str


def _one(conn, sql, **binds):
    cur = conn.cursor()
    cur.execute(sql, **binds)
    row = cur.fetchone()
    return row[0] if row else None


def _actual_schema(conn) -> dict[str, set[str]]:
    cur = conn.cursor()
    cur.execute(_SCHEMA_COLUMNS)
    out: dict[str, set[str]] = {}
    for table, column in cur.fetchall():
        out.setdefault(table.upper(), set()).add(column.upper())
    return out


def _check_timestamp_roundtrip(conn) -> CheckResult:
    """DB8: aware UTC datetime을 naive TIMESTAMP 컬럼에 바인드했을 때 oracledb가
    오프셋을 잘라내는지(맞음) 아니면 세션/로컬 타임존으로 변환해버리는지(틀리면
    모든 타임스탬프가 조용히 밀린다)를 실측한다. 동시에 valid_to DEFAULT로 쓰는
    SQL 리터럴 `TIMESTAMP '9999-12-31 00:00:00'`과 바인드한 SENTINEL이 같은 값으로
    저장/조회되는지도 함께 확인한다 — 다르면 asof 조회가 조용히 행을 놓친다."""
    cur = conn.cursor()
    cur.execute(_ROUNDTRIP_SQL, probe_ts=_PROBE_INSTANT, sentinel_ts=SENTINEL)
    row = cur.fetchone()
    impact = (
        "oracledb가 aware datetime을 세션 타임존으로 변환한 뒤 자르고 있다는 뜻이다 "
        "— 저장된 모든 타임스탬프가 오프셋만큼 밀려 있다. 바인드 전에 tzinfo를 "
        "벗기거나 cursor.setinputsizes(oracledb.DB_TYPE_TIMESTAMP)로 명시할 것 "
        "(spec §2.1, jira_dashboard/db/repository/history.py의 _as_utc와 짝을 "
        "이루는 쓰기측 규약이 없다는 뜻이므로 만들어야 한다)"
    )
    if row is None:
        return CheckResult(
            "DB8", "TIMESTAMP 바인드 왕복 (aware → naive 컬럼)", "FAIL",
            "SELECT ... FROM dual 이 행을 반환하지 않았다 — 왕복 자체를 확인할 수 없다",
            impact,
        )
    probe_back, sentinel_back, literal_back = row
    probe_utc = (probe_back.replace(tzinfo=timezone.utc)
                if probe_back.tzinfo is None else probe_back)
    probe_ok = probe_utc == _PROBE_INSTANT
    # 리터럴과 바인드한 SENTINEL은 재해석 없이 그대로(둘 다 naive) 비교한다 —
    # 이게 바로 DDL DEFAULT와 애플리케이션이 바인드하는 값이 실제로 같은 순간을
    # 가리키는지를 보는 지점이다.
    sentinel_ok = sentinel_back == literal_back
    verdict = "PASS" if (probe_ok and sentinel_ok) else "FAIL"
    observed = (
        f"probe bound={_PROBE_INSTANT.isoformat()} roundtrip(as UTC)={probe_utc.isoformat()} "
        f"match={probe_ok}; sentinel roundtrip={sentinel_back} "
        f"literal(valid_to DEFAULT)={literal_back} match={sentinel_ok}"
    )
    return CheckResult(
        "DB8", "TIMESTAMP 바인드 왕복 (aware → naive 컬럼)", verdict, observed, impact,
    )


def run_db_checks(conn, *, skip_schema: bool = False) -> list[CheckResult]:
    out: list[CheckResult] = []

    banner = _one(conn, "SELECT banner_full FROM v$version") or ""
    out.append(CheckResult(
        "DB1", "Oracle 버전이 19c인가", "PASS" if "19" in banner else "FAIL",
        banner, "상위 버전이면 spec §2.4의 문법 전제를 재검토해야 한다",
    ))

    mss = _one(conn, "SELECT value FROM v$parameter WHERE name = 'max_string_size'") or "?"
    out.append(CheckResult(
        "DB2", "max_string_size", "PASS" if mss == "STANDARD" else "WARN", mss,
        "EXTENDED면 VARCHAR2 상한이 32767이 되어 컬럼 폭 설계를 다시 볼 수 있다",
    ))

    tz = _one(conn, "SELECT COUNT(*) FROM v$timezone_names WHERE tzname = 'Asia/Seoul'")
    out.append(CheckResult(
        "DB3", "Asia/Seoul 타임존 파일", "PASS" if tz else "FAIL", str(tz),
        "없으면 AT TIME ZONE 버킷팅이 실패한다. 고정 오프셋으로 폴백해야 한다",
    ))

    charset = _one(
        conn,
        "SELECT value FROM nls_database_parameters "
        "WHERE parameter = 'NLS_CHARACTERSET'",
    ) or "?"
    block = _one(conn, "SELECT value FROM v$parameter WHERE name = 'db_block_size'") or "?"
    out.append(CheckResult(
        "DB4", "캐릭터셋 / 블록 크기", "PASS", f"{charset} / {block}",
        "기록용. VARCHAR2 BYTE 세만틱 전제(spec §2.2)의 근거가 된다",
    ))

    cur = conn.cursor()
    cur.execute(
        "SELECT privilege FROM user_sys_privs "
        "WHERE privilege IN ('CREATE TABLE', 'CREATE SEQUENCE', 'CREATE VIEW')"
    )
    granted = {r[0] for r in cur.fetchall()}
    needed = {"CREATE TABLE", "CREATE SEQUENCE", "CREATE VIEW"}
    missing = needed - granted
    out.append(CheckResult(
        "DB5", "DDL 권한", "PASS" if not missing else "FAIL",
        ", ".join(sorted(granted)) or "(none)",
        f"부족: {', '.join(sorted(missing)) or '없음'} — DDL 실행이 실패한다",
    ))

    existing = _one(
        conn,
        r"SELECT COUNT(*) FROM user_tables WHERE table_name LIKE 'TEST\_%' ESCAPE '\'",
    )
    out.append(CheckResult(
        "DB6", "기존 TEST_ 객체", "PASS", f"{existing} tables",
        "0이면 DDL을 아직 안 돌린 것이다. drop_all.sql 전에 목록을 눈으로 확인할 것",
    ))

    out.append(_check_timestamp_roundtrip(conn))

    if not skip_schema:
        expected = schema_map.parse_ddl(DDL_DIR)
        actual = _actual_schema(conn)
        problems = []
        for table, columns in expected.items():
            if table not in actual:
                problems.append(f"missing table {table}")
                continue
            for column in sorted(columns - actual[table]):
                problems.append(f"{table}.{column} missing")
        out.append(CheckResult(
            "DB7", "DDL 파일 ↔ 실제 스키마 대조",
            "PASS" if not problems else "FAIL",
            "matches" if not problems else "; ".join(problems[:5]),
            "DDL을 다시 적용해야 한다 (docs/ddl-apply.md). 자동으로 고치지 않는다",
        ))
    return out


def format_report(results: list[CheckResult]) -> str:
    lines = []
    for r in results:
        lines.append(f"{r.id:<4} {r.title:<34} {r.verdict:<5} {r.observed}")
        if r.verdict != "PASS":
            lines.append(f"     → {r.impact}")
    return "\n".join(lines)
