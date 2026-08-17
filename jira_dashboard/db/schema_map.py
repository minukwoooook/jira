"""DDL 파일에서 테이블→컬럼 사전을 만든다.

완전한 SQL 파서가 아니다. CREATE TABLE 블록의 컬럼 정의만 뽑는 수준이면
컬럼명 오타를 잡기에 충분하다 (spec §11.2).
"""
import re
from pathlib import Path

_CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(\w+)\s*\((.*?)\)\s*(?:LOB\s*\(|;|$)",
    re.IGNORECASE | re.DOTALL,
)
_CREATE_SEQUENCE = re.compile(r"CREATE\s+SEQUENCE\s+(\w+)", re.IGNORECASE)
# 컬럼 정의가 아닌 줄
_NOT_A_COLUMN = re.compile(
    r"^\s*(CONSTRAINT|PRIMARY|UNIQUE|FOREIGN|CHECK)\b", re.IGNORECASE
)
_COLUMN = re.compile(r"^\s*(\w+)\s+\S")


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


def parse_ddl(ddl_dir: Path) -> dict[str, set[str]]:
    tables: dict[str, set[str]] = {}
    for path in sorted(Path(ddl_dir).glob("[0-9][0-9]_*.sql")):
        text = path.read_text(encoding="utf-8")
        # 주석 제거
        text = re.sub(r"--[^\n]*", "", text)
        for name, body in _CREATE_TABLE.findall(text):
            cols = set()
            for chunk in _split_top_level(body):
                if _NOT_A_COLUMN.match(chunk):
                    continue
                m = _COLUMN.match(chunk)
                if m:
                    cols.add(m.group(1).upper())
            tables[name.upper()] = cols
    return tables


def parse_sequences(ddl_dir: Path) -> set[str]:
    """CREATE SEQUENCE로 선언된 이름 집합. 시퀀스는 컬럼이 없으므로 parse_ddl의
    테이블 사전과는 별도로 둔다 (컬럼 없는 항목이 섞이면 parse_ddl의 계약이
    깨진다)."""
    names: set[str] = set()
    for path in sorted(Path(ddl_dir).glob("[0-9][0-9]_*.sql")):
        text = re.sub(r"--[^\n]*", "", path.read_text(encoding="utf-8"))
        names.update(n.upper() for n in _CREATE_SEQUENCE.findall(text))
    return names


def ddl_text(ddl_dir: Path) -> str:
    return "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted(Path(ddl_dir).glob("*.sql"))
    )
