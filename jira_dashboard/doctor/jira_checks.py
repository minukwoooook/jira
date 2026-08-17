"""spec §4.0의 A1~A12를 실호출로 확인한다. 전부 읽기 전용이다.

docs/api-verification.md가 이미 정리해둔 상태(공개 문서로 확인됨 / 사내 확인
필요 / 상충하는 정보 있음)를 실측으로 좁힌다. A3(인라인 changelog 상한)은
"필드가 있는지 보는" 검사가 아니라 실험이다 — total이 maxResults를 넘는 이슈를
찾아 2차 호출이 새 항목을 주는지까지 실제로 측정한다.
"""
from jira_dashboard.doctor.db_checks import CheckResult, format_report

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
    """spec §4.0의 A1~A12를 실호출로 확인한다. 전부 읽기 전용이다."""
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

    jql = f"project = {probe_project_key} ORDER BY updated ASC"
    page = client.search_issues(jql, 0, 100, True)
    out.append(CheckResult(
        "A1", "POST /search + startAt 페이징", "PASS" if page.issues else "FAIL",
        f"total={page.total}, returned={len(page.issues)}",
        "수집 자체가 불가능하다. 대체 엔드포인트 조사부터 다시 해야 한다",
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
        second = client.get_issue_changelog(overflow_issue.get("key"), received)
        new_ids = [h.get("id") for h in second.histories if h.get("id") not in seen_ids]
        found_new = bool(new_ids)
        out.append(CheckResult(
            "A3", "인라인 changelog 상한 실측 (2차 호출 실측 포함)",
            "PASS" if found_new else "FAIL",
            f"issue={overflow_issue.get('key')} total={total} maxResults={max_results} "
            f"received={received} second_call_new_entries={len(new_ids)}",
            "2차 호출이 새 항목을 하나도 안 주면, changelog 100건 초과 이슈는 "
            "이 클라이언트로는 나머지를 절대 가져올 수 없다는 뜻이다 (DC 10.3에는 "
            "전용 엔드포인트도 startAt도 없다 — docs/api-verification.md A3). "
            "sync_issues의 보충 호출 로직과 changelog_truncated 처리(spec §5.2)를 "
            "'유실을 감수한다'는 전제로 다시 설계해야 한다",
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

    user = (sample.get("fields") or {}).get("reporter") or {}
    ok = (not user) or ("accountId" not in user and ("key" in user or "name" in user))
    out.append(CheckResult(
        "A10", "user 표현이 key/name", "PASS" if ok else "WARN", str(sorted(user)),
        "accountId 형태면 assignee_user_key 컬럼의 의미가 달라진다",
    ))

    out.append(CheckResult(
        "A12", "인증 동작", "PASS", "all calls succeeded",
        "여기까지 왔다면 인증은 정상이다",
    ))
    return out
