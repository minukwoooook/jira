"""쓰기 경로의 바인드 값이 DDL이 선언한 컬럼 폭을 넘지 않는지 검사하는 장치.

`ORA-12899`를 지금까지 세 번, 매번 눈으로 찾았다 (R19 → R22 → 이번 라운드의
`val_id`/`from_id`/`to_id`/`error_msg`). 그 부류를 스위트가 잡게 하려면 두 가지가
필요하다:

1. 폭의 출처가 DDL이어야 한다 — 손으로 적은 숫자는 DDL과 어긋날 수 있다.
   `schema_map.column_byte_limits`가 그 출처다.
2. "어떤 바인드가 어떤 컬럼으로 가는가"를 SQL에서 뽑아야 한다 — 손으로 적은
   대응표는 문장이 바뀌면 조용히 낡는다. `bind_columns`가 그 일을 한다.

그래서 이 모듈의 `WidthCheckingConn`은 진짜 리포지토리 함수를 그대로 실행시키면서
executemany/execute에 실린 값 하나하나를 그 컬럼의 바이트 폭과 대조한다.
"""
import re

_TARGET_TABLE = re.compile(
    r"\b(?:INSERT\s+INTO|MERGE\s+INTO|UPDATE)\s+(?!SET\b)(\w+)", re.IGNORECASE
)
_INSERT_HEAD = re.compile(r"\bINSERT\b(?:\s+INTO\s+\w+)?\s*\(", re.IGNORECASE)
_VALUES_HEAD = re.compile(r"\bVALUES\s*\(", re.IGNORECASE)
_ASSIGNMENT = re.compile(r"(?:\w+\.)?(\w+)\s*=\s*:(\w+)")
_BIND = re.compile(r":(\w+)")


class WidthViolation(AssertionError):
    pass


def _balanced(text: str, open_paren: int) -> tuple[str, int]:
    """text[open_paren]이 '('일 때 짝이 맞는 ')'까지의 내용과 그 다음 위치."""
    depth = 0
    for i in range(open_paren, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1:i], i + 1
    return "", len(text)


def _split_top_level(body: str) -> list[str]:
    parts, depth, buf = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def _insert_pairs(sql: str) -> list[tuple[list[str], list[str]]]:
    """(컬럼 목록, VALUES 목록) 쌍. VALUES 안의 함수 호출 괄호를 깨지 않는다 —
    SYS_EXTRACT_UTC(SYSTIMESTAMP) 하나 때문에 대응이 통째로 어긋났었다."""
    out = []
    for m in _INSERT_HEAD.finditer(sql):
        columns, after = _balanced(sql, m.end() - 1)
        v = _VALUES_HEAD.search(sql, after)
        if v is None:
            continue
        values, _ = _balanced(sql, v.end() - 1)
        out.append(([c.strip() for c in _split_top_level(columns)],
                    [x.strip() for x in _split_top_level(values)]))
    return out


def strip_literals(sql: str) -> str:
    """문자열 리터럴 제거. 'YYYY-MM-DD HH24:MI:SS'의 :MI가 바인드로 오인된다."""
    return re.sub(r"'[^']*'", "''", sql)


def bind_columns(sql: str) -> tuple[str | None, dict[str, str]]:
    """(대상 테이블, {바인드 이름: 컬럼명}). SELECT면 (None, {})."""
    sql = strip_literals(sql)
    m = _TARGET_TABLE.search(sql)
    if m is None:
        return None, {}
    table = m.group(1).upper()
    mapping: dict[str, str] = {}
    for cols, vals in _insert_pairs(sql):
        if len(cols) != len(vals):
            raise AssertionError(
                f"INSERT 컬럼 {len(cols)}개와 VALUES {len(vals)}개가 맞지 않는다: {sql}"
            )
        for column, value in zip(cols, vals):
            bind = _BIND.fullmatch(value)
            if bind:
                mapping[bind.group(1)] = column.upper()
    for column, bind in _ASSIGNMENT.findall(sql):
        mapping.setdefault(bind, column.upper())
    return table, mapping


def check_binds(limits: dict[tuple[str, str], int], sql: str, rows) -> list[str]:
    """폭을 넘은 (테이블.컬럼, 바이트) 목록. 넘은 게 없으면 빈 리스트."""
    table, mapping = bind_columns(sql)
    if table is None:
        return []
    problems = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for bind, value in row.items():
            column = mapping.get(bind)
            if column is None or not isinstance(value, str):
                continue
            limit = limits.get((table, column))
            if limit is None:
                continue
            size = len(value.encode("utf-8"))
            if size > limit:
                problems.append(
                    f"{table}.{column} <- :{bind} = {size} bytes > {limit} "
                    f"(value starts {value[:24]!r})"
                )
    return problems


class _Var:
    """cursor.var(int) 대역. RETURNING ... INTO 바인드용."""

    def __init__(self, value=1):
        self._value = value

    def getvalue(self):
        return [self._value]


class WidthCheckingCursor:
    def __init__(self, conn):
        self._conn = conn
        self._rows = []

    # --- 검사 ---
    def _check(self, sql, rows):
        self._conn.statements.append(sql)
        problems = check_binds(self._conn.limits, sql, rows)
        if problems:
            raise WidthViolation("; ".join(problems))

    def execute(self, sql, **binds):
        self._check(sql, [binds])
        self._rows = self._conn.answer_for(sql)

    def executemany(self, sql, rows, **kwargs):
        self._check(sql, rows)

    # --- 조회 대역 ---
    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def var(self, *args, **kwargs):
        return _Var()

    def setinputsizes(self, **kwargs):
        pass

    @property
    def rowcount(self):
        return len(self._rows)


class WidthCheckingConn:
    """리포지토리 함수를 실제로 실행시키되, SQL은 실행하지 않고 폭만 검사한다.

    answers는 SQL 부분문자열 → fetch 결과. 읽기 함수를 스텁하지 않고 진짜로
    돌리기 위한 최소한의 대역이다.
    """

    def __init__(self, limits, answers=None):
        self.limits = limits
        self.answers = answers or {}
        self.statements: list[str] = []
        self.commits = 0

    def answer_for(self, sql):
        for key, rows in self.answers.items():
            if key in sql:
                return rows
        return []

    def cursor(self):
        return WidthCheckingCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass
