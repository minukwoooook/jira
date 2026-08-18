# 사내 반입 후 절차 (운영자용)

사외에서 개발한 코드를 사내에 옮긴 뒤 처음부터 끝까지 무엇을 하는지 담은 문서다.
`docs/design.md`를 읽지 않아도 이 문서만으로 실행할 수 있게 썼다.

> **이 프로젝트의 전제를 먼저 알아두면 아래 순서가 왜 이런지 이해된다.**
> 개발 환경에는 **Oracle도 Jira도 없었다.** 그래서 DDL은 사람이 사내에서 적용하고,
> 파이프라인은 가짜 Jira 클라이언트로 테스트했고, SQL은 정적 대조로만 검증했다.
> **모든 `MERGE`·`INSERT` 문이 아래 9~12단계에서 생애 처음 실행된다.**
> 순서가 중요한 이유가 이것이다 — 읽기 전용 검사를 통과한 뒤 DDL, DDL이 끝난 뒤 코드.

---

## 0. 시작 전 준비물

| 항목 | 확인 방법 |
|---|---|
| Python 3.12 | `python3 --version` |
| Oracle 19c 접속 정보 | DSN, 전용 스키마 계정/비밀번호 |
| 스키마 권한 | `CREATE SESSION`, `CREATE TABLE`, `CREATE SEQUENCE`, `CREATE VIEW` + 테이블스페이스 쿼터 |
| Jira PAT | 대상 Jira DC 인스턴스의 Personal Access Token (읽기 권한이면 충분) |
| SQL 클라이언트 | `sqlplus` 등, DDL 파일을 실행할 수 있는 것 |

**쿼터**: 원본 보관(gzip BLOB)이 10만~100만 이슈 기준 3~6GB를 쓴다. 넉넉히 잡는다.

### `.env` 작성

`.env.example`을 복사해 채운다. **토큰은 DB에 저장하지 않는다** — 환경변수에 두고,
DB에는 그 변수의 *이름*만 기록한다.

```bash
cp .env.example .env
```

```
ORACLE_DSN=oracle.internal.example.com:1521/ORCL
ORACLE_USER=jira_dash
ORACLE_PASSWORD=<실제 비밀번호>
DISPLAY_TZ=Asia/Seoul
JIRA_SITE_A_TOKEN=<실제 PAT>
```

`DISPLAY_TZ`는 차트의 날짜 경계에만 쓰인다. DB에는 모든 시각이 UTC로 저장된다.

---

## 1. 의존성 설치 (오프라인)

사내에 PyPI 접근이 없다고 전제하고, wheel을 저장소에 함께 반입했다.

```bash
pip install --no-index --find-links vendor/ -r requirements-dev.txt
```

`requirements-dev.txt`(런타임 + pytest)를 쓰는 이유는 8단계가 pytest를 요구하기 때문이다.
`vendor/`는 `.gitignore`에 있지만 `git add -f`로 추적된다 — 반입 경로가 git 단방향이라
추적하지 않으면 wheel이 도달하지 못한다.

**여기서 실패하면** 사내 서버의 Python/OS가 wheel의 대상 플랫폼(`manylinux2014_x86_64`,
CPython 3.12)과 다르다는 뜻이다. 사외에서 맞는 플랫폼으로 `make vendor`를 다시 돌려야 한다.

---

## 2. DB 사전 점검 (읽기 전용, DDL 전)

```bash
python -m jira_dashboard.cli doctor --db --skip-schema
```

`--skip-schema`는 **아직 테이블이 없기 때문에** 붙인다. 이걸 빼면 스키마 대조가 실패해서,
정작 버전·권한 신호를 봐야 할 순간에 화면이 혼란해진다.

검사 8종 중 이 단계에서 의미 있는 것:

| ID | 무엇을 보는가 | FAIL이면 |
|---|---|---|
| DB1 | Oracle 버전이 19c인가 | 상위 버전이면 코드의 문법 전제를 재검토해야 한다 |
| DB2 | `max_string_size` | `EXTENDED`면 컬럼 폭 설계를 다시 본다 (`STANDARD` 전제로 설계됨) |
| DB3 | `Asia/Seoul` 타임존 파일 | 날짜 버킷팅이 불가 — 고정 오프셋으로 폴백해야 한다 |
| DB5 | DDL 권한 3종 | **여기서 멈춘다.** 부족한 권한을 받고 다시 시작 |
| DB8 | 타임스탬프 바인드 왕복 | 저장 시각이 밀린다는 뜻 — 사외로 돌아가야 하는 사안 |

> **DB5나 DB8이 FAIL이면 진행하지 않는다.** 나머지는 WARN이어도 기록만 하고 계속해도 된다.
> `doctor`는 실패가 있으면 종료코드 1을 낸다.

---

## 3. DDL 적용 (사람이 실행)

`docs/ddl-apply.md`에 상세가 있다. 요지는 **번호 순서가 곧 FK 의존 순서**라는 것이다.

```
@jira_dashboard/db/ddl/01_catalog.sql
@jira_dashboard/db/ddl/02_unified.sql
@jira_dashboard/db/ddl/03_issue.sql
@jira_dashboard/db/ddl/04_history.sql
@jira_dashboard/db/ddl/05_raw.sql
@jira_dashboard/db/ddl/06_ops.sql
```

생성되는 것: 테이블 16, 인덱스 15, 뷰 1, 시퀀스 1, 명명된 제약 66. 전부 `TEST_` 접두사.

---

## 4. 스키마 대조

```bash
python -m jira_dashboard.cli doctor --db
```

이제 `--skip-schema` 없이 돌린다. DB7이 DDL 파일과 실제 스키마를 대조한다 — 테이블·컬럼명뿐
아니라 **컬럼 폭·인덱스·제약·뷰·시퀀스**까지 본다. 3단계에서 파일 하나를 건너뛰었거나
중간에 오류가 났다면 여기서 드러난다.

**FAIL이면 DDL을 다시 적용한다.** `doctor`는 스스로 고치지 않는다 — 스키마 변경은 사람이
의식하고 하는 일이다.

---

## 5. 인스턴스 등록

```bash
python -m jira_dashboard.cli instance add \
    --key SITE_A \
    --base-url https://jira.internal.example.com \
    --auth-type PAT \
    --secret-ref JIRA_SITE_A_TOKEN
```

- `--key`는 이후 모든 명령에서 `--instance`로 참조하는 이름이다.
- `--secret-ref`는 **토큰이 아니라 토큰이 담긴 환경변수의 이름**이다. 토큰 자체는 DB에
  들어가지 않는다. 그 변수가 현재 설정돼 있지 않으면 경고가 나온다 — 지금 잡는 편이
  나중에 인증 실패를 디버깅하는 것보다 싸다.
- MERGE라 재실행해도 중복되지 않고 갱신된다.
- `--base-url`에 **context path를 빠뜨리지 않는다.** Jira가 `/jira` 아래 있으면 그것까지
  포함해야 한다. 빠뜨리면 카탈로그가 비고 수집이 0건으로 조용히 성공한다.

확인: `python -m jira_dashboard.cli instance list`

---

## 6. Jira API 계약 검증 (읽기 전용)

```bash
python -m jira_dashboard.cli doctor --jira --instance SITE_A --project TEST
```

사외에서는 실제 Jira가 없어 확인할 수 없었던 가정 12개(A1~A12)를 **실제 호출로 측정**한다.
`--project`에는 아무 프로젝트 키나 넣어도 되지만, changelog가 많은 프로젝트일수록 A3가
의미 있게 측정된다.

결과를 `docs/api-verification.md`에 반영한다. 특히 주의할 셋:

| ID | 왜 중요한가 |
|---|---|
| **A4** | changelog 정렬 방향. 내림차순이면 **모든 과거 시점 차트가 뒤집힌다.** FAIL이면 진행하지 말고 사외로 돌아간다 |
| **A9** | `statusCategory.key` 제공 여부. 없으면 인스턴스 간 교차 분석이라는 설계 전제가 사라진다 |
| **A3** | 인라인 changelog 상한. **WARN이 정상이다** — DC 10.3은 changelog를 페이징할 수 없고 파이프라인이 이미 그걸 처리한다. `sync` 출력의 `truncated` 값으로 몇 건이 영향받는지 보면 된다 |

> **WARN은 "측정하지 못했다"는 뜻이고 FAIL은 "가정이 틀렸다"는 뜻이다.** WARN은 기록하고
> 넘어가도 되지만, FAIL은 코드를 고쳐야 한다는 신호다.

**FAIL이 있으면 사외로 돌아가 고치고 다시 반입한다.** 사내에서 즉석으로 고치면 사외
테스트와 어긋나고, 반입이 단방향이라 되돌리기 어렵다.

---

## 7. 실데이터 픽스처 캡처

```bash
python -m jira_dashboard.cli capture --instance SITE_A --project TEST --limit 200
```

읽기 전용이다. 실제 API 응답을 `tests/fixtures/captured/`에 저장한다.
**이 디렉터리는 `.gitignore`에 있다** — 반출 금지 데이터가 사외로 나가는 경로를 막는 한 줄이니
지우지 않는다.

`--anonymize`는 기본으로 꺼져 있고, **켜도 반출 허가가 아니다.** 사내 보관 데이터의
가독성 조절용이다.

캡처된 이슈 중 `changelog.total`이 상한을 넘는 것이 없으면 경고가 나온다 — 그러면 가장
위험한 코드 경로(보충 호출)가 실데이터로 검증되지 않은 채 남는다는 뜻이다.

---

## 8. 사외 테스트를 실데이터로 재실행

```bash
JIRA_FIXTURES=captured python -m pytest
```

**이 단계가 이 프로젝트의 가장 강한 검증이다.** 사외에서 통과시킨 356개 테스트를 사내
실데이터로 그대로 돌린다. 사외 픽스처는 결국 "우리가 상상한 Jira"였고, 여기서 통과하면
그 상상이 실제와 맞았다는 뜻이다.

실패하면 **픽스처가 아니라 코드의 가정이 틀린 것**이다. 어느 테스트가 왜 실패했는지가
곧 무엇을 고쳐야 하는지다.

---

## 9. 카탈로그 발견 (첫 커밋되는 쓰기)

```bash
python -m jira_dashboard.cli sync --instance SITE_A
```

활성화된 프로젝트가 아직 없으므로(`is_enabled` 기본값 `'N'`) **이슈는 하나도 수집하지 않고**
`TEST_JIRA_PROJECT`와 `TEST_JIRA_FIELD`만 채운다.

**이것이 `MERGE` 문이 처음 커밋되는 순간이다.** 여기서 나는 오류는 아래 문제해결 표를 본다.

---

## 10. 화이트리스트 등록

```bash
python -m jira_dashboard.cli project list --instance SITE_A
python -m jira_dashboard.cli project enable --instance SITE_A --key TEST
```

**하나만 켜고 시작한다.** 처음부터 50개를 켜면 12단계에서 오류가 나도 어느 프로젝트 때문인지
알기 어렵다.

카탈로그 동기화는 프로젝트를 **발견만** 하고 절대 자동으로 켜지 않는다 — 화이트리스트는
사람이 정한다.

---

## 11. 커밋 없는 예행 (`--dry-run`)

```bash
python -m jira_dashboard.cli sync --instance SITE_A --project TEST --dry-run
```

**INSERT/MERGE를 실제로 실행하되 커밋하지 않는다.** 그래서 문법 오류·컬럼명 오타·FK 위반이
데이터를 남기지 않고 여기서 먼저 드러난다. 프로덕션에서 돌려도 안전하다.

커밋이 구조적으로 불가능하게 만들어져 있다 — 커넥션이 `commit()`을 삼키고 종료 시 롤백한다.
그래서 감사 테이블(`TEST_SYNC_RUN`)에도 행이 남지 않는다. 정상이다.

---

## 12. 첫 실제 수집

```bash
python -m jira_dashboard.cli sync --instance SITE_A --project TEST
```

출력 예:

```
ok=1 failed=0 upserted=1523 parse_failures=0 truncated=4
```

- `parse_failures` — 파싱 실패해 건너뛴 이슈 수. 0이 아니면 로그에서 어느 이슈인지 본다.
- `truncated` — **changelog를 전부 못 가져온 이슈 수.** DC 10.3이 changelog를 페이징할 수
  없어서 생기는 정상적 한계다. 그 이슈들의 과거 시점 차트는 불완전하다.

두 값 모두 **조용한 데이터 손실 카운터**다. 0이 아니면 알고 있어야 한다.

---

## 13~14. 멱등성 확인 (건너뛰지 않는다)

```bash
python -m jira_dashboard.cli sync --instance SITE_A --project TEST   # 2회차
python -m jira_dashboard.cli sync --instance SITE_A --project TEST   # 3회차
```

매 회차 뒤에 행 수를 센다. **세 번 모두 같아야 한다.**

```sql
SELECT 'issue'     t, COUNT(*) c FROM TEST_JIRA_ISSUE          UNION ALL
SELECT 'eav',         COUNT(*)   FROM TEST_ISSUE_FIELD_VALUE   UNION ALL
SELECT 'changelog',   COUNT(*)   FROM TEST_ISSUE_CHANGELOG     UNION ALL
SELECT 'history',     COUNT(*)   FROM TEST_ISSUE_FIELD_HISTORY;
```

멱등성은 **사외에서 전혀 검증되지 않았다** — DB가 없었으니 확인할 방법이 없었다.
깨져 있으면 배치마다 행이 늘어난다. 2·3회차의 `upserted`는 0에 가깝고 `skipped`가 대부분이어야
정상이다(해시가 같아 건너뛴다).

---

## 15. 이력 파생 확인

Jira에서 이슈 1건의 상태를 바꾸고 다시 동기화한 뒤:

```sql
-- changelog 신규 행
SELECT * FROM TEST_ISSUE_CHANGELOG
 WHERE issue_id = (SELECT issue_id FROM TEST_JIRA_ISSUE WHERE issue_key = 'TEST-1')
 ORDER BY changed_at DESC FETCH FIRST 5 ROWS ONLY;

-- 구간이 분할됐는지
SELECT f.field_id, h.valid_from, h.valid_to, h.val_str
  FROM TEST_ISSUE_FIELD_HISTORY h
  JOIN TEST_JIRA_FIELD f ON f.field_pk = h.field_pk
 WHERE h.issue_id = (SELECT issue_id FROM TEST_JIRA_ISSUE WHERE issue_key = 'TEST-1')
   AND f.field_id IN ('status', 'status_category')
 ORDER BY f.field_id, h.valid_from;
```

`valid_to`가 `9999-12-31`인 행이 필드마다 정확히 하나여야 하고, 그 값이 이슈의 현재
상태와 일치해야 한다.

---

## 16. 나머지 프로젝트 확장

12~14단계가 깨끗하면 프로젝트를 늘린다. 한 번에 다 켜지 말고 몇 개씩.

```bash
python -m jira_dashboard.cli project enable --instance SITE_A --key PROJ2
python -m jira_dashboard.cli sync --instance SITE_A
```

`--project`를 빼면 활성화된 전체를 돈다. 한 프로젝트가 실패해도 나머지는 계속되고,
실패한 프로젝트만 워터마크를 유지해 다음 실행이 따라잡는다.

### 일 1회 작업

```bash
python -m jira_dashboard.cli sync --instance SITE_A --daily
```

`--daily`는 필드 프로파일링(어떤 필드가 차트 축으로 쓸 만한지)과 삭제·이동 감지를 추가로
돌린다. 매 시간 돌릴 필요는 없다.

---

## 17. cron 등록

```cron
0  * * * *  flock -n /var/run/jira_sync.lock \
            /opt/jira_dashboard/.venv/bin/python -m jira_dashboard.cli sync --instance SITE_A
30 2 * * *  flock -n /var/run/jira_sync.lock \
            /opt/jira_dashboard/.venv/bin/python -m jira_dashboard.cli sync --instance SITE_A --daily
```

**`flock -n`을 빼지 않는다.** 배치가 주기보다 오래 걸리면 다음 실행과 겹치고, 같은 이슈에
대해 쓰기가 경합한다. DB의 `RUNNING` 상태로 막지 않는 이유는 프로세스가 죽으면 그 상태가
영원히 남기 때문이다 — `flock`은 커널이 자동으로 풀어준다.

---

## 문제해결

| 증상 | 원인 | 대응 |
|---|---|---|
| `unknown instance: SITE_A` | 5단계를 건너뛰었다 | `instance add` 실행 |
| `ORA-00904: invalid identifier` | SQL의 컬럼명 오타 | 정적 대조를 통과했다면 게이트가 못 보는 부분이다. `doctor --db`로 스키마부터 확인 |
| `ORA-00001: unique constraint violated` | MERGE의 `ON` 절 키가 잘못됨 | 해당 테이블의 UNIQUE 제약과 `ON` 절을 대조 |
| `ORA-02291: integrity constraint` | 적재 순서가 FK 위반 | 3단계 DDL 순서 또는 코드의 적재 순서 문제 |
| `ORA-12899: value too large` | 컬럼 폭 초과 | 폭은 DDL에서 유도해 검사하지만, 새 경로가 빠졌을 수 있다. 어느 컬럼인지 로그에서 확인 |
| 수집이 0건인데 성공 | `--base-url`에 context path 누락 | 5단계 URL 확인. `doctor --jira`의 A8/A11이 잡아준다 |
| 인증 실패 | `--secret-ref`가 가리키는 환경변수 미설정 | `doctor --jira`의 A12가 FAIL로 알려준다 |
| 2회차에 행이 늘어남 | 멱등성 깨짐 | **진행 중단.** 어느 테이블이 늘었는지 확인해 사외로 보고 |

### 스키마를 갈아엎을 때

```sql
-- 반드시 먼저 대상을 눈으로 확인한다
SELECT table_name FROM user_tables WHERE table_name LIKE 'TEST\_%' ESCAPE '\';
```

목록에 `TEST_`로 시작하지 않는 것이 하나라도 있으면 **멈추고** 조건을 다시 본다.
확인했으면 `@jira_dashboard/db/ddl/drop_all.sql` 후 3단계를 다시 실행한다.

> 이 스크립트는 **사외에서 한 번도 실행해보지 못했다.** `ESCAPE`가 빠지면 `_`가 와일드카드가
> 되어 `TESTX_...` 같은 남의 테이블까지 지운다. 테스트로 고정해 뒀지만 되돌릴 수 없는
> 작업이니 목록 확인을 생략하지 않는다.

---

## 알려진 한계

설계상 받아들인 것들이다. 버그로 보고할 필요는 없지만 알고 있어야 한다.

- **changelog 100건 초과 이슈의 이력이 불완전하다.** DC 10.3 REST에 changelog 페이징이
  없다. `sync` 출력의 `truncated`가 몇 건인지 알려준다.
- **다중값 필드의 "현재 값" 비교는 첫 번째 값만 본다** (`val_seq = 0`).
- **은퇴한 상태값**은 `/status`에 남아 있으면 정상 처리되지만, 거기서도 사라졌다면 과거
  구간이 `undefined`로 표시된다.
- **실패가 반복되는 프로젝트는 매 실행마다 전체 재수집한다.** 첫 성공에 자동으로 풀린다.
- **쿼리 API(차트 조회)는 아직 없다.** 이번 범위는 수집 파이프라인까지다. 설계는
  `docs/design.md` 6장에 있다.

---

## 참고 문서

| 문서 | 내용 |
|---|---|
| `docs/design.md` | 설계 전체 (DB 스키마, 파이프라인, 쿼리 API 설계) |
| `docs/ddl-apply.md` | DDL 적용·삭제 절차 상세 |
| `docs/api-verification.md` | Jira DC 10.3 API 가정 A1~A12와 판정 — 6단계 결과를 여기 반영 |
