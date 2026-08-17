# Jira Data Center 10.3 API 계약 검증

이 문서는 `docs/design.md` §4.0의 A1~A12 가정을 **공개 문서**만으로 검증한 기록이다.
사내 인스턴스(Jira Data Center 10.3)에 접속하지 않고 작성했으므로, 여기서 "확인됨"이라고
적은 항목도 실제로는 `cli doctor --jira`(11.5절)가 사내에서 재확인해야 한다. 다만
"확인됨"과 "확인 필요"를 구분해두면 `doctor --jira`가 어디를 중점적으로 찍어야 하는지
알 수 있다는 것이 이 문서의 목적이다.

**애매한 경우는 전부 "사내 확인 필요"로 분류했다.** 잘못된 "확인됨"이 조용한 데이터 오류로
이어지는 게 가장 위험하기 때문이다.

## 검증에 사용한 1차 자료

- **Jira Data Center 10.3 REST 레퍼런스**: `https://developer.atlassian.com/server/jira/platform/rest/v10003/`
  이 URL의 임베디드 OpenAPI 스키마(`info.version`)가 정확히 **"10.3.24"**로 표시됨을 직접
  확인했다. 브리프가 우려한 "버전 셀렉터가 다른 버전으로 랜딩할 위험"은 발생하지 않았다 —
  `v10003` 세그먼트가 실제로 10.3.x 빌드와 매핑된다 (참고로 `v10001`, `v10002`, `v10004`는
  스키마 본문이 `swagger-merged-template.yaml`이라는 플레이스홀더만 담고 있어 실제 스펙이
  아니었다. 오직 `v10003`만 실제 10.3 스펙이 채워져 있었다).
- **Jira 10.0 breaking change 공식 목록**: `https://developer.atlassian.com/server/jira/platform/jsw-10-jsm-6-all-breaking-changes/`
  (요약본인 `preparing-for-jsw-10-jsm-6/` 페이지와 내용이 동일함을 대조 확인)
- 그 외 `confluence.atlassian.com`(공식 관리자 문서), `support.atlassian.com`(공식 KB,
  "Data Center Only" 표기 확인) — 커뮤니티 포럼(`community.atlassian.com`,
  `community.developer.atlassian.com`)은 참고만 하고 단독 근거로는 쓰지 않았다.

## 이미 확인된 것 (사전 연구 — 재검증하지 않고 그대로 채택)

| 사실 | 근거 |
|---|---|
| `/rest/api/2/search` 제거는 Jira **Cloud** 얘기다. DC는 무관하다 | Cloud가 `/rest/api/3/search/jql`로 이전한 것과 DC 10.3 REST 레퍼런스에 `/api/2/search`가 여전히 존재하는 것(아래 A1)이 서로 다른 트랙임을 확인 |
| "DC 10.4의 새 search API" 공지는 REST가 아니라 앱 벤더용 Java/Lucene 인덱스 API다 | 사전 연구 결과 채택. 우리와 무관 |
| DC용 공식 OpenAPI/Swagger 스펙은 없고 WADL만 있다 → 실호출 검증이 유일한 확실한 방법 | 사전 연구 결과 채택. (단, 이번 조사에서 `developer.atlassian.com`이 각 버전 페이지 안에 **비공식적으로 생성된** OpenAPI 3.0 JSON(`window.__DATA__.schema`)을 내부적으로 쓰고 있다는 걸 발견했다 — 이건 "공식 배포 스펙 파일"이 아니라 문서 사이트를 렌더링하기 위한 내부 산출물이라 스펙 파일로 받아 코드를 생성하는 용도로 쓸 수 없다는 원 결론은 그대로 유효하다) |
| Jira 11에 search 관련 deprecation 예고 있음 — 지금은 10.3이라 무관, 업그레이드 시 재검토 | 사전 연구 결과 채택 |

**Jira 10.0 "이전 deprecated 엔드포인트 제거"의 구체 목록 — 이번 조사에서 발견했다.**
사전 연구에서는 "공개 changelog에서 확인되지 않음"이라 되어 있었지만, 아래 URL에서 표 형태의
전체 목록을 찾았다:

`https://developer.atlassian.com/server/jira/platform/jsw-10-jsm-6-all-breaking-changes/`

목록에 있는 REST 제거/변경 사항 전체:

- `GET /rest/api/2/auditing/record`, `POST /rest/api/2/auditing/record` 제거 → `rest/auditing/1.0/...`로 대체
- `GET /rest/api/2/group` 제거 → `GET /rest/api/2/group/member` 사용
- `DELETE /rest/api/2/version/{id}` 제거 → `POST /rest/api/2/version/{id}/removeAndSwap` 사용
- `rest/api/2/serverInfo`에서 `doHealthCheck` 파라미터/`healthChecks` 응답 필드 제거
- `rest/api/2/user/search`가 최대 100건까지만 반환하도록 변경 (제거는 아니고 응답 건수 제한)
- (`/api/1.0/user/{username}/avatar/{avatarid}` 같은 비공개 v1 엔드포인트, `/servicedeskapi/queues/*` 등 — 우리와 무관한 서비스데스크/내부 엔드포인트)

**우리가 쓰는 6개(`/search`, `/issue/{key}`, `/field`, `/project`, `/status`, `/myself`)는 이
목록에 전혀 등장하지 않는다.** 즉 Jira 10.0에서 우리가 쓰는 엔드포인트가 제거되거나
시그니처가 바뀐 사실은 없다 → **공개 문서로 확인됨**.

---

## A1. `POST /rest/api/2/search` 존재 + JQL/`startAt` 페이징

- **가정:** `POST /rest/api/2/search`가 존재하고 JQL + `startAt` 페이징을 지원
- **조사 결과:** 공개 문서로 확인됨
- **근거:** `https://developer.atlassian.com/server/jira/platform/rest/v10003/` (info.version
  10.3.24). `GET /api/2/search`와 `POST /api/2/search` 둘 다 존재. GET의 쿼리 파라미터에
  `jql`, `startAt`("the index of the first issue to return (0-based)"), `maxResults`가 있고,
  POST의 요청 바디 스키마 `SearchRequestBean`에도 `jql`, `startAt`, `maxResults`, `fields`,
  `expand`, `validateQuery` 필드가 그대로 있다.
- **틀렸을 때 할 일:** 수집 자체가 불가하므로 대체 엔드포인트 조사부터 다시 시작한다.

## A2. `expand=changelog`가 search 응답에 이력을 인라인 포함

- **가정:** `expand=changelog`가 search 응답에 이력을 인라인 포함
- **조사 결과:** 공개 문서로 확인됨
- **근거:** 동일 v10003 레퍼런스, `GET /api/2/search` operation 설명 원문: *"Expanding Issues in
  the Search Result: It is possible to expand the issues returned by directly specifying the
  expansion on the expand parameter... For instance, to expand the changelog for all the issues
  on the search result, it is necessary to specify changelog as one of the values to expand."*
  또한 `SearchRequestBean.expand`의 example 배열에 `"changelog"`가 포함되어 있다.
- **틀렸을 때 할 일:** 이슈별 개별 호출로 폴백해야 한다 → 요청 수 약 100배 증가.

## A3. 인라인 changelog 상한 100 + `total`/`maxResults`로 판별 가능 (+ `startAt` 지원 여부)

- **가정:** 인라인 changelog 상한이 100이고 `changelog.total`/`maxResults`로 판별 가능
- **조사 결과:** 사내 확인 필요
- **근거 (확인된 부분과 안 된 부분을 나눠 기록):**
  - **확인됨:** `ChangelogBean` 스키마(v10003)에 `histories`, `startAt`, `maxResults`, `total`
    필드가 존재한다 — 즉 changelog 응답 자체가 페이지네이션 메타데이터 구조를 갖고 있다는 것은
    스펙으로 확인된다.
  - **확인됨(브리프가 특별히 요청한 지점):** `GET /rest/api/2/issue/{issueIdOrKey}`의 쿼리
    파라미터는 `expand`, `fields`, `updateHistory`, `properties` 4개뿐이고 **`startAt`은
    파라미터 목록에 없다.** 즉 이 엔드포인트에는 changelog를 위한 `startAt`을 쿼리스트링으로
    넘길 방법이 공식 스펙에 없다.
  - **확인됨:** v10003 스펙의 전체 285개 경로(paths)를 모두 나열해봤을 때 `changelog`나
    `history`가 들어간 전용 엔드포인트(예: Cloud v3의 `/issue/{key}/changelog`에 해당하는
    것)가 **DC 10.3에는 존재하지 않는다.** 관련해서 `https://jira.atlassian.com/browse/JRASERVER-71168`
    ("Extend Jira REST API for Changelog History")가 아직 "Gathering Interest" 상태로 열려
    있어, Server/DC 쪽에 changelog 필터링/전용 페이징 엔드포인트가 없다는 정황과 일치한다.
  - **확인 안 됨:** "상한이 정확히 100건"이라는 숫자 자체는 DC 공식 문서(`developer.atlassian.com`,
    `support.atlassian.com`) 어디에도 없다. 커뮤니티 포럼에 "100건 제한"이라는 보고가 다수
    있지만(`https://community.atlassian.com/forums/Jira-questions/Rest-API-limiting-changelog-history-results-to-100-even-if/qaq-p/1466525`),
    이들 대부분이 Jira Cloud 또는 버전 불명 사례라 DC 10.3에 그대로 적용된다고 단정할 근거가
    부족하다.
  - **결론:** "`startAt`이 이 엔드포인트에 없다"는 확인됐지만, "몇 건에서 잘리는지"와 "잘렸을 때
    나머지를 어떻게 가져오는지(전용 엔드포인트가 없으므로)"는 사내에서 changelog 100건 초과
    이슈로 직접 실험해봐야 한다.
- **틀렸을 때 할 일:** 보충 호출 조건(임계값 100)과 보충 호출 방식(전용 엔드포인트가 없다면
  `GET /issue/{key}?expand=changelog` 자체를 반복 호출해 `total`이 줄어드는지 볼 수밖에
  없는데, 이 경우 서버가 매번 "가장 최근 100건"만 주는지 "항상 처음 100건"만 주는지도 함께
  확인해야 한다) 둘 다 재설계한다.

## A4. changelog 정렬 방향 (오름차순 = 오래된 것 먼저)

- **가정:** changelog가 오름차순(오래된 것 먼저)
- **조사 결과:** 상충하는 정보 있음
- **근거:**
  - DC 10.3 공식 REST 레퍼런스(v10003)는 정렬 방향을 **명시하지 않는다.** `ChangeHistoryBean`
    스키마에는 순서 보장에 대한 서술이 없다.
  - Atlassian Developer Community 스레드
    (`https://community.developer.atlassian.com/t/changelogs-sorting-limitation-rest-api/8620`)에
    따르면 **Jira Cloud**는 `expand=changelog`(search/issue 둘 다)의 기본 정렬을 오름차순에서
    **내림차순(최신 먼저)으로 변경**했다고 나온다. 반면 Cloud의 전용 changelog 엔드포인트는
    오름차순을 유지한다고 한다. 이건 Cloud 얘기라 DC에 그대로 적용된다고 볼 수 없지만, "한 회사가
    같은 이름의 필드를 버전에 따라 다른 순서로 바꾼 전례가 있다"는 점에서 DC도 안심할 수 없다.
  - Atlassian 공식 지원 담당자(Andy Heinzer)가 커뮤니티 포럼
    (`https://community.atlassian.com/forums/Jira-questions/Jira-ChangeLog-revisions-order-issues/qaq-p/816406`)에서
    한 답변: *"If you need to sort these changes based on when they happened, I'd recommend
    looking at the created parameter within that response to sort these changes."* — 이는 사실상
    "정렬 순서를 신뢰하지 말고 `created` 필드로 직접 정렬하라"는 공식 권고로 읽힌다.
  - 두 커뮤니티 자료가 서로 다른 제품(Cloud vs 버전 불명)을 얘기하고 있고, DC 10.3 자체에 대한
    1차 공식 진술은 찾지 못했다. **A4는 치명적 가정(§4.0에서 명시)이므로 특히 신중하게
    "확인 필요"로 남긴다.**
- **틀렸을 때 할 일:** `sync_issues`의 보충 호출 `startAt` 기준과 `derive_history` 정렬을
  뒤집는다. 추가로, 위 Andy Heinzer의 권고를 받아들여 **API가 주는 순서에 의존하지 않고
  `derive_history`가 매번 `created` 타임스탬프로 명시적으로 재정렬**하도록 만드는 편이
  근본적으로 더 안전해 보인다 (이건 설계 변경 제안이지, 이 문서의 판정 자체는 아니다).

## A5. changelog item에 `fieldId` 포함 여부

- **가정:** changelog item에 `fieldId`가 포함됨
- **조사 결과:** 상충하는 정보 있음
- **근거:**
  - `docs/design.md` §4.2는 "8.4 이후 버전은 `fieldId`를 함께 준다"고 서술한다(이 설계
    문서 자체가 검증 대상 가정이므로 근거로 재사용하지 않음).
  - DC 10.3 공식 레퍼런스(v10003)의 `ChangeItemBean` 스키마 — search/issue의 changelog
    item에 실제로 대응하는 타입 — 는 `field`, `fieldtype`, `from`, `fromString`, `to`,
    `toString` 6개 속성만 정의하고 있고 **`fieldId`가 없다.**
  - 다만 같은 스키마 문서 안에서 `fieldId`라는 이름의 속성은 `FieldValueBean`,
    `SortByBean`, `OrderByOption` 등 **다른** Bean에는 등장한다. 즉 스키마 생성기가
    `fieldId`라는 필드명 자체를 누락시키는 버그가 있는 건 아니고, `ChangeItemBean`에만
    없는 것으로 보인다.
  - 다만 이 자동 생성 스키마가 완전히 신뢰할 만한지는 별개 문제다 — 예를 들어
    `GET /api/2/field`의 응답 스키마는 실제로는 배열인데도 문서상 단일 `FieldBean`
    객체로 잘못 기술돼 있는 등, 이 문서 자체에 생성 오류 사례가 존재한다는 것도 이번
    조사에서 확인했다. 그래서 "`ChangeItemBean`에 `fieldId`가 없다"는 스키마 상의
    부재가 실제 서버 응답에도 없다는 것을 100% 보장하진 않는다.
  - 종합하면: 설계 문서의 사전 가정과 공식 스키마가 정면으로 다른 얘기를 하고, 공식
    스키마 자체의 신뢰도에도 의문 부호가 있다 → 상충.
- **틀렸을 때 할 일:** (실제로 `fieldId`가 없다면) 이름 매칭만 사용해야 하고, 동명 커스텀
  필드는 전부 `field_pk` NULL로 남긴다 (설계 §4.2의 2번 규칙을 기본 경로로 삼는다).

## A6. `fields=*all`이 모든 커스텀 필드 값을 반환

- **가정:** `fields=*all`이 모든 커스텀 필드 값을 반환
- **조사 결과:** 공개 문서로 확인됨
- **근거:** v10003 `GET /api/2/search` operation 설명 원문: *"By default, only navigable
  (\*navigable) fields are returned in this search resource. Note: the default is different in
  the get-issue resource -- the default there all fields (\*all). \*all - include all fields;
  \*navigable - include just navigable fields..."* — search의 기본값은 `*navigable`(전체가
  아님)이고, 전체 필드를 받으려면 명시적으로 `fields=*all`을 지정해야 한다는 것이 스펙 문서에
  그대로 적혀 있다.
- **틀렸을 때 할 일:** 필요한 필드를 명시 나열해야 함.

## A7. 서버가 `maxResults`를 축소할 수 있고 응답에 실제값이 담김

- **가정:** 서버가 `maxResults`를 축소할 수 있고 응답에 실제값이 담김
- **조사 결과:** 공개 문서로 확인됨
- **근거:**
  - 응답에 실제값 포함: v10003 `SearchResultsBean` 스키마에 `maxResults`, `startAt`, `total`
    필드가 명시돼 있다.
  - 서버가 축소할 수 있음: 공식 KB(Data Center Only로 명시됨)
    `https://support.atlassian.com/jira/kb/should-i-change-the-jirasearchviewsdefaultmax-parameter-to-unlimited-to-get-all-results-from-a-search-each-time/`
    원문: *"If a 'maxResults' parameter is given it will return that many issues as long as it
    is lower than the value of property jira.search.views.default.max."* (기본값 1000). 즉
    클라이언트가 요청한 `maxResults`가 이 인스턴스 설정값보다 크면 서버가 조용히 줄여서
    반환한다 — 그리고 그 실제값은 위 `SearchResultsBean.maxResults`로 확인 가능하다.
- **틀렸을 때 할 일:** 페이징 누락 방지를 위해 요청값이 아니라 응답의 `maxResults`를 기준으로
  다음 `startAt`을 계산하도록 한다 (설계상 이미 그렇게 돼 있다면 유지).

## A8. `/rest/api/2/field`가 `schema.type`/`schema.custom` 제공

- **가정:** `/rest/api/2/field`가 `schema.type` / `schema.custom`을 제공
- **조사 결과:** 공개 문서로 확인됨
- **근거:** v10003 `FieldBean.schema` → `$ref: JsonTypeBean`. `JsonTypeBean` 속성:
  `type`(예: `"string"`), `custom`(예: `"null"`, 커스텀 필드 타입 키), `customId`, `items`,
  `system`. `type`과 `custom` 둘 다 존재를 확인.
  (참고: 이 엔드포인트의 응답이 배열이라는 사실 자체는 문서 스키마상 단일 객체로 잘못
  기술돼 있었지만 — 이건 문서 생성 버그로 보이며 필드 존재 여부와는 무관하다.)
- **틀렸을 때 할 일:** `value_kind` 판정 로직 전면 변경.

## A9. `/rest/api/2/status`가 `statusCategory.key` 제공

- **가정:** `/rest/api/2/status`가 `statusCategory.key`(`new`/`indeterminate`/`done`)를 제공
- **조사 결과:** 공개 문서로 확인됨
- **근거:** v10003 `GET /api/2/status` 응답 스키마 `StatusJsonBean.statusCategory` →
  `$ref: StatusCategoryJsonBean`. 이 스키마의 속성: `id`, `key`(example `"new"`), `name`
  (example `"To Do"`), `colorName`, `self`. `key` 필드가 예시값 `"new"`와 함께 명시적으로
  존재한다. 이 설계의 핵심 전제(인스턴스 간 교차 분석)를 뒷받침하는 가장 중요한 항목이라
  가장 신중하게 봤지만, 공식 스키마에 `key` 필드가 분명하게 있고 예시값도 설계가 기대하는
  값(`"new"`)과 일치한다.
  **다만 이것도 스키마 문서일 뿐 실호출 응답은 아니므로, `doctor --jira`가 사내에서
  `new`/`indeterminate`/`done` 3개 값이 실제로 나오는지, 로컬라이즈된 `name`이 아니라 정말
  안정적인 영문 `key`인지 반드시 재확인해야 한다.**
- **틀렸을 때 할 일:** 인스턴스 간 교차 분석이라는 설계 전제가 무너지므로 최우선으로
  재설계해야 한다.

## A10. 사용자가 `key`/`name`/`displayName`으로 표현됨 (Cloud의 `accountId` 아님)

- **가정:** 사용자가 `key` / `name` / `displayName`으로 표현됨
- **조사 결과:** 공개 문서로 확인됨
- **근거:** v10003 `UserJsonBean`(changelog author 등에서 쓰임) 속성: `key`(example
  `"fred"`), `name`(example `"Fred"`), `displayName`(example `"Fred F. User"`),
  `emailAddress`, `active`, `avatarUrls`, `timeZone`, `self`. `UserBean`(assignee/reporter
  등)도 `key`, `displayName`을 포함한다. 두 스키마 어디에도 Cloud 특유의 `accountId`
  필드는 없다.
- **틀렸을 때 할 일:** 컬럼 의미 변경 (해당 없음 — 확인됨).

## A11. `/rest/api/2/project`가 `id`/`key`/`name` 제공

- **가정:** `/rest/api/2/project`가 `id`/`key`/`name`을 제공
- **조사 결과:** 공개 문서로 확인됨
- **근거:** v10003 `ProjectBean` 속성: `id`(example `"10000"`), `key`(example `"EX"`),
  `name`(example `"Example"`), 그 외 `description`, `avatarUrls`, `archived`, `self`.
- **틀렸을 때 할 일:** 해당 없음 (확인됨).

## A12. PAT Bearer 인증이 동작

- **가정:** PAT Bearer 인증이 동작
- **조사 결과:** 공개 문서로 확인됨
- **근거:** 공식 관리자 문서
  `https://confluence.atlassian.com/enterprise/using-personal-access-tokens-1026032365.html`
  원문: *"To use a personal access token for authentication, you have to pass it as a bearer
  token in the Authorization header of a REST API call."* (예시:
  `curl -H "Authorization: Bearer <yourToken>" https://{baseUrl}/rest/api/...`). 이 기능은
  "Jira Core 8.14 and later"/"Jira Software 8.14 and later"의 Data Center/Server 버전에
  적용된다고 명시돼 있고, 10.3은 이 범위에 포함된다.
- **틀렸을 때 할 일:** 인증 방식 재설계 (해당 없음 — 확인됨).

---

## 요약

| 분류 | 항목 | 개수 |
|---|---|---|
| 공개 문서로 확인됨 | A1, A2, A6, A7, A8, A9, A10, A11, A12 | 9 |
| 사내 확인 필요 | A3 | 1 |
| 상충하는 정보 있음 | A4, A5 | 2 |

**A4(정렬 방향)와 A5(fieldId 유무)는 `doctor --jira`가 사내 첫날 반드시 확인해야 하는
최우선 항목이다.** 둘 다 "공개 문서만으로는 결론을 낼 수 없고, 서로 다른 소스가 다른 얘기를
한다"는 상태이며, A4는 설계상 치명적 가정으로 지정된 항목이기도 하다.

A9도 "확인됨"으로 분류했지만 이 설계의 핵심 전제이므로 `doctor --jira`의 검사 우선순위에서
가장 위에 두는 것을 권장한다 — 스펙 문서와 실제 인스턴스 설정(커스텀 상태 카테고리 추가 여부
등)이 다를 가능성은 여전히 남아 있다.
