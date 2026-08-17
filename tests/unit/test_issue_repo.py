from jira_dashboard.db.repository import issue as issue_repo


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


def test_digest_does_not_depend_on_gzip_output():
    """다이제스트가 압축 결과가 아니라 원본 바이트를 대상으로 계산됨을 못박는다."""
    raw = issue_repo.canonical_json({"k": "v"})
    compressed = issue_repo.gzip_bytes(raw)

    assert issue_repo.sha256_hex(raw) != issue_repo.sha256_hex(compressed)
