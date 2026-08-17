"""spec §4.0의 A1~A12를 실호출로 확인한다. 전부 읽기 전용이다.

docs/api-verification.md가 이미 정리해둔 상태(공개 문서로 확인됨 / 사내 확인
필요 / 상충하는 정보 있음)를 실측으로 좁힌다. A3(인라인 changelog 상한)은
"필드가 있는지 보는" 검사가 아니라 실험이다 — total이 maxResults를 넘는 이슈를
찾아 2차 호출이 새 항목을 주는지까지 실제로 측정한다.

원칙: 관측하지 못한 것에 PASS를 주지 않는다. 증거가 없으면 WARN으로 그렇게
말한다(A1의 페이징 미검증, A10의 user 미관측, A3의 프로브 이슈 없음이 전부 이
패턴이다) — A4가 동일 타임스탬프에서 우연히 PASS하던 것을 막은 것과 같은 이유다.
"""
from jira_dashboard.doctor.db_checks import CheckResult, format_report
from jira_dashboard.jira.protocol import JiraAuthError

__all__ = ["run_jira_checks", "format_report"]

_VALID_CATEGORIES = {"new", "indeterminate", "done", "undefined"}


def _find_overflowing_issue(issues: list[dict]) -> dict | None:
    """changelog.total이 changelog.maxResults보다 큰 첫 이슈를 찾는다 — A3 실험의
    대상이다. 그런 이슈가 없으면 A3는 측정 불가능하다."""
    for issue in issues:
        cl = issue.get("changelog") or {}
        total, max_results = cl.get("total"), cl.get("maxResults")
        if total is not None and max_results is not None and total > max_results:
            return issue
    return None


def run_jira_checks(client, probe_project_key: str) -> list[CheckResult]:
    """spec §4.0의 A1~A12를 실호출로 확인한다. 전부 읽기 전용이다.

    자격증명이 거부되면(JiraAuthError) 나머지 A1~A11은 동일한 이유로 전부
    실패할 것이므로 실행을 생략하고 A12 FAIL 하나만 돌려준다 — 그래야 operator가
    11개의 동일한 실패 대신 진짜 원인(자격증명)을 바로 본다."""
    try:
        return _run_jira_checks(client, probe_project_key)
    except JiraAuthError as e:
        secret_ref = getattr(client, "secret_ref", None)
        hint = f" secret_ref 환경변수 {secret_ref}가" if secret_ref else " 이 인스턴스의 secret_ref 환경변수가"
        return [CheckResult(
            "A12", "인증 동작", "FAIL", str(e),
            f"자격증명이 거부됐다({e}).{hint} 유효한 PAT/Basic 토큰을 담고 있는지 "
            "확인할 것. A1~A11은 동일한 원인으로 전부 실패할 것이므로 실행을 "
            "생략했다 — 자격증명부터 고치고 재실행할 것",
        )]


def _run_jira_checks(client, probe_project_key: str) -> list[CheckResult]:
    out: list[CheckResult] = []

    fields = client.get_fields()
    has_schema = any("schema" in f for f in fields)
    out.append(CheckResult(
        "A8", "/field 가 schema 를 제공", "PASS" if has_schema else "FAIL",
        f"{len(fields)} fields, schema={has_schema}",
        "없으면 value_kind 판정 로직(spec §4.3)을 전면 재작성해야 한다",
    ))

    statuses = client.get_statuses()
    keys = {(s.get("statusCategory") or {}).get("key") for s in statuses}
    ok = bool(keys) and None not in keys and keys <= _VALID_CATEGORIES
    out.append(CheckResult(
        "A9", "statusCategory.key 제공", "PASS" if ok else "FAIL",
        str(sorted(k for k in keys if k)),
        "없으면 인스턴스 간 교차 분석(spec §3.3.1)이 성립하지 않는다 — 설계 재검토",
    ))

    projects = client.get_projects()
    ok = bool(projects) and all({"id", "key"} <= set(p) for p in projects)
    out.append(CheckResult(
        "A11", "/project 가 id/key 제공", "PASS" if ok else "FAIL",
        f"{len(projects)} projects", "화이트리스트를 구성할 수 없다",
    ))

    # --- A1: startAt 페이징이 "존재"하는지가 아니라 "실제로 전진"하는지를 본다.
    # startAt=0 호출이 행을 돌려주는 것만으로는 서버가 startAt을 무시하는지 알 수
    # 없다 — 두 번째 페이지를 받아 아이디 집합이 겹치지 않는지까지 확인해야 한다.
    jql = f"project = {probe_project_key} ORDER BY updated ASC"
    page = client.search_issues(jql, 0, 100, True)
    if not page.issues:
        out.append(CheckResult(
            "A1", "POST /search + startAt 페이징", "FAIL",
            f"total={page.total}, returned=0",
            "수집 자체가 불가능하다. 대체 엔드포인트 조사부터 다시 해야 한다",
        ))
    elif page.total <= len(page.issues):
        out.append(CheckResult(
            "A1", "POST /search + startAt 페이징", "WARN",
            f"total={page.total}, returned={len(page.issues)} — 프로젝트가 한 "
            "페이지에 다 들어가서 startAt 전진 자체는 미검증",
            "이슈 수가 한 페이지(현재 maxResults)를 넘는 프로젝트로 --project를 "
            "다시 지정해 재확인할 것 — startAt이 실제로 다음 페이지로 넘어가는지는 "
            "아직 확인되지 않았다",
        ))
    else:
        second = client.search_issues(jql, page.max_results, 100, True)
        ids1 = {i.get("id") for i in page.issues}
        ids2 = {i.get("id") for i in second.issues}
        disjoint = bool(ids2) and ids1.isdisjoint(ids2)
        out.append(CheckResult(
            "A1", "POST /search + startAt 페이징", "PASS" if disjoint else "FAIL",
            f"total={page.total} page1={len(page.issues)} page2={len(second.issues)} "
            f"disjoint={disjoint}",
            "startAt이 실제로 다음 페이지로 넘어가지 않는다 — 서버가 startAt을 "
            "무시하면 두 번째 페이지가 첫 페이지와 겹치거나 비어서, 수집이 이슈 "
            "대부분을 놓친 채 '완료'로 착각한다. 대체 페이징 파라미터/엔드포인트부터 "
            "재조사할 것",
        ))

    out.append(CheckResult(
        "A7", "서버가 maxResults 를 축소하는가",
        "PASS" if page.max_results == 100 else "WARN", str(page.max_results),
        "요청값이 아니라 응답값으로 페이징해야 한다 (이미 그렇게 구현되어 있다)",
    ))

    sample = page.issues[0] if page.issues else {}
    inline = "changelog" in sample
    out.append(CheckResult(
        "A2", "expand=changelog 인라인 포함", "PASS" if inline else "FAIL",
        f"present={inline}",
        "이슈별 개별 호출로 폴백해야 한다 — 요청 수가 약 100배가 된다",
    ))

    # --- A3: 인라인 changelog 상한을 실측한다 (조사만으로는 끝나지 않는 항목,
    # docs/api-verification.md A3). total > maxResults인 이슈를 찾아 2차 호출로
    # 새 항목이 실제로 오는지까지 확인해야 "판별 가능하다"는 가정이 검증된다.
    #
    # 검증 결과에 대한 판정: DC 10.3에 changelog 전용 엔드포인트도 startAt도 없다는
    # 조사 결과(docs/api-verification.md A3)로 볼 때 "2차 호출이 새 항목을 안 준다"는
    # 예상된 결과이고, 파이프라인은 이미 이를 처리하도록 설계돼 있다(sync_issues의
    # no-progress 가드 + changelog_truncated 카운터) — 그러므로 이건 FAIL이 아니라
    # WARN이다: 무언가 고장난 게 아니라, 알려진 한계를 실측으로 재확인한 것뿐이다.
    # 반대로 새 항목이 실제로 온다면 예상보다 나은 상황이라 PASS다. 프로브 프로젝트에
    # 초과 이슈가 아예 없으면 그 자체도 WARN — 이 경우 FAIL은 절대 맞지 않다.
    overflow_issue = _find_overflowing_issue(page.issues)
    if overflow_issue is None:
        out.append(CheckResult(
            "A3", "인라인 changelog 상한 실측 (2차 호출 실측 포함)", "WARN",
            f"프로젝트 {probe_project_key} 안에 changelog.total > maxResults인 "
            "이슈가 없다 — A3를 실측하지 못했다",
            "changelog가 100건을 넘는 이슈가 있는 프로젝트를 --project로 "
            "지정해 재실행할 것. 측정 없이는 sync_issues의 보충 호출 조건 "
            "(spec §5.2)이 여전히 미검증 상태다",
        ))
    else:
        cl = overflow_issue.get("changelog") or {}
        total, max_results = cl.get("total"), cl.get("maxResults")
        first_histories = cl.get("histories") or []
        received = len(first_histories)
        seen_ids = {h.get("id") for h in first_histories}
        second_cl = client.get_issue_changelog(overflow_issue.get("key"), received)
        new_ids = [h.get("id") for h in second_cl.histories if h.get("id") not in seen_ids]
        found_new = bool(new_ids)
        if found_new:
            verdict, impact = "PASS", (
                "예상보다 낫다 — 2차 호출로 changelog 전체를 받을 수 있다는 뜻이다. "
                "다만 DC 10.3에 이 동작을 보장하는 공식 근거는 없으므로, "
                "sync_issues가 이 결과에 의존하기 전에 다른 프로젝트/이슈로도 "
                "재확인할 것"
            )
        else:
            verdict, impact = "WARN", (
                "DC 10.3이 changelog 페이징을 지원하지 않는다는 조사 결과와 "
                "일치한다(docs/api-verification.md A3) — 알려진 한계이고 파이프라인은 "
                "이미 이를 처리하도록 설계돼 있다(sync_issues의 no-progress 가드 + "
                "changelog_truncated 카운터). sync 실행 결과의 changelog_truncated "
                "값을 관찰해 영향받는 이슈 수를 확인할 것"
            )
        out.append(CheckResult(
            "A3", "인라인 changelog 상한 실측 (2차 호출 실측 포함)", verdict,
            f"issue={overflow_issue.get('key')} total={total} maxResults={max_results} "
            f"received={received} second_call_new_entries={len(new_ids)}",
            impact,
        ))

    has_field_id = None
    for issue in page.issues:
        for h in (issue.get("changelog") or {}).get("histories") or []:
            for item in h.get("items") or []:
                has_field_id = "fieldId" in item
                break
            if has_field_id is not None:
                break
        if has_field_id is not None:
            break
    out.append(CheckResult(
        "A5", "changelog item 에 fieldId", "PASS" if has_field_id else "WARN",
        f"present={has_field_id}",
        "없으면 이름으로만 매칭하고, 동명 커스텀 필드는 field_pk NULL이 된다 (spec §4.2)",
    ))

    ascending = None
    for issue in page.issues:
        hist = (issue.get("changelog") or {}).get("histories") or []
        stamps = [h["created"] for h in hist]
        # 타임스탬프가 전부 같으면 오름차순이든 내림차순이든 우연히 "정렬돼 보이므로"
        # 판정에 쓸 수 없다 — 서로 다른 값이 최소 2개 있는 이슈까지 넘어간다.
        if len(set(stamps)) >= 2:
            ascending = stamps == sorted(stamps)
            break
    out.append(CheckResult(
        "A4", "changelog 오름차순", "PASS" if ascending else "FAIL",
        f"ascending={ascending}",
        "구간 테이블이 통째로 뒤집힌다. sync_issues 보충 호출과 "
        "derive_history 정렬을 함께 수정할 것 (spec §5.2, §5.3)",
    ))

    custom = [k for k in (sample.get("fields") or {}) if k.startswith("customfield_")]
    out.append(CheckResult(
        "A6", "fields=*all 이 커스텀 필드 반환", "PASS" if custom else "FAIL",
        f"{len(custom)} custom fields",
        "필요한 필드를 명시 나열해야 한다 — 새 필드 자동 수집이 불가능해진다",
    ))

    # --- A10: reporter가 비어 있는 걸 "PASS"로 해석하지 않는다. 첫 이슈에 없으면
    # 응답받은 이슈들을 계속 훑어서 실제로 user 객체가 있는 것을 찾는다 — 하나도
    # 없으면 관측한 게 없다는 뜻이므로 WARN이다 (A4의 "동일 타임스탬프는 결론을
    # 못 낸다"는 원칙과 같은 이유).
    observed_user = None
    for issue in page.issues:
        candidate = (issue.get("fields") or {}).get("reporter") or {}
        if candidate:
            observed_user = candidate
            break
    if observed_user is None:
        out.append(CheckResult(
            "A10", "user 표현이 key/name", "WARN",
            f"{len(page.issues)}개 이슈 중 reporter가 채워진 것이 없어 관측하지 못했다",
            "reporter가 있는 이슈로 --project/--instance를 다시 확인할 것 — "
            "지금은 user 표현(key/name vs accountId)이 실측되지 않았다",
        ))
    else:
        ok = "accountId" not in observed_user and (
            "key" in observed_user or "name" in observed_user
        )
        out.append(CheckResult(
            "A10", "user 표현이 key/name", "PASS" if ok else "WARN",
            str(sorted(observed_user)),
            "accountId 형태면 assignee_user_key 컬럼의 의미가 달라진다",
        ))

    out.append(CheckResult(
        "A12", "인증 동작", "PASS", "all calls succeeded",
        "여기까지 왔다면 인증은 정상이다",
    ))
    return out
