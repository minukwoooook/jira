from jira_dashboard.db import schema_map


def test_parses_all_16_tables(ddl_dir):
    tables = schema_map.parse_ddl(ddl_dir)
    assert len(tables) == 16, sorted(tables)


def test_table_names_are_uppercase(ddl_dir):
    tables = schema_map.parse_ddl(ddl_dir)
    assert all(t == t.upper() for t in tables)


def test_extracts_columns_of_jira_issue(ddl_dir):
    tables = schema_map.parse_ddl(ddl_dir)
    cols = tables["TEST_JIRA_ISSUE"]
    for expected in ("ISSUE_ID", "STATUS_CATEGORY", "FIRST_DONE_AT",
                     "DELETE_REASON", "SYNCED_AT", "TIME_SPENT_SEC"):
        assert expected in cols, f"{expected} missing from {sorted(cols)}"


def test_does_not_treat_constraints_as_columns(ddl_dir):
    cols = schema_map.parse_ddl(ddl_dir)["TEST_JIRA_ISSUE"]
    assert not any(c.startswith("TEST_PK") or c.startswith("TEST_CK") for c in cols)
    assert "CONSTRAINT" not in cols
    assert "PRIMARY" not in cols


def test_extracts_columns_of_eav_table(ddl_dir):
    cols = schema_map.parse_ddl(ddl_dir)["TEST_ISSUE_FIELD_VALUE"]
    assert cols == {"ISSUE_ID", "FIELD_PK", "VAL_SEQ", "VAL_STR",
                    "VAL_NUM", "VAL_DATE", "VAL_ID"}


def test_lob_clause_is_not_parsed_as_column(ddl_dir):
    """05_raw.sql의 LOB (payload) STORE AS ... 절이 컬럼으로 오인되면 안 된다."""
    cols = schema_map.parse_ddl(ddl_dir)["TEST_ISSUE_RAW"]
    assert cols == {"ISSUE_ID", "PAYLOAD", "PAYLOAD_HASH", "FETCHED_AT"}
