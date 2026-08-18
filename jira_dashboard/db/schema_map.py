"""DDL 파일에서 스키마 사전을 만든다.

완전한 SQL 파서가 아니다. CREATE TABLE 블록의 컬럼 정의, CREATE INDEX/VIEW/
SEQUENCE 이름, CONSTRAINT 이름을 뽑는 수준이면 (a) 컬럼명 오타를 잡고(spec §11.2),
(b) 손으로 적용한 DDL이 실제로 다 들어갔는지 대조하고(doctor DB7, R33),
(c) 쓰기 경로의 바인드 값이 컬럼 폭을 넘지 않는지 테스트에서 검사하기에(R33/I6)
충분하다.

폭(byte_length)이 여기서 나오는 게 핵심이다. `ORA-12899`를 지금까지 세 번 각각
"눈으로" 찾았다 — 100바이트 val_id 두 개(R22와 그 형제), 255바이트 from_id/to_id,
그리고 문자 단위로 자르던 4000바이트 error_msg. 폭을 DDL에서 파생하면 그 부류가
검사 대상이 된다.
"""
import re
from dataclasses import dataclass
from pathlib import Path

_CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(\w+)\s*\((.*?)\)\s*(?:LOB\s*\(|;|$)",
    re.IGNORECASE | re.DOTALL,
)
_CREATE_SEQUENCE = re.compile(r"CREATE\s+SEQUENCE\s+(\w+)", re.IGNORECASE)
_CREATE_INDEX = re.compile(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(\w+)", re.IGNORECASE)
_CREATE_VIEW = re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+(\w+)", re.IGNORECASE)
_CONSTRAINT = re.compile(r"CONSTRAINT\s+(\w+)", re.IGNORECASE)
# 컬럼 정의가 아닌 줄
_NOT_A_COLUMN = re.compile(
    r"^\s*(CONSTRAINT|PRIMARY|UNIQUE|FOREIGN|CHECK)\b", re.IGNORECASE
)
# 컬럼명 + 타입 + (선택) 길이. VARCHAR2(50 BYTE) / CHAR(1 BYTE) / NUMBER(10) / BLOB
_COLUMN_TYPE = re.compile(
    r"^\s*(\w+)\s+([A-Za-z][A-Za-z0-9_]*)\s*(?:\(\s*(\d+)\s*(BYTE|CHAR)?\s*[^)]*\))?"
)
_SIZED_TYPES = {"VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR", "RAW"}


@dataclass(frozen=True)
class Column:
    name: str
    data_type: str
    byte_length: int | None     # VARCHAR2/CHAR 계열만. 그 외는 None

    @property
    def is_text(self) -> bool:
        return self.byte_length is not None


def _split_top_level(body: str) -> list[str]:
    """괄호 깊이 0의 콤마로만 나눈다. CHECK (a IN ('x','y')) 를 깨지 않기 위해."""
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


def _sql_files(ddl_dir: Path):
    for path in sorted(Path(ddl_dir).glob("[0-9][0-9]_*.sql")):
        # 주석 제거 — 주석에 든 CREATE INDEX가 실제 객체로 세어지면 DB7이 있지도
        # 않은 인덱스를 요구한다 (03_issue.sql 끝의 주석 처리된 두 인덱스).
        yield re.sub(r"--[^\n]*", "", path.read_text(encoding="utf-8"))


def parse_columns(ddl_dir: Path) -> dict[str, dict[str, Column]]:
    """테이블 → {컬럼명: Column}. 폭까지 담는다."""
    tables: dict[str, dict[str, Column]] = {}
    for text in _sql_files(ddl_dir):
        for name, body in _CREATE_TABLE.findall(text):
            cols: dict[str, Column] = {}
            for chunk in _split_top_level(body):
                if _NOT_A_COLUMN.match(chunk):
                    continue
                m = _COLUMN_TYPE.match(chunk)
                if not m:
                    continue
                column, data_type, size, unit = m.groups()
                data_type = data_type.upper()
                byte_length = None
                if data_type in _SIZED_TYPES and size is not None:
                    if (unit or "BYTE").upper() != "BYTE":
                        # CHAR 세만틱이면 바이트 상한은 최대 4배가 되어 폭 검사가
                        # 성립하지 않는다. spec §2.2는 전부 BYTE로 선언한다.
                        raise ValueError(
                            f"{name}.{column}: CHAR 세만틱은 지원하지 않는다 "
                            "(spec §2.2는 모든 문자열 컬럼을 BYTE로 선언한다)"
                        )
                    byte_length = int(size)
                cols[column.upper()] = Column(column.upper(), data_type, byte_length)
            tables[name.upper()] = cols
    return tables


def parse_ddl(ddl_dir: Path) -> dict[str, set[str]]:
    """테이블 → 컬럼명 집합. 컬럼명 대조 전용 계약이다."""
    return {table: set(cols) for table, cols in parse_columns(ddl_dir).items()}


def column_byte_limits(ddl_dir: Path) -> dict[tuple[str, str], int]:
    """(테이블, 컬럼) → 선언된 바이트 폭. 문자열 컬럼만."""
    return {(table, col.name): col.byte_length
            for table, cols in parse_columns(ddl_dir).items()
            for col in cols.values() if col.byte_length is not None}


def parse_sequences(ddl_dir: Path) -> set[str]:
    """CREATE SEQUENCE로 선언된 이름 집합. 시퀀스는 컬럼이 없으므로 parse_ddl의
    테이블 사전과는 별도로 둔다 (컬럼 없는 항목이 섞이면 parse_ddl의 계약이
    깨진다)."""
    return _names(ddl_dir, _CREATE_SEQUENCE)


def parse_indexes(ddl_dir: Path) -> set[str]:
    """명시적으로 CREATE INDEX한 이름만. PK/UNIQUE 제약이 만드는 인덱스는
    user_indexes에 제약과 같은 이름으로 나타나므로 여기 넣지 않는다 — 그쪽은
    parse_constraints로 대조한다."""
    return _names(ddl_dir, _CREATE_INDEX)


def parse_views(ddl_dir: Path) -> set[str]:
    return _names(ddl_dir, _CREATE_VIEW)


def parse_constraints(ddl_dir: Path) -> set[str]:
    """이름 붙인 제약만. 이름 없는 NOT NULL은 시스템 생성 이름이라 대조할 수 없다."""
    return _names(ddl_dir, _CONSTRAINT)


def _names(ddl_dir: Path, pattern: re.Pattern) -> set[str]:
    names: set[str] = set()
    for text in _sql_files(ddl_dir):
        names.update(n.upper() for n in pattern.findall(text))
    return names


def ddl_text(ddl_dir: Path) -> str:
    return "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted(Path(ddl_dir).glob("*.sql"))
    )
