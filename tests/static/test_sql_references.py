import importlib
import inspect
import pkgutil
import re

import pytest

from jira_dashboard.db import schema_map

SCANNED_PACKAGES = ["jira_dashboard.db.repository", "jira_dashboard.pipeline",
                   "jira_dashboard.doctor"]
_TABLE_TOKEN = re.compile(r"\b(TEST_\w+)\b", re.IGNORECASE)
_BIND = re.compile(r":(\w+)")
# SQL로 볼 최소 신호
_LOOKS_LIKE_SQL = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE|MERGE)\b", re.IGNORECASE
)


def _repository_modules():
    modules = []
    for package in SCANNED_PACKAGES:
        try:
            pkg = importlib.import_module(package)
        except ModuleNotFoundError:
            continue
        modules.extend(
            importlib.import_module(f"{package}.{m.name}")
            for m in pkgutil.iter_modules(pkg.__path__)
        )
    return modules


def _sql_literals(module) -> list[tuple[str, str]]:
    """모듈의 소스에서 SQL로 보이는 문자열 리터럴을 뽑는다."""
    source = inspect.getsource(module)
    out = []
    for m in re.finditer(r'"""(.*?)"""|\'\'\'(.*?)\'\'\'|"([^"\n]{20,})"',
                         source, re.DOTALL):
        text = next(g for g in m.groups() if g is not None)
        if _LOOKS_LIKE_SQL.search(text):
            out.append((module.__name__, text))
    return out


def _all_sql() -> list[tuple[str, str]]:
    return [item for mod in _repository_modules() for item in _sql_literals(mod)]


def test_every_referenced_table_exists(ddl_dir):
    """테이블 참조뿐 아니라 시퀀스 참조도 허용한다 (예: NEXTVAL 채번).

    parse_ddl은 테이블 전용 계약(test_parses_all_16_tables)을 지키므로 시퀀스는
    섞지 않고 parse_sequences로 따로 확인한다.
    """
    tables = schema_map.parse_ddl(ddl_dir)
    sequences = schema_map.parse_sequences(ddl_dir)
    for module_name, sql in _all_sql():
        for token in _TABLE_TOKEN.findall(sql):
            up = token.upper()
            assert up in tables or up in sequences, \
                f"{module_name}: unknown table {token}"


def test_every_referenced_column_exists(ddl_dir):
    """SQL에 등장하는 식별자 중, 참조된 테이블들의 컬럼 합집합에 없는 것을 찾는다."""
    tables = schema_map.parse_ddl(ddl_dir)
    sql_keywords = _load_keywords()
    for module_name, sql in _all_sql():
        referenced = {t.upper() for t in _TABLE_TOKEN.findall(sql)}
        if not referenced:
            continue
        allowed = set()
        for t in referenced:
            allowed |= tables.get(t, set())
        body = _TABLE_TOKEN.sub(" ", sql)
        body = _BIND.sub(" ", body)                    # 바인드 이름은 제외
        body = re.sub(r"'[^']*'", " ", body)           # 리터럴 제외
        body = re.sub(r"\{[^}]*\}", " ", body)         # {placeholders} 포맷 슬롯 제외
        for ident in re.findall(r"\b([a-z_][a-z0-9_]{2,})\b", body, re.IGNORECASE):
            up = ident.upper()
            if up in sql_keywords or up in allowed:
                continue
            pytest.fail(f"{module_name}: unknown identifier {ident!r} "
                        f"(tables: {sorted(referenced)})")


def test_no_string_interpolation_into_sql():
    """값은 전부 바인드 변수로. f-string이나 % 포매팅이 있으면 injection 경로다."""
    for mod in _repository_modules():
        source = inspect.getsource(mod)
        for m in re.finditer(r'f"""(.*?)"""|f"([^"\n]*)"', source, re.DOTALL):
            text = next(g for g in m.groups() if g is not None)
            if not _LOOKS_LIKE_SQL.search(text):
                continue
            # 화이트리스트 테이블명과 바인드 placeholder 조립만 허용
            allowed = ("{table}", "{placeholders}", "{counts}", "{distincts}")
            for token in re.findall(r"\{[^}]*\}", text):
                assert token in allowed, f"{mod.__name__}: interpolation {token}"


def _load_keywords() -> set[str]:
    return {
        "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "NULL", "INSERT", "INTO",
        "VALUES", "UPDATE", "SET", "DELETE", "MERGE", "USING", "WHEN", "MATCHED",
        "THEN", "ELSE", "END", "CASE", "ON", "AS", "JOIN", "LEFT", "RIGHT",
        "INNER", "OUTER", "GROUP", "BY", "ORDER", "HAVING", "COUNT", "SUM", "MIN",
        "MAX", "AVG", "DISTINCT", "EXISTS", "IN", "IS", "LIKE", "ESCAPE", "DUAL",
        "NVL", "COALESCE", "TRUNC", "CAST", "TIMESTAMP", "DATE", "INTERVAL",
        "SYSTIMESTAMP", "SYS_EXTRACT_UTC", "NUMTODSINTERVAL", "TO_CHAR", "SUBSTR",
        "NEXTVAL", "CURRVAL", "LEVEL", "CONNECT", "WITH", "UNION", "ALL", "FETCH",
        "FIRST", "ROWS", "ONLY", "ROW_NUMBER", "OVER", "PARTITION", "RETURNING",
        "HOUR", "DAY", "MONTH", "YEAR", "ASC", "DESC", "BETWEEN",
    }
