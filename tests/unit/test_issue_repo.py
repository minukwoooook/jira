import pytest

from jira_dashboard.db.repository import issue as issue_repo
from tests.stubs import CONN


def test_hash_of_canonical_json_is_stable_across_separate_calls():
    """Correction 1의 핵심: 다이제스트는 gzip 프레이밍이 아니라 정규 JSON 바이트를
    대상으로 해야 한다. 같은 객체를 두 번 따로 정규화/해시해도 같은 값이 나와야
    스킵 경로가 살아 있다."""
    obj = {"b": 1, "a": [3, 2, 1], "nested": {"z": 1, "y": 2}}

    digest_1 = issue_repo.sha256_hex(issue_repo.canonical_json(obj))
    digest_2 = issue_repo.sha256_hex(issue_repo.canonical_json(dict(obj)))

    assert digest_1 == digest_2


def test_gzip_bytes_is_reproducible_for_identical_input():
    """gzip.compress()는 기본적으로 헤더에 현재 시각을 적어 넣어 같은 입력도 매번
    다른 바이트를 낸다. mtime=0으로 고정해야 두 번째 실행에서도 같은 압축 결과가
    나온다."""
    raw = issue_repo.canonical_json({"k": "v"})

    compressed_1 = issue_repo.gzip_bytes(raw)
    compressed_2 = issue_repo.gzip_bytes(raw)

    assert compressed_1 == compressed_2


def test_upsert_raw_rejects_unknown_table():
    """Item 4: _RAW_TABLES 화이트리스트가 유일한 injection 방어선이고 정적 게이트는
    f-string만 스캔하므로 이 .format() 지점은 그 게이트에 안 잡힌다 — 여기서 직접 덮는다."""
    with pytest.raises(ValueError):
        issue_repo.upsert_raw(
            CONN, "drop_table_students",
            [{"issue_id": 1, "payload": b"x", "payload_hash": "h"}],
        )


def test_merge_issue_revives_moved_out_issues_on_whitelist():
    """Item 5 / spec §5.6: MOVED_OUT으로 표시된 이슈가 화이트리스트에 다시 들어오면
    되살아나야 한다. 이 문자열이 사라지면 사외에서는 아무것도 눈치채지 못하고
    온프레미스 화이트리스트 변경 때에야 드러난다."""
    assert "deleted_at = NULL" in issue_repo._MERGE_ISSUE
    assert "delete_reason = NULL" in issue_repo._MERGE_ISSUE
