from datetime import datetime, timedelta, timezone

import pytest

from jira_dashboard.db.repository import issue as issue_repo
from jira_dashboard.pipeline import sync_issues as mod
from tests.stubs import CONN, Recorder

FIELD_PKS = {
    "labels": 101, "customfield_10001": 102, "customfield_10002": 103,
    "customfield_10003": 104, "customfield_10004": 105,
}


@pytest.fixture
def rec(monkeypatch):
    r = Recorder()
    r.returns["load_existing"] = lambda *a, **k: {}
    r.returns["next_issue_ids"] = lambda conn, n: list(range(9000, 9000 + n))
    r.patch(monkeypatch, mod.issue_repo,
            "load_existing", "next_issue_ids", "upsert_issues", "touch_synced_at",
            "upsert_raw", "replace_field_values")
    r.patch(monkeypatch, mod.history_repo, "upsert_changelog")
    monkeypatch.setattr(mod, "field_pk_by_field_id", lambda conn, i: FIELD_PKS)
    # Correction 3: upsert_changelog resolves by name too, so sync_issues must
    # also fetch the name index. Stub it out to keep these tests DB-free.
    monkeypatch.setattr(mod, "field_pk_by_field_name", lambda conn, i: {})
    monkeypatch.setattr(CONN, "commit", lambda: None, raising=False)
    return r


def _run(fake_jira, page_size=100, since=None):
    return mod.sync_issues(CONN, fake_jira, 1, 7, "PROJ", since, page_size=page_size)


# --- 순수 함수 ---

def test_build_jql_uses_epoch_when_no_watermark():
    jql = mod.build_jql("PROJ", None)
    assert 'updated >= "1970-01-01 00:00"' in jql
    assert jql.endswith("ORDER BY updated ASC")


def test_build_jql_formats_watermark():
    since = datetime(2026, 5, 1, 9, 30, tzinfo=timezone.utc)
    assert 'updated >= "2026-05-01 09:30"' in mod.build_jql("PROJ", since)


def test_next_watermark_subtracts_overlap():
    """겹침 구간이 없으면 페이징 중 수정된 이슈를 놓친다 (spec 5.2)."""
    m = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    assert mod.next_watermark(m, None) == m - timedelta(minutes=5)


def test_next_watermark_keeps_previous_when_nothing_fetched():
    prev = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert mod.next_watermark(None, prev) == prev


# --- 적재 순서 (spec 5.2 ①~⑥) ---

def test_reads_existing_state_before_any_write(rec, fake_jira):
    _run(fake_jira)
    names = rec.names()
    assert names[0] == "load_existing", names[:3]


def test_issues_are_written_before_raw(rec, fake_jira):
    """ISSUE_RAW가 JIRA_ISSUE를 FK로 참조하므로 순서를 뒤집으면 ORA-02291."""
    _run(fake_jira)
    i_issues, i_raw = rec.order_of("upsert_issues", "upsert_raw")
    assert i_issues < i_raw


def test_eav_and_changelog_come_after_issues(rec, fake_jira):
    _run(fake_jira)
    i_issues, i_eav, i_chg = rec.order_of(
        "upsert_issues", "replace_field_values", "upsert_changelog"
    )
    assert i_issues < i_eav
    assert i_issues < i_chg


def test_both_raw_tables_are_written(rec, fake_jira):
    _run(fake_jira)
    tables = {call["args"][0] for call in rec.args_of("upsert_raw")}
    assert tables == {"test_issue_raw", "test_changelog_raw"}


# --- 해시 스킵 ---

def test_unchanged_issue_only_touches_synced_at(monkeypatch, fake_jira):
    """1회차 해시를 그대로 돌려주면 2회차는 전부 스킵되어야 한다."""
    first = Recorder()
    first.returns["load_existing"] = lambda *a, **k: {}
    first.returns["next_issue_ids"] = lambda conn, n: list(range(1, n + 1))
    first.patch(monkeypatch, mod.issue_repo,
                "load_existing", "next_issue_ids", "upsert_issues",
                "touch_synced_at", "upsert_raw", "replace_field_values")
    first.patch(monkeypatch, mod.history_repo, "upsert_changelog")
    monkeypatch.setattr(mod, "field_pk_by_field_id", lambda conn, i: FIELD_PKS)
    monkeypatch.setattr(mod, "field_pk_by_field_name", lambda conn, i: {})
    monkeypatch.setattr(CONN, "commit", lambda: None, raising=False)
    _run(fake_jira)

    hashes = {}
    for call in first.args_of("upsert_raw"):
        if call["args"][0] != "test_issue_raw":
            continue
        for row in call["args"][1]:
            hashes[row["issue_id"]] = row["payload_hash"]
    keys = {}
    for call in first.args_of("upsert_issues"):
        for row in call["args"][0]:
            keys[row["jira_issue_id"]] = row["issue_id"]

    second = Recorder()
    second.returns["load_existing"] = lambda conn, inst, ids: {
        jid: issue_repo.ExistingIssue(keys[jid], hashes[keys[jid]])
        for jid in ids if jid in keys
    }
    second.returns["next_issue_ids"] = lambda conn, n: list(range(5000, 5000 + n))
    second.patch(monkeypatch, mod.issue_repo,
                 "load_existing", "next_issue_ids", "upsert_issues",
                 "touch_synced_at", "upsert_raw", "replace_field_values")
    second.patch(monkeypatch, mod.history_repo, "upsert_changelog")
    result = _run(fake_jira)

    assert result.upserted == 0
    assert result.skipped > 0
    assert second.count("upsert_issues") == 0
    assert second.count("touch_synced_at") > 0


def test_skipped_issues_still_get_synced_at_touched(rec, fake_jira):
    """마지막으로 확인한 시각이 정확해야 삭제 감지가 오래된 행을 구분한다."""
    _run(fake_jira)
    assert rec.count("touch_synced_at") >= 1


# --- 페이징 ---

def test_paging_advances_by_response_max_results(monkeypatch, fixture_dir):
    """A7: 서버가 요청보다 작은 페이지를 줄 수 있다. 응답값으로 전진해야 한다."""
    from jira_dashboard.jira.fake import FakeJiraClient

    r = Recorder()
    r.returns["load_existing"] = lambda *a, **k: {}
    r.returns["next_issue_ids"] = lambda conn, n: list(range(1, n + 1))
    r.patch(monkeypatch, mod.issue_repo,
            "load_existing", "next_issue_ids", "upsert_issues", "touch_synced_at",
            "upsert_raw", "replace_field_values")
    r.patch(monkeypatch, mod.history_repo, "upsert_changelog")
    monkeypatch.setattr(mod, "field_pk_by_field_id", lambda conn, i: FIELD_PKS)
    monkeypatch.setattr(mod, "field_pk_by_field_name", lambda conn, i: {})
    monkeypatch.setattr(CONN, "commit", lambda: None, raising=False)

    client = FakeJiraClient(fixture_dir, server_max_results=2)
    total = client.search_issues("project = PROJ", 0, 500, False).total
    result = mod.sync_issues(CONN, client, 1, 7, "PROJ", None, page_size=100)
    assert result.fetched == total


def test_overlap_window_appears_in_jql(rec, fake_jira):
    """겹침 구간이 살아있는지 사외에서 확인하는 방법. OVERLAP=0이면 실패해야 한다.

    since를 픽스처 범위보다 이전으로 잡아 실제로 이슈가 반환되게 한다 —
    그래야 result.max_updated가 채워져 겹침 구간 비교가 의미를 가진다.
    """
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = mod.sync_issues(CONN, fake_jira, 1, 7, "PROJ", since)
    assert mod.next_watermark(result.max_updated, since) < result.max_updated


# --- changelog 보충 (A3) ---

def test_fetches_changelog_beyond_inline_limit(rec, fake_jira):
    _run(fake_jira)
    counts = {}
    for call in rec.args_of("upsert_changelog"):
        issue_id, items = call["args"][0], call["args"][1]
        counts[issue_id] = len(items)
    assert max(counts.values()) > 100, counts


class _StuckChangelogClient:
    """모든 get_issue_changelog 호출에 항상 같은 첫 슬라이스를 되돌려주는 스텁.

    Correction 2: DC 10.3에는 changelog 페이징이 없다 (/issue/{key}에 startAt이
    없고, 별도 changelog 엔드포인트도 없다). 진행 없이 같은 슬라이스를 반복해서
    주는 서버를 흉내내, 무한 루프에 빠지지 않고 멈추는지 검증한다.
    """

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def get_issue_changelog(self, issue_key, start_at):
        return self._inner.get_issue_changelog(issue_key, 0)


def test_changelog_supplemental_fetch_does_not_spin_on_no_progress(rec, fake_jira):
    client = _StuckChangelogClient(fake_jira)
    result = mod.sync_issues(CONN, client, 1, 7, "PROJ", None, page_size=100)

    assert result.changelog_truncated >= 1
    for call in rec.args_of("upsert_changelog"):
        items = call["args"][1]
        keys = [(item.history_id, item.item_seq) for item in items]
        assert len(keys) == len(set(keys)), "duplicate changelog entries"
        assert len(items) <= 100, "server never progressed past the inline slice"


# --- 파싱 실패 격리 (spec 5.8) ---

def test_unparseable_issue_is_skipped_not_fatal(rec, fake_jira):
    page = fake_jira.search_issues("project = PROJ ORDER BY updated ASC", 0, 100, True)
    total = page.total
    victim = str(page.issues[0]["id"])
    fake_jira._issues[victim]["fields"]["created"] = "not-a-timestamp"

    result = _run(fake_jira)
    assert result.parse_failures == 1
    assert result.upserted == total - 1


def test_parse_failure_does_not_consume_a_sequence_id(rec, fake_jira):
    """실패한 이슈가 채번을 소진하면 뒤 이슈들이 id를 못 받는다."""
    page = fake_jira.search_issues("project = PROJ ORDER BY updated ASC", 0, 100, True)
    victim = str(page.issues[0]["id"])
    fake_jira._issues[victim]["fields"]["created"] = "not-a-timestamp"

    result = _run(fake_jira)
    written = [row["issue_id"] for call in rec.args_of("upsert_issues")
               for row in call["args"][0]]
    assert len(written) == len(set(written))
    assert len(written) == result.upserted


# --- EAV 매핑 ---

def test_eav_rows_carry_resolved_field_pks(rec, fake_jira):
    _run(fake_jira)
    for call in rec.args_of("replace_field_values"):
        for row in call["args"][1]:
            assert row["field_pk"] in FIELD_PKS.values()


def test_issue_with_no_custom_values_gets_empty_replace(rec, fake_jira):
    """값이 없어도 replace를 불러야 옛 행이 지워진다."""
    _run(fake_jira)
    payloads = [call["args"][1] for call in rec.args_of("replace_field_values")]
    assert any(p == [] for p in payloads), "fixture #2 should produce no EAV rows"


def test_status_category_is_a_key_not_a_name(rec, fake_jira):
    _run(fake_jira)
    valid = {"new", "indeterminate", "done", "undefined"}
    for call in rec.args_of("upsert_issues"):
        for row in call["args"][0]:
            assert row["status_category"] in valid, row["status_category"]


# --- Correction 1: 해시는 gzip 프레이밍이 아니라 정규 JSON을 대상으로 한다 ---

def test_payload_hash_is_stable_across_pipeline_runs(monkeypatch, fake_jira):
    """gzip.compress()는 헤더에 시각을 넣어 같은 입력도 매번 다른 바이트를 낸다.
    압축 결과를 해시하면 2회차 실행에서 해시가 절대 일치하지 않아 스킵 경로가
    죽는다 (Correction 1). 이 테스트가 그 회귀를 잡는다."""
    r1 = Recorder()
    r1.returns["load_existing"] = lambda *a, **k: {}
    r1.returns["next_issue_ids"] = lambda conn, n: list(range(1, n + 1))
    r1.patch(monkeypatch, mod.issue_repo,
             "load_existing", "next_issue_ids", "upsert_issues", "touch_synced_at",
             "upsert_raw", "replace_field_values")
    r1.patch(monkeypatch, mod.history_repo, "upsert_changelog")
    monkeypatch.setattr(mod, "field_pk_by_field_id", lambda conn, i: FIELD_PKS)
    monkeypatch.setattr(mod, "field_pk_by_field_name", lambda conn, i: {})
    monkeypatch.setattr(CONN, "commit", lambda: None, raising=False)
    _run(fake_jira)
    hashes_1 = {
        row["issue_id"]: row["payload_hash"]
        for call in r1.args_of("upsert_raw") if call["args"][0] == "test_issue_raw"
        for row in call["args"][1]
    }

    r2 = Recorder()
    r2.returns["load_existing"] = lambda *a, **k: {}
    r2.returns["next_issue_ids"] = lambda conn, n: list(range(1, n + 1))
    r2.patch(monkeypatch, mod.issue_repo,
             "load_existing", "next_issue_ids", "upsert_issues", "touch_synced_at",
             "upsert_raw", "replace_field_values")
    r2.patch(monkeypatch, mod.history_repo, "upsert_changelog")
    monkeypatch.setattr(mod, "field_pk_by_field_id", lambda conn, i: FIELD_PKS)
    monkeypatch.setattr(mod, "field_pk_by_field_name", lambda conn, i: {})
    monkeypatch.setattr(CONN, "commit", lambda: None, raising=False)
    from jira_dashboard.jira.fake import FakeJiraClient
    client2 = FakeJiraClient(fake_jira._dir)
    _run(client2)
    hashes_2 = {
        row["issue_id"]: row["payload_hash"]
        for call in r2.args_of("upsert_raw") if call["args"][0] == "test_issue_raw"
        for row in call["args"][1]
    }

    assert hashes_1 == hashes_2
    assert hashes_1, "expected at least one hashed issue"
