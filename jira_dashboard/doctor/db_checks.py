"""spec §2.4의 Oracle DB 전제를 실행 가능한 검사로 바꾼다. 읽기 전용이다.

유일한 예외는 DB8(타임스탬프 바인드 왕복)이며, 이것도 dual 한 줄짜리 조회로만
확인하고 테이블을 만들거나 쓰지 않는다.

이 모듈은 정적 SQL 게이트(SCANNED_PACKAGES)의 스캔 대상이지만, 지금 이 파일의
SQL 문자열 중 그 게이트가 실제로 검증하는 것은 하나도 없다. DB1~DB8은 전부
Oracle 딕셔너리 뷰/의사테이블(v$version, v$parameter, v$timezone_names,
user_sys_privs, user_tables, user_tab_columns, user_indexes, user_constraints,
user_views, user_sequences, dual)만 참조하고 애플리케이션의 TEST_ 테이블을 전혀
언급하지 않는다. TEST_ 라는 글자가 나오는 유일한 자리는 LIKE 이스케이프
패턴(밑줄 앞에 이스케이프 문자를 붙여 Oracle LIKE의 와일드카드 의미를 지운 것)인데,
게이트의 테이블 토큰 정규식은 TEST_ 바로 뒤에 그 이스케이프 문자가 끼어 있으면
매치하지 못한다 — 그래서 실제 테이블명이 아닌 이 패턴은 게이트에 걸리지 않는다.
DB7의 대조 자체도 SQL 문자열이 아니라 Python 비교(schema_map의 파싱 결과 vs
데이터 딕셔너리)로 이뤄지므로 애초에 이 게이트가 볼 자리가 없다. DB7은 오직
Python 레벨 테스트(tests/unit/test_doctor_db.py)로만 검증되며, 그 테스트는 실제
DDL에서 만든 딕셔너리 응답을 먹여 이름·폭·인덱스·제약·뷰·시퀀스 대조를 모두
확인한다 (R33).
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import oracledb

from jira_dashboard.db import schema_map
from jira_dashboard.jira.models import KST, SENTINEL

DDL_DIR = Path(__file__).parents[1] / "db" / "ddl"
# data_length를 바이트 폭으로 읽어도 되는 타입 (spec §2.2는 전부 BYTE 선언이다)
_TEXT_TYPES = {"VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR", "RAW"}

_SCHEMA_COLUMNS = """
SELECT table_name, column_name, data_type, data_length FROM user_tab_columns
WHERE  table_name LIKE 'TEST\\_%' ESCAPE '\\'
"""

# DB7이 이름만 보던 나머지 객체들. 인덱스/제약/뷰/시퀀스가 빠지면, 손으로 적용한
# DDL에서 파일 하나를 건너뛰어도 "스키마 일치"라고 보고한다 (R33).
_SCHEMA_INDEXES = """
SELECT index_name FROM user_indexes
WHERE  index_name LIKE 'TEST\\_%' ESCAPE '\\'
"""

_SCHEMA_CONSTRAINTS = """
SELECT constraint_name FROM user_constraints
WHERE  constraint_name LIKE 'TEST\\_%' ESCAPE '\\'
"""

_SCHEMA_VIEWS = """
SELECT view_name FROM user_views
WHERE  view_name LIKE 'TEST\\_%' ESCAPE '\\'
"""

_SCHEMA_SEQUENCES = """
SELECT sequence_name FROM user_sequences
WHERE  sequence_name LIKE 'TEST\\_%' ESCAPE '\\'
"""

# DB8: 알려진 KST 시각. 자정/자정 언저리가 아닌 시각을 골라 오프셋 변환 버그가
# 자정 근처 우연한 일치로 숨는 것을 막는다.
_PROBE_INSTANT = datetime(2020, 3, 4, 5, 6, 7, tzinfo=KST)
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


@contextmanager
def _degrade_to_warn(out: list[CheckResult], check_id: str, title: str):
    """블록 안의 조회가 ORA- 오류로 실패하면(전형적으로 v$/딕셔너리 뷰에 대한 권한
    부족 — 예: SELECT_CATALOG_ROLE 없이 v$parameter를 읽으려다 ORA-00942) 이 검사
    하나만 WARN으로 내려앉히고 나머지 검사는 계속 진행한다. 권한이 빠진 뷰 하나
    때문에 doctor --db 전체가 죽어서 DB5(DDL 권한)나 DB8(타임스탬프 왕복)처럼
    전혀 다른 걸 보는 검사까지 실행되지 않는 것을 막는다."""
    try:
        yield
    except oracledb.DatabaseError as e:
        out.append(CheckResult(
            check_id, title, "WARN", str(e).strip(),
            "조회 자체가 실패했다 — 이 계정에 v$/딕셔너리 뷰 조회 권한이 없을 가능성이 "
            "높다. DBA에게 SELECT_CATALOG_ROLE(또는 해당 v$ 뷰에 대한 개별 SELECT) "
            "권한을 요청한 뒤 다시 실행할 것",
        ))


def _one(conn, sql, **binds):
    cur = conn.cursor()
    cur.execute(sql, **binds)
    row = cur.fetchone()
    return row[0] if row else None


def _actual_columns(conn) -> dict[str, dict[str, int | None]]:
    """테이블 → {컬럼명: 선언 바이트 폭 또는 None}.

    data_length는 VARCHAR2/CHAR에서 바이트 수다 (BYTE 세만틱 전제 — DB2/DB4가 그
    전제를 실측한다). NUMBER/TIMESTAMP/BLOB의 data_length는 내부 표현 크기라 폭
    대조에 쓸 수 없으므로 None으로 버린다.
    """
    cur = conn.cursor()
    cur.execute(_SCHEMA_COLUMNS)
    out: dict[str, dict[str, int | None]] = {}
    for table, column, data_type, data_length in cur.fetchall():
        width = data_length if (data_type or "").upper() in _TEXT_TYPES else None
        out.setdefault(table.upper(), {})[column.upper()] = width
    return out


def _names(conn, sql: str) -> set[str]:
    cur = conn.cursor()
    cur.execute(sql)
    return {r[0].upper() for r in cur.fetchall()}


def _schema_problems(conn) -> tuple[list[str], dict[str, int]]:
    """(문제 목록, 실제로 관측한 객체 수). 이름·폭·인덱스·제약·뷰·시퀀스를 전부 본다."""
    expected = schema_map.parse_columns(DDL_DIR)
    actual = _actual_columns(conn)
    problems: list[str] = []

    for table, columns in expected.items():
        if table not in actual:
            problems.append(f"missing table {table}")
            continue
        for name, column in sorted(columns.items()):
            if name not in actual[table]:
                problems.append(f"{table}.{name} missing")
                continue
            declared, found = column.byte_length, actual[table][name]
            if declared is not None and found is not None and declared != found:
                problems.append(
                    f"{table}.{name} width {found} != DDL {declared}"
                )

    counts = {"tables": len(actual)}
    for label, expected_names, sql in (
        ("indexes", schema_map.parse_indexes(DDL_DIR), _SCHEMA_INDEXES),
        ("constraints", schema_map.parse_constraints(DDL_DIR), _SCHEMA_CONSTRAINTS),
        ("views", schema_map.parse_views(DDL_DIR), _SCHEMA_VIEWS),
        ("sequences", schema_map.parse_sequences(DDL_DIR), _SCHEMA_SEQUENCES),
    ):
        found = _names(conn, sql)
        counts[label] = len(found)
        for name in sorted(expected_names - found):
            problems.append(f"missing {label[:-1]} {name}")
    return problems, counts


def _check_timestamp_roundtrip(conn) -> CheckResult:
    """DB8: aware KST datetime을 naive TIMESTAMP 컬럼에 바인드했을 때 oracledb가
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
        "(spec §2.1, jira_dashboard/db/repository/history.py의 as_kst와 짝을 "
        "이루는 쓰기측 규약이 없다는 뜻이므로 만들어야 한다)"
    )
    if row is None:
        return CheckResult(
            "DB8", "TIMESTAMP 바인드 왕복 (aware → naive 컬럼)", "FAIL",
            "SELECT ... FROM dual 이 행을 반환하지 않았다 — 왕복 자체를 확인할 수 없다",
            impact,
        )
    probe_back, sentinel_back, literal_back = row
    probe_kst = (probe_back.replace(tzinfo=KST)
                if probe_back.tzinfo is None else probe_back)
    probe_ok = probe_kst == _PROBE_INSTANT
    # 리터럴과 바인드한 SENTINEL은 재해석 없이 그대로(둘 다 naive) 비교한다 —
    # 이게 바로 DDL DEFAULT와 애플리케이션이 바인드하는 값이 실제로 같은 순간을
    # 가리키는지를 보는 지점이다.
    sentinel_ok = sentinel_back == literal_back
    verdict = "PASS" if (probe_ok and sentinel_ok) else "FAIL"
    observed = (
        f"probe bound={_PROBE_INSTANT.isoformat()} roundtrip(as KST)={probe_kst.isoformat()} "
        f"match={probe_ok}; sentinel roundtrip={sentinel_back} "
        f"literal(valid_to DEFAULT)={literal_back} match={sentinel_ok}"
    )
    return CheckResult(
        "DB8", "TIMESTAMP 바인드 왕복 (aware → naive 컬럼)", verdict, observed, impact,
    )


def run_db_checks(conn, *, skip_schema: bool = False) -> list[CheckResult]:
    out: list[CheckResult] = []

    with _degrade_to_warn(out, "DB1", "Oracle 버전이 19c인가"):
        banner = _one(conn, "SELECT banner_full FROM v$version") or ""
        out.append(CheckResult(
            "DB1", "Oracle 버전이 19c인가", "PASS" if "19" in banner else "FAIL",
            banner, "상위 버전이면 spec §2.4의 문법 전제를 재검토해야 한다",
        ))

    with _degrade_to_warn(out, "DB2", "max_string_size"):
        mss = _one(conn, "SELECT value FROM v$parameter WHERE name = 'max_string_size'") or "?"
        out.append(CheckResult(
            "DB2", "max_string_size", "PASS" if mss == "STANDARD" else "WARN", mss,
            "EXTENDED면 VARCHAR2 상한이 32767이 되어 컬럼 폭 설계를 다시 볼 수 있다",
        ))

    with _degrade_to_warn(out, "DB3", "Asia/Seoul 타임존 파일"):
        tz = _one(conn, "SELECT COUNT(*) FROM v$timezone_names WHERE tzname = 'Asia/Seoul'")
        out.append(CheckResult(
            "DB3", "Asia/Seoul 타임존 파일", "PASS" if tz else "FAIL", str(tz),
            "없으면 AT TIME ZONE 버킷팅이 실패한다. 고정 오프셋으로 폴백해야 한다",
        ))

    with _degrade_to_warn(out, "DB4", "캐릭터셋 / 블록 크기"):
        charset = _one(
            conn,
            "SELECT value FROM nls_database_parameters "
            "WHERE parameter = 'NLS_CHARACTERSET'",
        )
        block = _one(conn, "SELECT value FROM v$parameter WHERE name = 'db_block_size'")
        # 관측하지 못한 것에 PASS를 주지 않는다 (R34). DB4는 모든 truncate()가 깔고 있는
        # "VARCHAR2 폭은 바이트다"라는 전제의 근거인데, 두 조회가 아무 행도 돌려주지
        # 않아도 PASS를 찍고 있었다 — 근거가 없는데 근거가 있다고 보고한 셈이다.
        missing_facts = [name for name, value in
                         (("NLS_CHARACTERSET", charset), ("db_block_size", block))
                         if not value]
        out.append(CheckResult(
            "DB4", "캐릭터셋 / 블록 크기",
            "PASS" if not missing_facts else "WARN",
            f"{charset or '?'} / {block or '?'}",
            "기록용. VARCHAR2 BYTE 세만틱 전제(spec §2.2)의 근거가 된다"
            if not missing_facts else
            f"{', '.join(missing_facts)}를 관측하지 못했다 — BYTE 세만틱 전제(spec §2.2)의 "
            "근거가 비어 있다. 모든 truncate() 상한이 이 전제에 기대고 있으므로 "
            "sqlplus로 직접 확인할 것",
        ))

    with _degrade_to_warn(out, "DB5", "DDL 권한"):
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

    with _degrade_to_warn(out, "DB6", "기존 TEST_ 객체"):
        existing = _one(
            conn,
            r"SELECT COUNT(*) FROM user_tables WHERE table_name LIKE 'TEST\_%' ESCAPE '\'",
        )
        # 여기도 무조건 PASS였다 (R34). 0개는 "DDL 미적용"이라는 관측이고, None은
        # "관측 실패"다 — 둘 다 PASS가 아니다.
        out.append(CheckResult(
            "DB6", "기존 TEST_ 객체",
            "PASS" if existing else "WARN",
            "count 조회가 행을 돌려주지 않았다" if existing is None else f"{existing} tables",
            "TEST_ 테이블이 0개다 — DDL을 아직 안 돌린 상태다 (런북 3단계 전이라면 정상, "
            "4단계에서 이게 보이면 DDL이 적용되지 않았다는 뜻이다). drop_all.sql 전에는 "
            "반드시 목록을 눈으로 확인할 것",
        ))

    with _degrade_to_warn(out, "DB8", "TIMESTAMP 바인드 왕복 (aware → naive 컬럼)"):
        out.append(_check_timestamp_roundtrip(conn))

    if not skip_schema:
        with _degrade_to_warn(out, "DB7", "DDL 파일 ↔ 실제 스키마 대조"):
            problems, counts = _schema_problems(conn)
            observed = ", ".join(f"{k}={v}" for k, v in counts.items())
            out.append(CheckResult(
                "DB7", "DDL 파일 ↔ 실제 스키마 대조",
                "PASS" if not problems else "FAIL",
                (f"matches ({observed})" if not problems
                 else f"{observed}; " + "; ".join(problems[:5])
                      + (f" (+{len(problems) - 5} more)" if len(problems) > 5 else "")),
                "DDL을 다시 적용해야 한다 (docs/ddl-apply.md). 자동으로 고치지 않는다. "
                "폭 불일치는 코드의 truncate 상한(jira_dashboard/jira/models.py)과 "
                "어긋난다는 뜻이므로 ORA-12899가 예정되어 있다",
            ))
    return out


def format_report(results: list[CheckResult]) -> str:
    lines = []
    for r in results:
        lines.append(f"{r.id:<4} {r.title:<34} {r.verdict:<5} {r.observed}")
        if r.verdict != "PASS":
            lines.append(f"     → {r.impact}")
    return "\n".join(lines)
