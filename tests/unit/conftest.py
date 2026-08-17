import pytest

from jira_dashboard.jira import parser

_FIELDS = [
    {"id": "summary", "name": "Summary", "custom": False,
     "schema": {"type": "string"}},
    {"id": "status", "name": "Status", "custom": False,
     "schema": {"type": "status"}},
    {"id": "labels", "name": "Labels", "custom": False,
     "schema": {"type": "array", "items": "string"}},
    {"id": "created", "name": "Created", "custom": False,
     "schema": {"type": "datetime"}},
    {"id": "updated", "name": "Updated", "custom": False,
     "schema": {"type": "datetime"}},
    {"id": "customfield_10001", "name": "결함원인", "custom": True,
     "schema": {"type": "option", "custom": "…:select"}},
]


@pytest.fixture
def field_index():
    return {fd.field_id: fd for fd in parser.parse_field_defs(_FIELDS)}


@pytest.fixture
def sample_issue():
    return {
        "id": "10100", "key": "PROJ-1",
        "fields": {
            "project": {"id": "10000", "key": "PROJ"},
            "summary": "sample",
            "status": {"name": "완료", "id": "10",
                       "statusCategory": {"id": 3, "key": "done"}},
            "labels": ["urgent", "ux"],
            "created": "2026-01-01T09:00:00.000+0900",
            "updated": "2026-06-01T09:00:00.000+0900",
            "customfield_10001": {"value": "Regression", "id": "10100"},
        },
        "changelog": {"startAt": 0, "maxResults": 100, "total": 1, "histories": [{
            "id": "1", "created": "2026-03-01T09:00:00.000+0900",
            "author": {"key": "jdoe", "displayName": "Jane"},
            "items": [{"field": "status", "fieldId": "status",
                       "fromString": "To Do", "toString": "완료"}],
        }]},
    }
