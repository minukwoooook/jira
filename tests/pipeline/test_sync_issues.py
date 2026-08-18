import logging
from datetime import datetime, timedelta, timezone

import pytest

from jira_dashboard.db.repository import issue as issue_repo
from jira_dashboard.jira.protocol import SearchPage
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

def _sync_twice_with_matching_hashes(monkeypatch, fake_jira):
    """1회차를 돌려 해시/키를 얻고, 1회차와 똑같은 해시를 돌려주는 2회차를 실행한다.

    Item 3: 기본 `rec` 픽스처는 load_existing이 항상 {}를 돌려줘 아무것도 스킵되지
    않는다 — 그 픽스처로는 "스킵된 이슈도 touch_synced_at을 받는다"를 검증할 수
    없다 (touch_synced_at([])이 호출돼도 통과해버리는 공허한 테스트가 된다).
    실제로 스킵이 일어나는 2회차를 구성해야 의미가 생긴다.
    """
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
    return first, second, result


def test_unchanged_issue_only_touches_synced_at(monkeypatch, fake_jira):
    """1회차 해시를 그대로 돌려주면 2회차는 전부 스킵되어야 한다."""
    first, second, result = _sync_twice_with_matching_hashes(monkeypatch, fake_jira)

    assert result.upserted == 0
    assert result.skipped > 0
    assert second.count("upsert_issues") == 0
    assert second.count("touch_synced_at") > 0


def test_skipped_issues_still_get_synced_at_touched(monkeypatch, fake_jira):
    """마지막으로 확인한 시각이 정확해야 삭제 감지가 오래된 행을 구분한다.

    실제로 스킵된 issue_id가 touch_synced_at에 전달됐는지까지 확인한다 — 빈 리스트로
    불려도 통과하는 `count() >= 1` 만으로는 부족하다.
    """
    first, second, result = _sync_twice_with_matching_hashes(monkeypatch, fake_jira)

    touched = [issue_id for call in second.args_of("touch_synced_at")
               for issue_id in call["args"][0]]
    assert touched, "스킵 경로에서도 touch_synced_at에 실제 issue_id가 전달돼야 한다"
    assert result.skipped == len(touched)


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


def test_iter_pages_stops_on_zero_progress(fake_jira):
    """Item 8: max_results=0인데 issues가 비어있지 않은 응답이 오면 start_at이 절대
    전진하지 못해 같은 페이지를 영원히 재요청한다 — C2와 같은 무진행 루프 계열이다."""
    real_page = fake_jira.search_issues(
        "project = PROJ ORDER BY updated ASC", 0, 100, True
    )
    stuck_page = SearchPage(start_at=0, max_results=0, total=real_page.total,
                            issues=real_page.issues)

    class _ZeroProgressClient:
        def search_issues(self, jql, start_at, max_results, expand_changelog):
            return stuck_page

    pages = list(mod.iter_search_pages(_ZeroProgressClient(), "whatever", 100))
    assert len(pages) == 1


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


class _NullCursor:
    """실제 SQL을 실행하지 않는 커서. history_repo.upsert_changelog의 진짜 해석
    로직(3단계 field_pk 매칭)은 그대로 돌리되 conn.cursor()가 오라클을 건드리지
    않게 한다."""

    def executemany(self, *a, **k):
        pass

    def execute(self, *a, **k):
        pass


def test_unresolved_changelog_field_names_are_logged_once_as_a_distinct_set(
    monkeypatch, fake_jira, caplog,
):
    """Item 6: field 문자열이 fieldId/field_id/카탈로그 이름 어디에도 안 걸리면
    field_pk가 조용히 NULL이 된다 (§5.3 이력 유실). 개별 행마다 찍으면 안 되고,
    실행당 한 번, 서로 다른 이름의 집합으로만 찍혀야 한다.

    history_repo.upsert_changelog는 일부러 stub하지 않는다 — 진짜 3단계 해석기를
    돌려야 이 회귀를 잡을 수 있다. 대신 conn.cursor()만 no-op으로 바꿔 오라클
    호출 없이 실행한다. FIELD_PKS/field_names 테스트 더미에는 "status"가 없으므로
    픽스처의 status 변경 이력이 전부 미해결로 잡힌다.
    """
    r = Recorder()
    r.returns["load_existing"] = lambda *a, **k: {}
    r.returns["next_issue_ids"] = lambda conn, n: list(range(9000, 9000 + n))
    r.patch(monkeypatch, mod.issue_repo,
            "load_existing", "next_issue_ids", "upsert_issues", "touch_synced_at",
            "upsert_raw", "replace_field_values")
    monkeypatch.setattr(mod, "field_pk_by_field_id", lambda conn, i: FIELD_PKS)
    monkeypatch.setattr(mod, "field_pk_by_field_name", lambda conn, i: {})
    monkeypatch.setattr(CONN, "commit", lambda: None, raising=False)
    monkeypatch.setattr(CONN, "cursor", lambda: _NullCursor(), raising=False)

    with caplog.at_level(logging.WARNING, logger=mod.log.name):
        _run(fake_jira)

    unresolved_logs = [r for r in caplog.records if "unresolved" in r.getMessage().lower()]
    assert len(unresolved_logs) == 1, "한 번만, 실행 전체에 대해 찍혀야 한다"
    assert "status" in unresolved_logs[0].getMessage()


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
    """실패한 이슈가 채번을 소진하면 뒤 이슈들이 id를 못 받는다.

    Item 2: 개수만 비교하면 (고유 id 7개, 7 upserted) '파싱 전에 채번'하는
    버그와 구분이 안 된다 — 그 버그는 8개를 채번해 실패한 1개의 id를 그냥
    버리므로 여전히 서로 다른 7개 id가 7건에 쓰인다. 실제로 구분하려면
    (a) next_issue_ids에 요청한 개수가 살아남은 이슈 수와 정확히 같은지,
    (b) 받은 id가 시작점부터 빈틈없이 이어지는지를 봐야 한다.
    """
    page = fake_jira.search_issues("project = PROJ ORDER BY updated ASC", 0, 100, True)
    victim = str(page.issues[0]["id"])
    fake_jira._issues[victim]["fields"]["created"] = "not-a-timestamp"

    result = _run(fake_jira)
    written = [row["issue_id"] for call in rec.args_of("upsert_issues")
               for row in call["args"][0]]
    assert len(written) == len(set(written))
    assert len(written) == result.upserted

    requested = rec.args_of("next_issue_ids")[0]["args"][0]
    assert requested == result.upserted, (
        "next_issue_ids를 파싱 실패 개수까지 포함해 요청하면 id 하나가 좌초된다"
    )
    assert written == list(range(9000, 9000 + len(written))), (
        "좌초된 id가 있으면 9000부터 이어지는 구간에 구멍이 생긴다"
    )


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
    rows_1 = [
        row for call in r1.args_of("upsert_raw") if call["args"][0] == "test_issue_raw"
        for row in call["args"][1]
    ]
    hashes_1 = {row["issue_id"]: row["payload_hash"] for row in rows_1}

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

    # Item 1 (critical): a same-second re-run of gzip.compress() over identical bytes
    # can itself be reproducible, so equality of hashes across two runs does NOT prove
    # the digest was taken over canonical JSON rather than gzip output. Pin it directly:
    # the stored digest must differ from the digest of the stored (compressed) payload.
    # This is the assertion that cannot pass if someone reverts to hashing gzip output.
    for row in rows_1:
        assert row["payload_hash"] != issue_repo.sha256_hex(row["payload"])


# --- C1: dry-run은 절대 커밋하지 않는다 -------------------------------------

def _commit_recording_rec(monkeypatch):
    """rec 픽스처와 같은 스텁 + conn.commit 호출을 기록한다."""
    r = Recorder()
    r.returns["load_existing"] = lambda *a, **k: {}
    r.returns["next_issue_ids"] = lambda conn, n: list(range(9000, 9000 + n))
    r.patch(monkeypatch, mod.issue_repo,
            "load_existing", "next_issue_ids", "upsert_issues", "touch_synced_at",
            "upsert_raw", "replace_field_values")
    r.patch(monkeypatch, mod.history_repo, "upsert_changelog")
    monkeypatch.setattr(mod, "field_pk_by_field_id", lambda conn, i: FIELD_PKS)
    monkeypatch.setattr(mod, "field_pk_by_field_name", lambda conn, i: {})
    commits = []
    monkeypatch.setattr(CONN, "commit", lambda: commits.append(1), raising=False)
    return r, commits


def test_dry_run_never_commits(monkeypatch, fake_jira):
    """C1: README는 dry-run이 프로덕션에 안전하다고 말하는데, sync_issues가 페이지마다
    커밋하고 db_conn이 종료 시 한 번 더 커밋해서 runner의 rollback()은 이미 커밋된
    데이터 위의 no-op이었다. runner 테스트는 sync_issues를 스텁하므로 이걸 잡을 수
    없다 — 진짜 sync_issues를 돌려야 한다."""
    _, commits = _commit_recording_rec(monkeypatch)
    result = mod.sync_issues(CONN, fake_jira, 1, 7, "PROJ", None, dry_run=True)

    assert commits == [], "dry-run에서 conn.commit()이 한 번이라도 불리면 안 된다"
    assert result.upserted > 0, "쓰기 자체는 실행돼야 한다 (호출자가 롤백한다)"


def test_normal_run_still_commits_each_page(monkeypatch, fake_jira):
    """반대 방향도 고정한다 — dry_run 플래그가 항상 참이 되어버리면 증분 수집이
    한 트랜잭션으로 부풀어 undo를 터뜨린다."""
    _, commits = _commit_recording_rec(monkeypatch)
    mod.sync_issues(CONN, fake_jira, 1, 7, "PROJ", None)
    assert commits, "정상 실행은 페이지마다 커밋해야 한다"


def test_page_size_above_the_in_list_limit_is_rejected():
    """load_existing이 페이지 전체를 IN 리스트로 조회하므로 1000을 넘으면 ORA-01795다."""
    with pytest.raises(ValueError, match="1000"):
        mod.sync_issues(CONN, None, 1, 7, "PROJ", None, page_size=1001)


# --- C4: 전체 재수집은 해시 비교를 우회한다 ----------------------------------

def test_full_resync_bypasses_the_payload_hash(monkeypatch, fake_jira):
    """R31: derive_history가 터진 다음 실행은 해시가 같아 전부 스킵하고 이력을
    만들지 않은 채 SUCCESS를 보고했다. full_resync_requested='Y'는 워터마크만
    비웠으므로 전체 재수집으로도 복구되지 않았다 — 해시 자체를 우회해야 한다."""
    first, second, skipped_result = _sync_twice_with_matching_hashes(
        monkeypatch, fake_jira
    )
    assert skipped_result.upserted == 0        # 해시가 같으면 평소엔 전부 스킵

    third = Recorder()
    third.returns["load_existing"] = second.returns["load_existing"]
    third.returns["next_issue_ids"] = lambda conn, n: list(range(7000, 7000 + n))
    third.patch(monkeypatch, mod.issue_repo,
                "load_existing", "next_issue_ids", "upsert_issues",
                "touch_synced_at", "upsert_raw", "replace_field_values")
    third.patch(monkeypatch, mod.history_repo, "upsert_changelog")
    result = mod.sync_issues(CONN, fake_jira, 1, 7, "PROJ", None, full_resync=True)

    assert result.skipped == 0
    assert result.upserted == skipped_result.skipped > 0
    assert result.changed_issue_ids, "재수집이 이력 파생 대상을 다시 넘겨줘야 한다"
