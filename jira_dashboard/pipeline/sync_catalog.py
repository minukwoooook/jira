from jira_dashboard.db.repository.catalog import (
    FieldChangeReport, upsert_fields, upsert_projects,
)
from jira_dashboard.jira.fieldmap import SYNTHETIC_FIELDS
from jira_dashboard.jira.models import FieldDef
from jira_dashboard.jira.parser import parse_field_defs
from jira_dashboard.jira.protocol import JiraClient

_SYNTHETIC_SCHEMA = {"status_category": "string", "first_done_at": "datetime"}


def _synthetic_defs() -> list[FieldDef]:
    """/rest/api/2/field 에 없는 필드. 쿼리 API가 다른 필드와 똑같이 참조하려면 필요하다."""
    return [
        FieldDef(field_id=field_id, field_name=name, is_custom=False,
                 schema_type=_SYNTHETIC_SCHEMA[field_id],
                 schema_items=None, custom_type=None)
        for field_id, name in SYNTHETIC_FIELDS.items()
    ]


def sync_catalog(conn, client: JiraClient, instance_id: int) -> FieldChangeReport:
    defs = parse_field_defs(client.get_fields()) + _synthetic_defs()
    value_kind_changed = upsert_fields(conn, instance_id, defs)
    key_changed = upsert_projects(conn, instance_id, client.get_projects())
    return FieldChangeReport(value_kind_changed=value_kind_changed,
                            key_changed_projects=key_changed)
