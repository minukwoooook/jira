from dataclasses import dataclass, field

from jira_dashboard.jira.fieldmap import SYSTEM_FIELD_MAP, value_kind_of
from jira_dashboard.jira.models import FieldDef

_MERGE_INSTANCE = """
MERGE INTO test_jira_instance t
USING (SELECT :instance_key AS instance_key FROM dual) s
ON (t.instance_key = s.instance_key)
WHEN MATCHED THEN UPDATE SET t.base_url = :base_url, t.auth_type = :auth_type,
     t.secret_ref = :secret_ref, t.updated_at = SYS_EXTRACT_UTC(SYSTIMESTAMP)
WHEN NOT MATCHED THEN
  INSERT (instance_key, base_url, auth_type, secret_ref)
  VALUES (:instance_key, :base_url, :auth_type, :secret_ref)
"""

_SELECT_INSTANCE_ID = """
SELECT instance_id FROM test_jira_instance WHERE instance_key = :instance_key
"""

_SELECT_PROJECTS = """
SELECT jira_project_id, project_id, project_key
FROM   test_jira_project WHERE instance_id = :instance_id
"""

_MERGE_PROJECT = """
MERGE INTO test_jira_project t
USING (SELECT :instance_id AS instance_id,
              :jira_project_id AS jira_project_id FROM dual) s
ON (t.instance_id = s.instance_id AND t.jira_project_id = s.jira_project_id)
WHEN MATCHED THEN UPDATE SET t.project_key = :project_key, t.name = :name,
     t.updated_at = SYS_EXTRACT_UTC(SYSTIMESTAMP)
WHEN NOT MATCHED THEN
  INSERT (instance_id, jira_project_id, project_key, name)
  VALUES (:instance_id, :jira_project_id, :project_key, :name)
"""

_SELECT_PROJECT_IDS = """
SELECT jira_project_id, project_id FROM test_jira_project WHERE instance_id = :instance_id
"""

_SELECT_ENABLED = """
SELECT project_id, jira_project_id, project_key
FROM   test_jira_project
WHERE  instance_id = :instance_id AND is_enabled = 'Y'
ORDER  BY project_key
"""

_SELECT_FIELD_KINDS = """
SELECT field_id, value_kind FROM test_jira_field WHERE instance_id = :instance_id
"""

_MERGE_FIELD = """
MERGE INTO test_jira_field t
USING (SELECT :instance_id AS instance_id, :field_id AS field_id FROM dual) s
ON (t.instance_id = s.instance_id AND t.field_id = s.field_id)
WHEN MATCHED THEN UPDATE SET
  t.field_name = :field_name, t.schema_type = :schema_type,
  t.schema_items = :schema_items, t.custom_type = :custom_type,
  t.value_kind = :value_kind, t.storage_kind = :storage_kind,
  t.column_name = :column_name, t.label_column_name = :label_column_name,
  t.is_dimension = :is_dimension, t.is_measure = :is_measure,
  t.last_seen_at = SYS_EXTRACT_UTC(SYSTIMESTAMP)
WHEN NOT MATCHED THEN
  INSERT (instance_id, field_id, field_name, is_custom, schema_type, schema_items,
          custom_type, value_kind, storage_kind, column_name, label_column_name,
          is_dimension, is_measure)
  VALUES (:instance_id, :field_id, :field_name, :is_custom, :schema_type,
          :schema_items, :custom_type, :value_kind, :storage_kind, :column_name,
          :label_column_name, :is_dimension, :is_measure)
"""

_SELECT_FIELD_PKS = """
SELECT field_id, field_pk FROM test_jira_field WHERE instance_id = :instance_id
"""

_SELECT_FIELD_PKS_BY_NAME = """
SELECT field_name, field_pk FROM test_jira_field WHERE instance_id = :instance_id
"""


@dataclass
class FieldChangeReport:
    value_kind_changed: list[str] = field(default_factory=list)
    key_changed_projects: list[int] = field(default_factory=list)


def storage_for(fd: FieldDef) -> tuple[str, str | None, str | None, str, str, str]:
    """(storage_kind, column_name, label_column_name, value_kind, is_dim, is_msr)"""
    kind = value_kind_of(fd.schema_type, fd.schema_items)
    spec = SYSTEM_FIELD_MAP.get(fd.field_id)
    # 다중값은 고정 컬럼에 담을 수 없다. labels/components/fixVersions가 여기 걸린다.
    if spec is None or kind == "MULTI":
        return ("EAV", None, None, kind, "Y", "N")
    return (
        "COLUMN", spec.column_name, spec.label_column_name, spec.value_kind,
        "Y" if spec.is_dimension else "N",
        "Y" if spec.is_measure else "N",
    )


def upsert_instance(conn, instance_key, base_url, auth_type, secret_ref) -> int:
    cur = conn.cursor()
    cur.execute(_MERGE_INSTANCE, instance_key=instance_key, base_url=base_url,
                auth_type=auth_type, secret_ref=secret_ref)
    cur.execute(_SELECT_INSTANCE_ID, instance_key=instance_key)
    return cur.fetchone()[0]


def upsert_projects(conn, instance_id: int, projects: list[dict]) -> list[int]:
    """is_enabled는 절대 덮지 않는다 — 화이트리스트는 사람이 정한다 (spec §5.1)."""
    cur = conn.cursor()
    cur.execute(_SELECT_PROJECTS, instance_id=instance_id)
    existing = {jid: (pid, key) for jid, pid, key in cur.fetchall()}

    key_changed, rows = [], []
    for p in projects:
        jid = str(p["id"])
        if jid in existing and existing[jid][1] != p["key"]:
            key_changed.append(existing[jid][0])
        rows.append({"instance_id": instance_id, "jira_project_id": jid,
                     "project_key": p["key"], "name": p.get("name")})
    if rows:
        cur.executemany(_MERGE_PROJECT, rows, batcherrors=False)
    return key_changed


def project_id_by_jira_id(conn, instance_id: int) -> dict[str, int]:
    cur = conn.cursor()
    cur.execute(_SELECT_PROJECT_IDS, instance_id=instance_id)
    return {j: p for j, p in cur.fetchall()}


def enabled_projects(conn, instance_id: int) -> list[tuple[int, str, str]]:
    cur = conn.cursor()
    cur.execute(_SELECT_ENABLED, instance_id=instance_id)
    return list(cur.fetchall())


def upsert_fields(conn, instance_id: int, defs: list[FieldDef]) -> list[str]:
    """value_kind가 바뀐 field_id 목록을 반환한다 (spec §4.2)."""
    cur = conn.cursor()
    cur.execute(_SELECT_FIELD_KINDS, instance_id=instance_id)
    previous = dict(cur.fetchall())

    changed, rows = [], []
    for fd in defs:
        storage, column, label, kind, dim, msr = storage_for(fd)
        if fd.field_id in previous and previous[fd.field_id] != kind:
            changed.append(fd.field_id)
        rows.append({
            "instance_id": instance_id, "field_id": fd.field_id,
            "field_name": fd.field_name,
            "is_custom": "Y" if fd.is_custom else "N",
            "schema_type": fd.schema_type, "schema_items": fd.schema_items,
            "custom_type": fd.custom_type, "value_kind": kind,
            "storage_kind": storage, "column_name": column,
            "label_column_name": label, "is_dimension": dim, "is_measure": msr,
        })
    if rows:
        cur.executemany(_MERGE_FIELD, rows, batcherrors=False)
    return changed


def field_pk_by_field_id(conn, instance_id: int) -> dict[str, int]:
    cur = conn.cursor()
    cur.execute(_SELECT_FIELD_PKS, instance_id=instance_id)
    return {f: pk for f, pk in cur.fetchall()}


def field_pk_by_field_name(conn, instance_id: int) -> dict[str, int]:
    """이름 → field_pk. 인스턴스 안에서 중복되는 이름은 제외한다."""
    cur = conn.cursor()
    cur.execute(_SELECT_FIELD_PKS_BY_NAME, instance_id=instance_id)
    rows = cur.fetchall()
    counts: dict[str, int] = {}
    for name, _ in rows:
        counts[name] = counts.get(name, 0) + 1
    return {name: pk for name, pk in rows if counts[name] == 1}
