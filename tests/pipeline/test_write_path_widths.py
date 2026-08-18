"""폭을 일부러 넘긴 데이터를 실제 쓰기 경로에 통과시킨다 (R33 / Important 6).

리포지토리 함수를 스텁하지 않는다 — 진짜 함수가 진짜 SQL과 진짜 바인드 dict를
만들고, WidthCheckingConn이 그 값들을 DDL에서 파생한 컬럼 폭과 대조한다. 어딘가에
truncate가 빠져 있으면 여기서 WidthViolation으로 터진다.

여기서 쓰는 "적대적 픽스처"는 사내 실데이터가 실제로 하는 짓을 흉내낸 것이다:
표시 이름이 긴 통합 계정, Sprint/다중선택 변경의 콤마 결합 id 목록, 한글 요약,
한글 에러 메시지.
"""
from datetime import datetime, timezone

import pytest

from jira_dashboard.db import schema_map
from jira_dashboard.db.repository import history as history_repo
from jira_dashboard.db.repository import sync as sync_repo
from jira_dashboard.doctor.db_checks import DDL_DIR
from jira_dashboard.jira import parser
from jira_dashboard.pipeline import derive_history as history_mod
from jira_dashboard.pipeline import sync_issues as mod
from tests.widths import WidthCheckingConn, WidthViolation

LONG_KR = "가나다라마바사아자차" * 60      # 600자 = 1800바이트
IDS = ",".join(str(10000 + i) for i in range(200))    # Sprint 변경의 id 목록


@pytest.fixture(scope="session")
def limits():
    return schema_map.column_byte_limits(DDL_DIR)


def _blow_up(fake_jira):
    """모든 문자열 값을 컬럼 폭 위로 부풀린다. 식별자(키/id)는 건드리지 않는다 —
    식별자는 자르지 않는 것이 옳고, 자르면 조용히 잘못된 데이터가 된다."""
    for issue in fake_jira._issues.values():
        f = issue["fields"]
        f["summary"] = LONG_KR
        f["assignee"] = {"key": LONG_KR, "name": "acct",
                         "displayName": LONG_KR}
        f["reporter"] = {"name": LONG_KR, "displayName": LONG_KR}
        for key in ("issuetype", "priority", "resolution"):
            if f.get(key):
                f[key]["name"] = LONG_KR
        if f.get("status"):
            f["status"]["name"] = LONG_KR
        for key, value in list(f.items()):
            if not key.startswith("customfield_"):
                continue
            if isinstance(value, dict):
                value["value"] = LONG_KR
                value["id"] = IDS
            elif isinstance(value, list):
                for element in value:
                    if isinstance(element, dict):
                        element["value"] = LONG_KR
                        element["id"] = IDS
        for h in (issue.get("changelog") or {}).get("histories") or []:
            h["author"] = {"key": LONG_KR, "displayName": LONG_KR}
            for item in h.get("items") or []:
                item["field"] = LONG_KR
                item["fromString"] = LONG_KR * 4
                item["toString"] = LONG_KR * 4
                item["from"] = IDS
                item["to"] = IDS


def _conn_for(fake_jira, limits):
    field_ids = [f["id"] for f in fake_jira.get_fields()] + ["status", "status_category"]
    return WidthCheckingConn(limits, answers={
        # load_existing — 전부 신규로 취급
        "SELECT i.jira_issue_id": [],
        # next_issue_ids
        "NEXTVAL": [(9000 + i,) for i in range(500)],
        # field_pk_by_field_id / dimension_field_pks
        "SELECT field_id, field_pk": [(fid, i + 1) for i, fid in enumerate(field_ids)],
        # field_pk_by_field_name
        "SELECT field_name, field_pk": [],
    })


def test_sync_issues_write_path_respects_every_column_width(fake_jira, limits):
    """C1/I5/I6/M17이 고친 truncate가 실제로 모든 바인드를 덮는지 확인한다."""
    _blow_up(fake_jira)
    conn = _conn_for(fake_jira, limits)
    result = mod.sync_issues(conn, fake_jira, 1, 7, "PROJ", None)

    assert result.upserted > 0, "적재가 실제로 일어나야 검사에 의미가 있다"
    assert any("test_issue_changelog" in s.lower() for s in conn.statements)
    assert any("test_issue_field_value" in s.lower() for s in conn.statements)


@pytest.mark.parametrize("module", [history_repo, parser])
def test_the_harness_fails_when_truncation_is_removed(fake_jira, limits, monkeypatch,
                                                     module):
    """장치가 진짜로 감시하고 있음을 증명한다 — 두 자리(changelog 쪽 history_repo,
    이슈/EAV 쪽 parser) 중 어느 쪽 truncate를 무력화해도 터져야 한다. 이게 통과하지
    않으면 위 테스트는 아무것도 주장하지 않는 셈이다."""
    _blow_up(fake_jira)
    monkeypatch.setattr(module, "truncate", lambda text, *a, **k: text)
    conn = _conn_for(fake_jira, limits)
    with pytest.raises(WidthViolation):
        mod.sync_issues(conn, fake_jira, 1, 7, "PROJ", None)


def test_derived_history_rows_respect_val_str_and_val_id_widths(limits):
    """이력 파생은 changelog 컬럼(4000/255)에서 읽어 이력 컬럼(1000/100)에 쓴다 —
    폭이 줄어드는 구간이라 자르지 않으면 반드시 넘친다."""
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    changed = datetime(2026, 2, 1, tzinfo=timezone.utc)
    conn = WidthCheckingConn(limits, answers={
        "SELECT field_id, field_pk": [("status", 1)],
        "SELECT issue_id, created_at": [
            (9000, created, LONG_KR, LONG_KR, None, None, None, None, None, None, None)
        ],
        "SELECT issue_id, field_pk, val_str": [(9000, 1, LONG_KR, IDS)],
        "SELECT c.issue_id": [
            (9000, "9001", 0, changed, "status", "status",
             IDS, LONG_KR * 4, IDS, LONG_KR * 4),
        ],
    })
    written = history_mod.derive_history(conn, 1, [9000],
                                        category_of={LONG_KR: "done"})
    assert written > 0
    assert any("test_issue_field_history" in s.lower() for s in conn.statements)


def test_finish_run_error_message_respects_the_4000_byte_column(limits):
    """문자 단위로 자르면 한글 에러가 3배로 부풀어 ORA-12899가 나고, *처리된*
    프로젝트 실패가 처리되지 않은 예외로 바뀌면서 원래 에러까지 사라진다."""
    conn = WidthCheckingConn(limits)
    sync_repo.finish_run(conn, 1, "FAILED", error=LONG_KR * 10)


def test_watermark_and_run_rows_respect_their_widths(limits):
    conn = WidthCheckingConn(limits)
    sync_repo.write_watermark(conn, 7, datetime(2026, 1, 1, tzinfo=timezone.utc),
                              "SUCCESS")
    sync_repo.request_full_resync(conn, 7)
    assert sync_repo.start_run(conn, 1, 7, "HISTORY") == 1
