import re

from jira_dashboard.db import schema_map


def test_all_objects_have_test_prefix(ddl_dir):
    text = schema_map.ddl_text(ddl_dir)
    bad = re.findall(
        r"CREATE\s+(?:TABLE|INDEX|VIEW|SEQUENCE|OR\s+REPLACE\s+VIEW)\s+(\w+)",
        text, re.IGNORECASE,
    )
    offenders = [n for n in bad if not n.upper().startswith("TEST_")]
    assert offenders == [], offenders


def test_all_constraints_have_test_prefix(ddl_dir):
    text = schema_map.ddl_text(ddl_dir)
    names = re.findall(r"CONSTRAINT\s+(\w+)", text, re.IGNORECASE)
    offenders = [n for n in names if not n.upper().startswith("TEST_")]
    assert offenders == [], offenders


def test_varchar2_always_declares_byte_semantics(ddl_dir):
    text = schema_map.ddl_text(ddl_dir)
    offenders = re.findall(r"VARCHAR2\s*\(\s*\d+\s*\)", text, re.IGNORECASE)
    assert offenders == [], offenders


def test_defaults_use_kst(ddl_dir):
    """DEFAULT SYSTIMESTAMP 는 세션 타임존에 따라 값이 흔들린다 (spec 2.1). 저장 규약은
    KST이므로 UTC로 정규화한 뒤 고정 +9시간을 더한 표현이어야 한다."""
    text = schema_map.ddl_text(ddl_dir)
    bare = re.findall(r"DEFAULT\s+SYSTIMESTAMP", text, re.IGNORECASE)
    assert bare == [], bare
    assert "SYS_EXTRACT_UTC(SYSTIMESTAMP) + INTERVAL '9' HOUR" in text


def test_no_syntax_newer_than_19c(ddl_dir):
    text = schema_map.ddl_text(ddl_dir).upper()
    for forbidden in ("IF NOT EXISTS", " BOOLEAN", "JSON_TABLE", " AS JSON"):
        assert forbidden not in text, forbidden


def test_no_function_based_indexes_or_virtual_columns(ddl_dir):
    """spec 2.4: 인덱스는 단순 B-tree만."""
    text = schema_map.ddl_text(ddl_dir)
    assert not re.search(r"GENERATED\s+ALWAYS\s+AS\s*\(", text, re.IGNORECASE)
    for m in re.finditer(r"CREATE\s+INDEX[^;]+\(([^)]*)\)", text, re.IGNORECASE):
        assert "(" not in m.group(1), m.group(0)


def test_history_sentinel_is_used(ddl_dir):
    text = schema_map.ddl_text(ddl_dir)
    assert "TIMESTAMP '9999-12-31 00:00:00'" in text


def test_drop_all_escapes_the_underscore(ddl_dir):
    """ESCAPE 누락은 남의 테이블을 지운다. 사외에서 실행 검증이 불가하므로 문자열로 막는다."""
    text = (ddl_dir / "drop_all.sql").read_text(encoding="utf-8")
    like_clauses = re.findall(r"LIKE\s+'([^']*)'(\s+ESCAPE\s+'[^']*')?",
                             text, re.IGNORECASE)
    assert like_clauses, "no LIKE clause found"
    for pattern, escape in like_clauses:
        assert escape, f"LIKE '{pattern}' has no ESCAPE clause"
        assert pattern.startswith("TEST\\_"), pattern
    assert "PURGE" in text.upper()
    assert "CASCADE CONSTRAINTS" in text.upper()
