# Jira Dashboard 수집 파이프라인 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 사내 Jira DC 10.3 인스턴스들의 이슈·변경이력을 Oracle 19c로 증분 수집하는 파이프라인을, **DB와 Jira가 모두 없는 사외 환경에서** 검증 가능한 만큼 검증해 완성한다.

**Architecture:** 하이브리드 저장(시스템 필드는 고정 컬럼, 커스텀 필드는 EAV, 이력은 SCD Type-2 구간 테이블). 파이프라인은 `JiraClient` 프로토콜과 리포지토리 함수만 의존하므로, 사외에서는 `FakeJiraClient` + 리포지토리 스텁으로 호출 순서와 분기를 검증한다. DDL은 사내에서 사람이 실행하고, 코드는 테이블이 존재한다고 가정한다.

**Tech Stack:** Python 3.12, `oracledb` (thin), `httpx`, `pydantic-settings`, `pytest`. **DB·컨테이너 없음.**

**Spec:** `docs/design.md`

## Global Constraints

이 절의 값은 **모든 태스크의 요구사항에 암묵적으로 포함된다.**

- **Oracle 19c 전용 문법만 사용한다.** 네이티브 `JSON` 타입, `BOOLEAN`, `IF NOT EXISTS`, `VALUES` 절, `SELECT` without `FROM` 금지 (전부 21c/23ai 기능). 인덱스는 단순 B-tree만. **사외에 DB가 없어 실행으로 잡을 수 없으므로 T3의 금지 문법 검사가 유일한 방어선이다.** (spec §2.4)
- **모든 DB 객체 이름은 `TEST_` 접두사로 시작한다.** 테이블·인덱스·제약조건·시퀀스·뷰 전부. 컬럼명은 예외. (spec §2.3)
- **`VARCHAR2`는 전부 `BYTE` 세만틱을 명시한다.** 값 컬럼(`val_str`, `canonical_value`, `raw_value`, 이력 `val_str`)은 `VARCHAR2(1000 BYTE)`. (spec §2.2)
- **모든 시각은 UTC로 저장한다.** `DEFAULT`는 `SYS_EXTRACT_UTC(SYSTIMESTAMP)`. `SYSTIMESTAMP` 단독 사용 금지. (spec §2.1)
- **모든 SQL은 바인드 변수를 쓴다.** f-string·`%`·`+`로 값을 SQL에 넣는 코드 금지. 식별자는 코드 내 화이트리스트에서만 온다.
- **SQL은 모듈 수준 상수나 함수 안의 리터럴 문자열로 둔다.** 조각을 런타임에 이어 붙이면 T3의 정적 대조가 읽을 수 없다. 개수가 가변인 바인드 목록만 예외로 하고, 테이블·컬럼 이름은 리터럴에 남긴다. (spec §11.2)
- **`max_string_size = STANDARD`(VARCHAR2 4000바이트 상한)를 전제한다.**
- **Python 3.12**, 의존성은 `oracledb`, `httpx`, `pydantic-settings`, `pytest`로 제한한다. **정적 대조에 SQL 파서 라이브러리를 추가하지 않는다** — 정규표현식으로 충분하다.
- **구간 테이블 센티넬은 `TIMESTAMP '9999-12-31 00:00:00'`이다.** `valid_to`에 NULL 금지.
- **`status_category`는 `name`이 아니라 `key`를 저장한다** — `new` / `indeterminate` / `done` / `undefined`.
- **`executemany`는 `batcherrors=False`로 쓴다.** spec §5.7은 `True`를 권하지만 의도적으로 이탈한다 — 격리 단위가 행이 아니라 **프로젝트**이기 때문이다(spec §5.8). 한 페이지에서 일부 행만 조용히 빠지면 이슈와 EAV·changelog가 어긋난 채 커밋된다.
- **커밋은 태스크의 각 TDD 사이클 끝에서 한다.** 메시지는 `feat:` / `test:` / `chore:` 접두사.

### 사외에서 검증되지 않는 것 (모든 태스크에 적용)

DB가 없으므로 아래는 **사내 첫 실행이 첫 테스트다.** 코드를 쓸 때 이 사실을 전제로 방어적으로 쓴다.

`MERGE` 문법과 `ON` 절 키 · 멱등성 · 제약조건 실제 동작 · BLOB 적재 · 시퀀스 채번 · `drop_all.sql`의 `ESCAPE` · 실행계획 · 사내 권한/쿼터

---

## File Structure

```
pyproject.toml                          의존성, pytest 설정
.env.example                            접속 정보 템플릿
docs/api-verification.md          T1    A1~A12 조사 결과 (spec §4.0)
docs/ddl-apply.md                 T2    사내 DDL 적용 절차

jira_dashboard/
  config/settings.py              T2    Settings — 환경변수 로드
  db/pool.py                      T2    커넥션 풀 + 컨텍스트 매니저 (사외에선 미실행)
  db/ddl/01_catalog.sql           T2    instance/project/field/project_field
  db/ddl/02_unified.sql           T2    unified_* 4개 + 뷰
  db/ddl/03_issue.sql             T2    시퀀스 + jira_issue + issue_field_value
  db/ddl/04_history.sql           T2    changelog + field_history
  db/ddl/05_raw.sql               T2    issue_raw + changelog_raw
  db/ddl/06_ops.sql               T2    sync_watermark + sync_run
  db/ddl/drop_all.sql             T2    TEST_ 객체 일괄 삭제 (사람이 실행)
  db/schema_map.py                T3    DDL 파싱 → {테이블: {컬럼}}
  jira/models.py                  T4    FieldDef / FieldValue / ChangelogItem / ParsedIssue
  jira/fieldmap.py                T4    SYSTEM_FIELD_MAP, value_kind_of()
  jira/parser.py                  T4    Jira JSON → 모델 (DB를 모른다)
  jira/protocol.py                T5    JiraClient Protocol, SearchPage, ChangelogPage
  jira/fake.py                    T5    FakeJiraClient + 시나리오 훅
  jira/client.py                  T11   HttpJiraClient
  db/repository/catalog.py        T6    instance/project/field/project_field
  db/repository/issue.py          T7    issue + raw + field_value
  db/repository/history.py        T8    changelog + field_history
  db/repository/sync.py           T10   watermark + sync_run
  pipeline/sync_catalog.py        T6
  pipeline/sync_issues.py         T7
  pipeline/derive_history.py      T8    구간 생성 (순수 함수) + 적재
  pipeline/profile_fields.py      T9
  pipeline/detect_deleted.py      T9
  pipeline/runner.py              T10   프로젝트 단위 격리 + 좀비 정리
  doctor/db_checks.py             T11
  doctor/jira_checks.py           T11
  capture.py                      T12
  cli.py                          T10+  sync / doctor / capture

tests/
  conftest.py                     T5    픽스처 (Fake, 스텁)
  stubs.py                        T7    리포지토리 기록용 스텁
  fixtures/synthetic/             T5    사외 픽스처
  fixtures/captured/              T12   사내 전용 (.gitignore)
  unit/                           T4,T8 parser, derive_history, doctor 판정
  static/                         T3    DDL↔SQL 대조, 금지 문법
  pipeline/                       T6-10 호출 순서·분기 (스텁)
vendor/                           T12   오프라인 wheel
```

**분할 원칙:** `parser.py`는 DB를 모르고, `repository/*`는 Jira를 모르고, `pipeline/*`은 HTTP도 SQL도 모른다. 세 번째가 새로 추가된 제약이다 — 파이프라인이 리포지토리 함수만 부르면 스텁으로 갈아끼울 수 있다.

---

### Task 1: Jira DC 10.3 API 계약 조사 (게이트)

spec §4.0의 A1~A12를 공개 문서로 최대한 좁힌다. **이 태스크 전에 `jira/` 아래 코드를 쓰지 않는다.**

**Files:**
- Create: `docs/api-verification.md`

**Interfaces:**
- Consumes: 없음
- Produces: T4(`parser.py`)와 T5(`fake.py`)가 픽스처 구조를 정하는 근거. T11(`doctor/jira_checks.py`)이 검사할 항목 목록.

- [ ] **Step 1: 문서 골격 작성**

A1~A12 각 항목에 대해 아래 블록을 만든다.

```markdown
## A4. changelog 정렬 방향

- **가정:** 오름차순(오래된 것 먼저)
- **조사 결과:** (공개 문서 확인됨 | 사내 확인 필요 | 상충하는 정보 있음)
- **근거:** URL + 인용문
- **틀렸을 때 할 일:** sync_issues의 보충 호출 startAt 기준과 derive_history 정렬을 뒤집는다
```

- [ ] **Step 2: 버전별 REST 레퍼런스 조사**

`developer.atlassian.com/server/jira/platform/rest/` 에서 **버전 셀렉터를 10.3으로 맞추고** 확인한다.

| 확인 대상 | 엔드포인트 |
|---|---|
| A1, A2, A6, A7 | `POST /rest/api/2/search` 파라미터와 응답 |
| A3, A4 | `GET /rest/api/2/issue/{key}` 의 `expand=changelog`, `startAt` 지원 여부 |
| A5 | changelog item 스키마에 `fieldId` 유무 |
| A8 | `GET /rest/api/2/field` 응답의 `schema` |
| A9 | `GET /rest/api/2/status` 응답의 `statusCategory` |
| A10 | 응답에 나타나는 user 객체 필드 |
| A11 | `GET /rest/api/2/project` |

- [ ] **Step 3: Jira 10.0 제거 엔드포인트 확인**

`developer.atlassian.com/server/jira/platform/changelog/` 에서 9.13 → 10.0 breaking change를 찾는다. 우리가 쓰는 6개(`/search`, `/issue/{key}`, `/field`, `/project`, `/status`, `/myself`) 중 제거된 것이 있는지 본다.

- [ ] **Step 4: 판정 기록**

**애매하면 "사내 확인 필요"로 둔다** — 잘못된 "확인됨"이 가장 위험하다.

- [ ] **Step 5: Commit**

```bash
git add docs/api-verification.md
git commit -m "docs: record Jira DC 10.3 API contract verification (A1-A12)"
```

---

### Task 2: 스캐폴딩 + DDL 파일 + 적용 절차 문서

**DB에 접속하지 않는다.** DDL은 파일로 만들고, 적용은 사내에서 사람이 한다.

**Files:**
- Create: `pyproject.toml`, `.env.example`, `.gitignore`
- Create: `jira_dashboard/__init__.py`, `jira_dashboard/config/settings.py`, `jira_dashboard/db/pool.py`
- Create: `jira_dashboard/db/ddl/01_catalog.sql` … `06_ops.sql`, `drop_all.sql`
- Create: `docs/ddl-apply.md`
- Create: `tests/unit/test_settings.py`

**Interfaces:**
- Consumes: 없음
- Produces:
  - `Settings` — `.oracle_dsn`, `.oracle_user`, `.oracle_password`, `.display_tz`
  - `get_pool() -> oracledb.ConnectionPool`, `db_conn() -> ContextManager[Connection]`
  - `db/ddl/*.sql` — spec §3의 전체 스키마

- [ ] **Step 1: pyproject.toml + .env.example + .gitignore**

```toml
[project]
name = "jira-dashboard"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["oracledb>=2.0", "httpx>=0.27", "pydantic-settings>=2.0"]

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.setuptools.packages.find]
include = ["jira_dashboard*"]
```

```bash
# .env.example
ORACLE_DSN=oracle.internal.example.com:1521/ORCL
ORACLE_USER=jira_dash
ORACLE_PASSWORD=set_me_on_prem
DISPLAY_TZ=Asia/Seoul
JIRA_SITE_A_TOKEN=set_me_on_prem
```

`.gitignore`: `.env`, `vendor/`, `tests/fixtures/captured/`, `__pycache__/`, `*.pyc`

- [ ] **Step 2: 실패하는 설정 테스트 작성**

```python
# tests/unit/test_settings.py
from jira_dashboard.config.settings import Settings


def test_loads_from_environment(monkeypatch):
    monkeypatch.setenv("ORACLE_DSN", "host:1521/SVC")
    monkeypatch.setenv("ORACLE_USER", "u")
    monkeypatch.setenv("ORACLE_PASSWORD", "p")
    s = Settings(_env_file=None)
    assert s.oracle_dsn == "host:1521/SVC"
    assert s.display_tz == "Asia/Seoul"


def test_display_tz_is_overridable(monkeypatch):
    monkeypatch.setenv("ORACLE_DSN", "h:1521/S")
    monkeypatch.setenv("ORACLE_USER", "u")
    monkeypatch.setenv("ORACLE_PASSWORD", "p")
    monkeypatch.setenv("DISPLAY_TZ", "UTC")
    assert Settings(_env_file=None).display_tz == "UTC"
```

- [ ] **Step 3: 테스트 실패 확인**

Run: `pytest tests/unit/test_settings.py -v`
Expected: FAIL — `ModuleNotFoundError: jira_dashboard.config.settings`

- [ ] **Step 4: settings.py + pool.py 구현**

```python
# jira_dashboard/config/settings.py
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    oracle_dsn: str
    oracle_user: str
    oracle_password: str
    display_tz: str = "Asia/Seoul"
    pool_min: int = 2
    pool_max: int = 8
    call_timeout_ms: int = 30_000


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
```

```python
# jira_dashboard/db/pool.py
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

import oracledb

from jira_dashboard.config.settings import get_settings


@lru_cache(maxsize=1)
def get_pool() -> oracledb.ConnectionPool:
    s = get_settings()
    return oracledb.create_pool(
        user=s.oracle_user, password=s.oracle_password, dsn=s.oracle_dsn,
        min=s.pool_min, max=s.pool_max, increment=1,
    )


@contextmanager
def db_conn() -> Iterator[oracledb.Connection]:
    """정상 종료 시 commit, 예외 시 rollback. 사외에서는 실행되지 않는다."""
    conn = get_pool().acquire()
    conn.call_timeout = get_settings().call_timeout_ms
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        get_pool().release(conn)
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `pytest tests/unit/test_settings.py -v`
Expected: 2 passed

- [ ] **Step 6: DDL 6개 파일 작성**

spec §3의 DDL을 아래 표대로 옮긴다. **한 글자도 바꾸지 않는다.**

| 파일 | spec 절 | 담을 객체 |
|---|---|---|
| `01_catalog.sql` | §3.1 | `test_jira_instance`, `test_jira_project`, `test_jira_field`, `test_jira_project_field` + 인덱스 |
| `02_unified.sql` | §3.2 | `test_unified_field`, `test_unified_field_member`, `test_unified_value`, `test_unified_value_member`, `test_v_unify_candidate` |
| `03_issue.sql` | §3.3 | `test_seq_issue_id`, `test_jira_issue` + 인덱스 6개, `test_issue_field_value` + `test_ix_ifv_str` |
| `04_history.sql` | §3.4 | `test_issue_changelog` + 인덱스 2개, `test_issue_field_history` + `test_ix_ifh_scan` |
| `05_raw.sql` | §3.5 | `test_issue_raw`, `test_changelog_raw` (`LOB ... DISABLE STORAGE IN ROW`) |
| `06_ops.sql` | §3.6 | `test_sync_watermark`, `test_sync_run` + `test_ix_sync_run_recent` |

**번호 순서가 FK 의존 순서다.** `02_unified.sql` 내부에서도 `test_unified_value_member`가 마지막이어야 한다.

각 문장은 `;`로 끝낸다. `drop_all.sql`은 PL/SQL 블록이므로 `/`로 끝낸다.

- [ ] **Step 7: drop_all.sql 작성**

spec §2.3의 PL/SQL 블록을 그대로 옮긴다. **`ESCAPE '\'`를 빠뜨리지 않는다** — `_`가 와일드카드가 되어 `TESTX...` 같은 남의 테이블까지 지운다. **사외에서 이 스크립트를 실행해볼 수 없으므로 눈으로 세 번 확인한다.**

- [ ] **Step 8: docs/ddl-apply.md 작성**

```markdown
# DDL 적용 절차 (사내)

## 처음 만들 때

`doctor --db --skip-schema`로 권한을 먼저 확인한 뒤, 번호 순서대로 실행한다.

    @db/ddl/01_catalog.sql
    @db/ddl/02_unified.sql
    @db/ddl/03_issue.sql
    @db/ddl/04_history.sql
    @db/ddl/05_raw.sql
    @db/ddl/06_ops.sql

끝나면 `cli doctor --db`가 스키마 대조까지 통과해야 한다.

## 갈아엎을 때

**먼저 삭제 대상을 눈으로 확인한다.** `ESCAPE` 조건이 잘못되면 되돌릴 수 없다.

    SELECT table_name FROM user_tables WHERE table_name LIKE 'TEST\_%' ESCAPE '\';

목록에 `TEST_`로 시작하지 않는 것이 하나라도 있으면 멈추고 조건을 다시 본다.
확인했으면:

    @db/ddl/drop_all.sql

그 다음 위의 01~06을 다시 실행한다.

## 필요 권한

CREATE SESSION, CREATE TABLE, CREATE SEQUENCE, CREATE VIEW + 테이블스페이스 쿼터.
원본 보관(gzip BLOB)이 3~6GB를 쓰므로 쿼터를 넉넉히 잡는다.
```

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml .env.example .gitignore jira_dashboard/ docs/ddl-apply.md tests/
git commit -m "feat: scaffolding, DDL files, and on-prem apply procedure"
```

---

### Task 3: DDL↔SQL 정적 대조 하네스

**DB 없이 잡을 수 있는 것을 전부 잡는 장치다.** T6 이후 모든 리포지토리가 이 테스트의 대상이 된다.

**Files:**
- Create: `jira_dashboard/db/schema_map.py`
- Create: `tests/static/test_schema_map.py`, `tests/static/test_ddl_rules.py`, `tests/static/test_sql_references.py`

**Interfaces:**
- Consumes: `db/ddl/*.sql` (T2)
- Produces:
  - `parse_ddl(ddl_dir) -> dict[str, set[str]]` — {테이블명(대문자): {컬럼명(대문자)}}
  - `ddl_text(ddl_dir) -> str` — 전체 DDL 원문
  - `collect_sql_literals(module) -> list[tuple[str, str]]` — [(함수명, SQL문자열)]
  - `referenced_tables(sql) -> set[str]`, `referenced_columns(sql, tables) -> set[tuple[str, str]]`

- [ ] **Step 1: 실패하는 파서 테스트 작성**

```python
# tests/static/test_schema_map.py
from jira_dashboard.db import schema_map

DDL_DIR = None  # conftest에서 주입


def test_parses_all_16_tables(ddl_dir):
    tables = schema_map.parse_ddl(ddl_dir)
    assert len(tables) == 16, sorted(tables)


def test_table_names_are_uppercase(ddl_dir):
    tables = schema_map.parse_ddl(ddl_dir)
    assert all(t == t.upper() for t in tables)


def test_extracts_columns_of_jira_issue(ddl_dir):
    tables = schema_map.parse_ddl(ddl_dir)
    cols = tables["TEST_JIRA_ISSUE"]
    for expected in ("ISSUE_ID", "STATUS_CATEGORY", "FIRST_DONE_AT",
                     "DELETE_REASON", "SYNCED_AT", "TIME_SPENT_SEC"):
        assert expected in cols, f"{expected} missing from {sorted(cols)}"


def test_does_not_treat_constraints_as_columns(ddl_dir):
    cols = schema_map.parse_ddl(ddl_dir)["TEST_JIRA_ISSUE"]
    assert not any(c.startswith("TEST_PK") or c.startswith("TEST_CK") for c in cols)
    assert "CONSTRAINT" not in cols
    assert "PRIMARY" not in cols


def test_extracts_columns_of_eav_table(ddl_dir):
    cols = schema_map.parse_ddl(ddl_dir)["TEST_ISSUE_FIELD_VALUE"]
    assert cols == {"ISSUE_ID", "FIELD_PK", "VAL_SEQ", "VAL_STR",
                    "VAL_NUM", "VAL_DATE", "VAL_ID"}


def test_lob_clause_is_not_parsed_as_column(ddl_dir):
    """05_raw.sql의 LOB (payload) STORE AS ... 절이 컬럼으로 오인되면 안 된다."""
    cols = schema_map.parse_ddl(ddl_dir)["TEST_ISSUE_RAW"]
    assert cols == {"ISSUE_ID", "PAYLOAD", "PAYLOAD_HASH", "FETCHED_AT"}
```

```python
# tests/static/conftest.py
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def ddl_dir() -> Path:
    return Path(__file__).parents[2] / "jira_dashboard" / "db" / "ddl"
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/static/ -v`
Expected: FAIL — `ModuleNotFoundError: jira_dashboard.db.schema_map`

- [ ] **Step 3: schema_map.py 구현**

```python
# jira_dashboard/db/schema_map.py
"""DDL 파일에서 테이블→컬럼 사전을 만든다.

완전한 SQL 파서가 아니다. CREATE TABLE 블록의 컬럼 정의만 뽑는 수준이면
컬럼명 오타를 잡기에 충분하다 (spec §11.2).
"""
import re
from pathlib import Path

_CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(\w+)\s*\((.*?)\)\s*(?:LOB\s*\(|;|$)",
    re.IGNORECASE | re.DOTALL,
)
# 컬럼 정의가 아닌 줄
_NOT_A_COLUMN = re.compile(
    r"^\s*(CONSTRAINT|PRIMARY|UNIQUE|FOREIGN|CHECK)\b", re.IGNORECASE
)
_COLUMN = re.compile(r"^\s*(\w+)\s+\S")


def _split_top_level(body: str) -> list[str]:
    """괄호 깊이 0의 콤마로만 나눈다. CHECK (a IN ('x','y')) 를 깨지 않기 위해."""
    parts, depth, buf = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def parse_ddl(ddl_dir: Path) -> dict[str, set[str]]:
    tables: dict[str, set[str]] = {}
    for path in sorted(Path(ddl_dir).glob("[0-9][0-9]_*.sql")):
        text = path.read_text(encoding="utf-8")
        # 주석 제거
        text = re.sub(r"--[^\n]*", "", text)
        for name, body in _CREATE_TABLE.findall(text):
            cols = set()
            for chunk in _split_top_level(body):
                if _NOT_A_COLUMN.match(chunk):
                    continue
                m = _COLUMN.match(chunk)
                if m:
                    cols.add(m.group(1).upper())
            tables[name.upper()] = cols
    return tables


def ddl_text(ddl_dir: Path) -> str:
    return "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted(Path(ddl_dir).glob("*.sql"))
    )
```

- [ ] **Step 4: 파서 테스트 통과 확인**

Run: `pytest tests/static/test_schema_map.py -v`
Expected: 6 passed. `TEST_ISSUE_RAW` 테스트가 실패하면 `LOB` 절 처리가 틀린 것이다.

- [ ] **Step 5: DDL 규칙 검사 테스트 작성 + 통과 확인**

19c 금지 문법과 전역 규약을 문자열로 검사한다. **DB가 없어 실행으로 잡을 수 없으므로 이게 유일한 방어선이다.**

```python
# tests/static/test_ddl_rules.py
import re

from jira_dashboard.db import schema_map


def test_all_objects_have_test_prefix(ddl_dir):
    text = schema_map.ddl_text(ddl_dir)
    bad = re.findall(
        r"CREATE\s+(?:TABLE|INDEX|VIEW|SEQUENCE|OR\s+REPLACE\s+VIEW)\s+(\w+)",
        text, re.IGNORECASE,
    )
    offenders = [n for n in bad if not n.upper().startswith("TEST_")]
    assert offenders == [], offenders


def test_all_constraints_have_test_prefix(ddl_dir):
    text = schema_map.ddl_text(ddl_dir)
    names = re.findall(r"CONSTRAINT\s+(\w+)", text, re.IGNORECASE)
    offenders = [n for n in names if not n.upper().startswith("TEST_")]
    assert offenders == [], offenders


def test_varchar2_always_declares_byte_semantics(ddl_dir):
    text = schema_map.ddl_text(ddl_dir)
    offenders = re.findall(r"VARCHAR2\s*\(\s*\d+\s*\)", text, re.IGNORECASE)
    assert offenders == [], offenders


def test_defaults_use_utc(ddl_dir):
    """DEFAULT SYSTIMESTAMP 는 세션 타임존에 따라 값이 흔들린다 (spec 2.1)."""
    text = schema_map.ddl_text(ddl_dir)
    bare = re.findall(r"DEFAULT\s+SYSTIMESTAMP", text, re.IGNORECASE)
    assert bare == [], bare
    assert "SYS_EXTRACT_UTC(SYSTIMESTAMP)" in text


def test_no_syntax_newer_than_19c(ddl_dir):
    text = schema_map.ddl_text(ddl_dir).upper()
    for forbidden in ("IF NOT EXISTS", " BOOLEAN", "JSON_TABLE", " AS JSON"):
        assert forbidden not in text, forbidden


def test_no_function_based_indexes_or_virtual_columns(ddl_dir):
    """spec 2.4: 인덱스는 단순 B-tree만."""
    text = schema_map.ddl_text(ddl_dir)
    assert not re.search(r"GENERATED\s+ALWAYS\s+AS\s*\(", text, re.IGNORECASE)
    for m in re.finditer(r"CREATE\s+INDEX[^;]+\(([^)]*)\)", text, re.IGNORECASE):
        assert "(" not in m.group(1), m.group(0)


def test_history_sentinel_is_used(ddl_dir):
    text = schema_map.ddl_text(ddl_dir)
    assert "TIMESTAMP '9999-12-31 00:00:00'" in text


def test_drop_all_escapes_the_underscore(ddl_dir):
    """ESCAPE 누락은 남의 테이블을 지운다. 사외에서 실행 검증이 불가하므로 문자열로 막는다."""
    text = (ddl_dir / "drop_all.sql").read_text(encoding="utf-8")
    like_clauses = re.findall(r"LIKE\s+'([^']*)'(\s+ESCAPE\s+'[^']*')?",
                             text, re.IGNORECASE)
    assert like_clauses, "no LIKE clause found"
    for pattern, escape in like_clauses:
        assert escape, f"LIKE '{pattern}' has no ESCAPE clause"
        assert pattern.startswith("TEST\\_"), pattern
    assert "PURGE" in text.upper()
    assert "CASCADE CONSTRAINTS" in text.upper()
```

Run: `pytest tests/static/test_ddl_rules.py -v`
Expected: 8 passed. 실패하면 **DDL을 고친다** (테스트가 맞다).

- [ ] **Step 6: SQL 참조 대조 하네스 작성**

리포지토리가 아직 없으므로 이 테스트는 지금 **빈 목록에 대해 통과**한다. T6부터 실제 대상이 생긴다.

```python
# tests/static/test_sql_references.py
import importlib
import inspect
import pkgutil
import re

import pytest

from jira_dashboard.db import schema_map

REPO_PACKAGE = "jira_dashboard.db.repository"
_TABLE_TOKEN = re.compile(r"\b(TEST_\w+)\b", re.IGNORECASE)
_BIND = re.compile(r":(\w+)")
# SQL로 볼 최소 신호
_LOOKS_LIKE_SQL = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE|MERGE)\b", re.IGNORECASE
)


def _repository_modules():
    try:
        pkg = importlib.import_module(REPO_PACKAGE)
    except ModuleNotFoundError:
        return []
    return [
        importlib.import_module(f"{REPO_PACKAGE}.{m.name}")
        for m in pkgutil.iter_modules(pkg.__path__)
    ]


def _sql_literals(module) -> list[tuple[str, str]]:
    """모듈의 소스에서 SQL로 보이는 문자열 리터럴을 뽑는다."""
    source = inspect.getsource(module)
    out = []
    for m in re.finditer(r'"""(.*?)"""|\'\'\'(.*?)\'\'\'|"([^"\n]{20,})"',
                         source, re.DOTALL):
        text = next(g for g in m.groups() if g is not None)
        if _LOOKS_LIKE_SQL.search(text):
            out.append((module.__name__, text))
    return out


def _all_sql() -> list[tuple[str, str]]:
    return [item for mod in _repository_modules() for item in _sql_literals(mod)]


def test_every_referenced_table_exists(ddl_dir):
    tables = schema_map.parse_ddl(ddl_dir)
    for module_name, sql in _all_sql():
        for token in _TABLE_TOKEN.findall(sql):
            assert token.upper() in tables, f"{module_name}: unknown table {token}"


def test_every_referenced_column_exists(ddl_dir):
    """SQL에 등장하는 식별자 중, 참조된 테이블들의 컬럼 합집합에 없는 것을 찾는다."""
    tables = schema_map.parse_ddl(ddl_dir)
    sql_keywords = _load_keywords()
    for module_name, sql in _all_sql():
        referenced = {t.upper() for t in _TABLE_TOKEN.findall(sql)}
        if not referenced:
            continue
        allowed = set()
        for t in referenced:
            allowed |= tables.get(t, set())
        body = _TABLE_TOKEN.sub(" ", sql)
        body = _BIND.sub(" ", body)                    # 바인드 이름은 제외
        body = re.sub(r"'[^']*'", " ", body)           # 리터럴 제외
        for ident in re.findall(r"\b([a-z_][a-z0-9_]{2,})\b", body, re.IGNORECASE):
            up = ident.upper()
            if up in sql_keywords or up in allowed:
                continue
            pytest.fail(f"{module_name}: unknown identifier {ident!r} "
                        f"(tables: {sorted(referenced)})")


def test_no_string_interpolation_into_sql():
    """값은 전부 바인드 변수로. f-string이나 % 포매팅이 있으면 injection 경로다."""
    for mod in _repository_modules():
        source = inspect.getsource(mod)
        for m in re.finditer(r'f"""(.*?)"""|f"([^"\n]*)"', source, re.DOTALL):
            text = next(g for g in m.groups() if g is not None)
            if not _LOOKS_LIKE_SQL.search(text):
                continue
            # 화이트리스트 테이블명과 바인드 placeholder 조립만 허용
            allowed = ("{table}", "{placeholders}", "{counts}", "{distincts}")
            leftover = re.sub(r"\{[^}]*\}", "\x00", text)
            for token in re.findall(r"\{[^}]*\}", text):
                assert token in allowed, f"{mod.__name__}: interpolation {token}"


def _load_keywords() -> set[str]:
    return {
        "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "NULL", "INSERT", "INTO",
        "VALUES", "UPDATE", "SET", "DELETE", "MERGE", "USING", "WHEN", "MATCHED",
        "THEN", "ELSE", "END", "CASE", "ON", "AS", "JOIN", "LEFT", "RIGHT",
        "INNER", "OUTER", "GROUP", "BY", "ORDER", "HAVING", "COUNT", "SUM", "MIN",
        "MAX", "AVG", "DISTINCT", "EXISTS", "IN", "IS", "LIKE", "ESCAPE", "DUAL",
        "NVL", "COALESCE", "TRUNC", "CAST", "TIMESTAMP", "DATE", "INTERVAL",
        "SYSTIMESTAMP", "SYS_EXTRACT_UTC", "NUMTODSINTERVAL", "TO_CHAR", "SUBSTR",
        "NEXTVAL", "CURRVAL", "LEVEL", "CONNECT", "WITH", "UNION", "ALL", "FETCH",
        "FIRST", "ROWS", "ONLY", "ROW_NUMBER", "OVER", "PARTITION", "RETURNING",
        "HOUR", "DAY", "MONTH", "YEAR", "ASC", "DESC", "BETWEEN",
    }
```

- [ ] **Step 7: 통과 확인**

Run: `pytest tests/static/ -v`
Expected: 전부 통과 (SQL 참조 테스트는 대상이 없어 자동 통과)

- [ ] **Step 8: Commit**

```bash
git add jira_dashboard/db/schema_map.py tests/static/
git commit -m "feat: static DDL-to-SQL cross-check harness"
```

---

### Task 4: 모델 + 필드 매핑 + 파서

DB도 HTTP도 모르는 순수 계층이다. **사외에서 완전히 검증된다.**

**Files:**
- Create: `jira_dashboard/jira/models.py`, `jira_dashboard/jira/fieldmap.py`, `jira_dashboard/jira/parser.py`
- Create: `tests/unit/test_parser.py`, `tests/unit/conftest.py`

**Interfaces:**
- Consumes: T1의 판정 결과
- Produces:
  - `FieldDef(field_id, field_name, is_custom, schema_type, schema_items, custom_type)`
  - `FieldValue(field_id, val_seq, val_str, val_num, val_date, val_id)`
  - `ChangelogItem(history_id, item_seq, author_user_key, author_display_name, changed_at, field_name, field_id, from_id, from_str, to_id, to_str)`
  - `ParsedIssue(jira_issue_id, issue_key, project_jira_id, issue_type_name, status_name, status_category, priority_name, resolution_name, assignee_user_key, assignee_display_name, reporter_user_key, reporter_display_name, parent_key, summary, created_at, updated_at, resolved_at, due_date, original_estimate_sec, remaining_estimate_sec, time_spent_sec, custom_values, changelog, changelog_total)`
  - `SystemFieldSpec(column_name, label_column_name, value_kind, is_dimension, is_measure)`, `SYSTEM_FIELD_MAP`, `SYNTHETIC_FIELDS`
  - `value_kind_of(schema_type, schema_items) -> str`
  - `parse_field_defs(raw) -> list[FieldDef]`
  - `extract_values(field_id, fd, raw) -> list[FieldValue]`
  - `parse_changelog(raw_histories) -> list[ChangelogItem]`
  - `parse_issue(raw, field_index, category_of) -> ParsedIssue`
  - `to_utc(text) -> datetime | None`, `to_date(text) -> date | None`, `truncate(text) -> str | None`
  - 상수 `MAX_VAL_STR_BYTES = 1000`, `SENTINEL = datetime(9999, 12, 31)`

**이 태스크의 코드는 spec §4.1(매핑표)과 §4.3(파싱 규칙)의 직역이다.** 두 표를 옆에 두고 쓴다.

- [ ] **Step 1: 실패하는 파서 테스트 작성**

spec §4.3 표의 모든 분기를 덮는다. `sample_issue`/`field_index` 픽스처는 T5에서 파일 기반으로 옮기므로, 지금은 `tests/unit/conftest.py`에 인라인으로 최소 이슈 하나를 둔다.

```python
# tests/unit/test_parser.py
from datetime import datetime, timezone

from jira_dashboard.jira.fieldmap import SYSTEM_FIELD_MAP, value_kind_of
from jira_dashboard.jira.models import FieldDef
from jira_dashboard.jira import parser


def _fd(field_id, schema_type, items=None, custom=None):
    return FieldDef(field_id, "F", True, schema_type, items, custom)


def test_to_utc_converts_offset_to_utc():
    assert parser.to_utc("2026-05-01T09:00:00.000+0900") == datetime(
        2026, 5, 1, 0, 0, tzinfo=timezone.utc
    )


def test_to_utc_handles_none():
    assert parser.to_utc(None) is None


def test_string_value_is_stripped():
    vals = parser.extract_values("customfield_1", _fd("customfield_1", "string"), "  hi  ")
    assert (vals[0].val_str, vals[0].val_num, vals[0].val_id) == ("hi", None, None)


def test_string_is_truncated_to_1000_bytes():
    vals = parser.extract_values("customfield_1", _fd("customfield_1", "string"), "가" * 500)
    assert len(vals[0].val_str.encode("utf-8")) <= 1000


def test_number_value():
    vals = parser.extract_values("customfield_2", _fd("customfield_2", "number"), 3.5)
    assert (vals[0].val_num, vals[0].val_str) == (3.5, None)


def test_date_value_is_midnight_utc():
    vals = parser.extract_values("customfield_3", _fd("customfield_3", "date"), "2026-05-01")
    assert vals[0].val_date == datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)


def test_datetime_value_is_converted_to_utc():
    vals = parser.extract_values(
        "customfield_3", _fd("customfield_3", "datetime"), "2026-05-01T09:00:00.000+0900"
    )
    assert vals[0].val_date == datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)


def test_option_keeps_id_and_value():
    raw = {"value": "Regression", "id": "10100"}
    vals = parser.extract_values("customfield_4", _fd("customfield_4", "option"), raw)
    assert (vals[0].val_str, vals[0].val_id) == ("Regression", "10100")


def test_user_uses_display_name_and_key():
    raw = {"key": "jdoe", "name": "jdoe", "displayName": "Jane Doe"}
    vals = parser.extract_values("customfield_5", _fd("customfield_5", "user"), raw)
    assert (vals[0].val_str, vals[0].val_id) == ("Jane Doe", "jdoe")


def test_named_entity_uses_name():
    raw = {"name": "Blocker", "id": "1"}
    vals = parser.extract_values("priority", _fd("priority", "priority"), raw)
    assert (vals[0].val_str, vals[0].val_id) == ("Blocker", "1")


def test_array_produces_one_row_per_element_with_seq():
    raw = [{"value": "A", "id": "1"}, {"value": "B", "id": "2"}]
    fd = _fd("customfield_6", "array", items="option")
    vals = parser.extract_values("customfield_6", fd, raw)
    assert [(v.val_seq, v.val_str, v.val_id) for v in vals] == [
        (0, "A", "1"), (1, "B", "2")
    ]


def test_array_of_plain_strings():
    fd = _fd("labels", "array", items="string")
    vals = parser.extract_values("labels", fd, ["urgent", "ux"])
    assert [(v.val_seq, v.val_str) for v in vals] == [(0, "urgent"), (1, "ux")]


def test_empty_array_produces_no_rows():
    fd = _fd("labels", "array", items="string")
    assert parser.extract_values("labels", fd, []) == []


def test_null_value_produces_no_rows():
    assert parser.extract_values("customfield_1", _fd("customfield_1", "string"), None) == []


def test_unknown_plugin_type_falls_back_to_json_string():
    fd = _fd("customfield_9", "any", custom="com.example:weird")
    vals = parser.extract_values("customfield_9", fd, {"a": 1})
    assert vals[0].val_str == '{"a": 1}'


def test_value_kind_of():
    assert value_kind_of("number", None) == "NUM"
    assert value_kind_of("date", None) == "DATE"
    assert value_kind_of("datetime", None) == "DATE"
    assert value_kind_of("array", "option") == "MULTI"
    assert value_kind_of("option", None) == "STR"
    assert value_kind_of("string", None) == "STR"


def test_assignee_maps_to_user_key_with_display_label():
    """spec 4.1: 동명이인이 합쳐지지 않도록 그룹핑 키와 라벨을 분리한다."""
    spec = SYSTEM_FIELD_MAP["assignee"]
    assert spec.column_name == "assignee_user_key"
    assert spec.label_column_name == "assignee_display_name"


def test_summary_is_not_a_dimension():
    assert SYSTEM_FIELD_MAP["summary"].is_dimension is False


def test_parse_issue_stores_status_category_key(sample_issue, field_index):
    issue = parser.parse_issue(sample_issue, field_index, category_of={"완료": "done"})
    assert issue.status_category == "done"
    assert issue.status_name == "완료"


def test_parse_issue_falls_back_to_category_map(sample_issue, field_index):
    """응답에 statusCategory가 없으면 /status로 만든 사전을 쓴다."""
    sample_issue["fields"]["status"].pop("statusCategory", None)
    issue = parser.parse_issue(sample_issue, field_index, category_of={"완료": "done"})
    assert issue.status_category == "done"


def test_parse_issue_uses_undefined_for_unknown_status(sample_issue, field_index):
    sample_issue["fields"]["status"].pop("statusCategory", None)
    issue = parser.parse_issue(sample_issue, field_index, category_of={})
    assert issue.status_category == "undefined"


def test_parse_issue_excludes_system_fields_from_custom_values(sample_issue, field_index):
    issue = parser.parse_issue(sample_issue, field_index, category_of={"완료": "done"})
    custom_ids = {v.field_id for v in issue.custom_values}
    assert "summary" not in custom_ids
    assert "status" not in custom_ids


def test_parse_issue_includes_multi_value_system_fields_as_custom(sample_issue, field_index):
    """labels는 시스템 필드지만 다중값이라 EAV로 간다 (spec 4.1)."""
    issue = parser.parse_issue(sample_issue, field_index, category_of={"완료": "done"})
    assert "labels" in {v.field_id for v in issue.custom_values}


def test_parse_issue_reports_changelog_total(sample_issue, field_index):
    issue = parser.parse_issue(sample_issue, field_index, category_of={"완료": "done"})
    assert issue.changelog_total >= len(issue.changelog)


def test_parse_changelog_assigns_item_seq_within_history():
    raw = [{
        "id": "1001", "created": "2026-05-01T09:00:00.000+0900",
        "author": {"key": "jdoe", "displayName": "Jane"},
        "items": [
            {"field": "status", "fieldId": "status",
             "fromString": "To Do", "toString": "완료"},
            {"field": "resolution", "fieldId": "resolution",
             "fromString": None, "toString": "Done"},
        ],
    }]
    items = parser.parse_changelog(raw)
    assert [(i.history_id, i.item_seq, i.field_id) for i in items] == [
        ("1001", 0, "status"), ("1001", 1, "resolution")
    ]


def test_parse_changelog_keeps_field_id_none_when_absent():
    """A5가 거짓인 경우. field_pk 매칭은 이름으로 하되 모호하면 NULL이다."""
    raw = [{
        "id": "1", "created": "2026-05-01T09:00:00.000+0900", "author": {},
        "items": [{"field": "Link", "toString": "blocks ABC-1"}],
    }]
    assert parser.parse_changelog(raw)[0].field_id is None
```

- [ ] **Step 2: conftest에 최소 픽스처 추가**

```python
# tests/unit/conftest.py
import pytest

from jira_dashboard.jira import parser

_FIELDS = [
    {"id": "summary", "name": "Summary", "custom": False,
     "schema": {"type": "string"}},
    {"id": "status", "name": "Status", "custom": False,
     "schema": {"type": "status"}},
    {"id": "labels", "name": "Labels", "custom": False,
     "schema": {"type": "array", "items": "string"}},
    {"id": "created", "name": "Created", "custom": False,
     "schema": {"type": "datetime"}},
    {"id": "updated", "name": "Updated", "custom": False,
     "schema": {"type": "datetime"}},
    {"id": "customfield_10001", "name": "결함원인", "custom": True,
     "schema": {"type": "option", "custom": "…:select"}},
]


@pytest.fixture
def field_index():
    return {fd.field_id: fd for fd in parser.parse_field_defs(_FIELDS)}


@pytest.fixture
def sample_issue():
    return {
        "id": "10100", "key": "PROJ-1",
        "fields": {
            "project": {"id": "10000", "key": "PROJ"},
            "summary": "sample",
            "status": {"name": "완료", "id": "10",
                       "statusCategory": {"id": 3, "key": "done"}},
            "labels": ["urgent", "ux"],
            "created": "2026-01-01T09:00:00.000+0900",
            "updated": "2026-06-01T09:00:00.000+0900",
            "customfield_10001": {"value": "Regression", "id": "10100"},
        },
        "changelog": {"startAt": 0, "maxResults": 100, "total": 1, "histories": [{
            "id": "1", "created": "2026-03-01T09:00:00.000+0900",
            "author": {"key": "jdoe", "displayName": "Jane"},
            "items": [{"field": "status", "fieldId": "status",
                       "fromString": "To Do", "toString": "완료"}],
        }]},
    }
```

- [ ] **Step 3: 테스트 실패 확인**

Run: `pytest tests/unit/test_parser.py -v`
Expected: FAIL — `ModuleNotFoundError: jira_dashboard.jira.models`

- [ ] **Step 4: models.py + fieldmap.py 구현**

```python
# jira_dashboard/jira/models.py
from dataclasses import dataclass
from datetime import date, datetime

SENTINEL = datetime(9999, 12, 31)
MAX_VAL_STR_BYTES = 1000


@dataclass(frozen=True)
class FieldDef:
    field_id: str
    field_name: str
    is_custom: bool
    schema_type: str | None
    schema_items: str | None
    custom_type: str | None


@dataclass(frozen=True)
class FieldValue:
    field_id: str
    val_seq: int
    val_str: str | None
    val_num: float | None
    val_date: datetime | None
    val_id: str | None


@dataclass(frozen=True)
class ChangelogItem:
    history_id: str
    item_seq: int
    author_user_key: str | None
    author_display_name: str | None
    changed_at: datetime
    field_name: str
    field_id: str | None
    from_id: str | None
    from_str: str | None
    to_id: str | None
    to_str: str | None


@dataclass(frozen=True)
class ParsedIssue:
    jira_issue_id: str
    issue_key: str
    project_jira_id: str
    issue_type_name: str | None
    status_name: str | None
    status_category: str | None
    priority_name: str | None
    resolution_name: str | None
    assignee_user_key: str | None
    assignee_display_name: str | None
    reporter_user_key: str | None
    reporter_display_name: str | None
    parent_key: str | None
    summary: str | None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    due_date: date | None
    original_estimate_sec: int | None
    remaining_estimate_sec: int | None
    time_spent_sec: int | None
    custom_values: tuple[FieldValue, ...]
    changelog: tuple[ChangelogItem, ...]
    changelog_total: int
```

```python
# jira_dashboard/jira/fieldmap.py
from dataclasses import dataclass


@dataclass(frozen=True)
class SystemFieldSpec:
    column_name: str
    label_column_name: str | None
    value_kind: str
    is_dimension: bool
    is_measure: bool


# spec §4.1 매핑표. 여기 없는 필드는 전부 EAV로 간다.
# labels/components/fixVersions는 시스템 필드지만 다중값이라 의도적으로 제외했다.
SYSTEM_FIELD_MAP: dict[str, SystemFieldSpec] = {
    "issuetype":            SystemFieldSpec("issue_type_name", None, "STR", True, False),
    "status":               SystemFieldSpec("status_name", None, "STR", True, False),
    "status_category":      SystemFieldSpec("status_category", None, "STR", True, False),
    "priority":             SystemFieldSpec("priority_name", None, "STR", True, False),
    "resolution":           SystemFieldSpec("resolution_name", None, "STR", True, False),
    "assignee":             SystemFieldSpec("assignee_user_key",
                                            "assignee_display_name", "STR", True, False),
    "reporter":             SystemFieldSpec("reporter_user_key",
                                            "reporter_display_name", "STR", True, False),
    "parent":               SystemFieldSpec("parent_key", None, "STR", True, False),
    "summary":              SystemFieldSpec("summary", None, "STR", False, False),
    "created":              SystemFieldSpec("created_at", None, "DATE", True, False),
    "updated":              SystemFieldSpec("updated_at", None, "DATE", True, False),
    "resolutiondate":       SystemFieldSpec("resolved_at", None, "DATE", True, False),
    "duedate":              SystemFieldSpec("due_date", None, "DATE", True, False),
    "first_done_at":        SystemFieldSpec("first_done_at", None, "DATE", True, False),
    "timeoriginalestimate": SystemFieldSpec("original_estimate_sec", None,
                                            "NUM", False, True),
    "timeestimate":         SystemFieldSpec("remaining_estimate_sec", None,
                                            "NUM", False, True),
    "timespent":            SystemFieldSpec("time_spent_sec", None, "NUM", False, True),
}

# /rest/api/2/field 에 없어서 sync_catalog가 직접 만들어 넣는 합성 필드 (spec §4.1)
SYNTHETIC_FIELDS: dict[str, str] = {
    "status_category": "Status Category",
    "first_done_at": "First Done At",
}


def value_kind_of(schema_type: str | None, schema_items: str | None) -> str:
    if schema_type == "array":
        return "MULTI"
    if schema_type == "number":
        return "NUM"
    if schema_type in ("date", "datetime"):
        return "DATE"
    return "STR"
```

- [ ] **Step 5: parser.py 구현**

```python
# jira_dashboard/jira/parser.py
import json
from collections.abc import Mapping
from datetime import date, datetime, timezone

from jira_dashboard.jira.fieldmap import SYSTEM_FIELD_MAP
from jira_dashboard.jira.models import (
    MAX_VAL_STR_BYTES, ChangelogItem, FieldDef, FieldValue, ParsedIssue,
)

_VALUE_TYPES = {"option", "option-with-child"}
_NAME_TYPES = {"priority", "status", "resolution", "issuetype", "version", "component"}


def to_utc(text: str | None) -> datetime | None:
    if not text:
        return None
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def to_date(text: str | None) -> date | None:
    return date.fromisoformat(text) if text else None


def truncate(text: str | None) -> str | None:
    if text is None:
        return None
    text = text.strip()
    raw = text.encode("utf-8")
    if len(raw) <= MAX_VAL_STR_BYTES:
        return text
    return raw[:MAX_VAL_STR_BYTES].decode("utf-8", errors="ignore")


def parse_field_defs(raw: list[dict]) -> list[FieldDef]:
    out = []
    for f in raw:
        schema = f.get("schema") or {}
        out.append(FieldDef(
            field_id=f["id"],
            field_name=f.get("name") or f["id"],
            is_custom=bool(f.get("custom", False)),
            schema_type=schema.get("type"),
            schema_items=schema.get("items"),
            custom_type=schema.get("custom"),
        ))
    return out


def _scalar(field_id: str, seq: int, fd: FieldDef, raw) -> FieldValue | None:
    if raw is None:
        return None
    t = fd.schema_items if fd.schema_type == "array" else fd.schema_type

    if t == "number":
        return FieldValue(field_id, seq, None, float(raw), None, None)
    if t == "date":
        d = to_date(raw)
        return FieldValue(field_id, seq, None, None,
                          datetime(d.year, d.month, d.day, tzinfo=timezone.utc), None)
    if t == "datetime":
        return FieldValue(field_id, seq, None, None, to_utc(raw), None)
    if isinstance(raw, dict):
        if t == "user" or "displayName" in raw:
            return FieldValue(field_id, seq, truncate(raw.get("displayName")),
                              None, None, raw.get("key") or raw.get("name"))
        if t in _VALUE_TYPES or "value" in raw:
            return FieldValue(field_id, seq, truncate(raw.get("value")),
                              None, None, raw.get("id"))
        if t in _NAME_TYPES or "name" in raw:
            return FieldValue(field_id, seq, truncate(raw.get("name")),
                              None, None, raw.get("id"))
        return FieldValue(field_id, seq,
                          truncate(json.dumps(raw, ensure_ascii=False)),
                          None, None, None)
    return FieldValue(field_id, seq, truncate(str(raw)), None, None, None)


def extract_values(field_id: str, fd: FieldDef, raw) -> list[FieldValue]:
    if raw is None:
        return []
    if fd.schema_type == "array":
        if not isinstance(raw, list):
            return []
        out = []
        for i, element in enumerate(raw):
            v = _scalar(field_id, i, fd, element)
            if v is not None:
                out.append(v)
        return out
    v = _scalar(field_id, 0, fd, raw)
    return [v] if v is not None else []


def parse_changelog(raw_histories: list[dict]) -> list[ChangelogItem]:
    out = []
    for h in raw_histories:
        author = h.get("author") or {}
        changed_at = to_utc(h["created"])
        for seq, item in enumerate(h.get("items") or []):
            out.append(ChangelogItem(
                history_id=str(h["id"]),
                item_seq=seq,
                author_user_key=author.get("key") or author.get("name"),
                author_display_name=author.get("displayName"),
                changed_at=changed_at,
                field_name=item.get("field") or "",
                field_id=item.get("fieldId"),
                from_id=item.get("from"),
                from_str=item.get("fromString"),
                to_id=item.get("to"),
                to_str=item.get("toString"),
            ))
    return out


def _named(obj) -> str | None:
    return obj.get("name") if isinstance(obj, dict) else None


def _is_multi_value(fd: FieldDef) -> bool:
    return fd.schema_type == "array"


def parse_issue(
    raw: dict,
    field_index: Mapping[str, FieldDef],
    category_of: Mapping[str, str],
) -> ParsedIssue:
    f = raw["fields"]
    status = f.get("status") or {}
    status_name = status.get("name")
    # A9: statusCategory.key 우선, 없으면 /status 사전으로 폴백
    category = ((status.get("statusCategory") or {}).get("key")
                or category_of.get(status_name or "")
                or "undefined")
    assignee = f.get("assignee") or {}
    reporter = f.get("reporter") or {}
    parent = f.get("parent") or {}
    changelog = raw.get("changelog") or {}

    custom: list[FieldValue] = []
    for field_id, value in f.items():
        fd = field_index.get(field_id)
        if fd is None:
            continue
        # 고정 컬럼으로 가는 시스템 필드는 EAV에 넣지 않는다.
        # 단 다중값이면 고정 컬럼에 담을 수 없으므로 EAV로 보낸다 (spec §4.1).
        if field_id in SYSTEM_FIELD_MAP and not _is_multi_value(fd):
            continue
        custom.extend(extract_values(field_id, fd, value))

    return ParsedIssue(
        jira_issue_id=str(raw["id"]),
        issue_key=raw["key"],
        project_jira_id=str((f.get("project") or {})["id"]),
        issue_type_name=_named(f.get("issuetype")),
        status_name=status_name,
        status_category=category,
        priority_name=_named(f.get("priority")),
        resolution_name=_named(f.get("resolution")),
        assignee_user_key=assignee.get("key") or assignee.get("name"),
        assignee_display_name=assignee.get("displayName"),
        reporter_user_key=reporter.get("key") or reporter.get("name"),
        reporter_display_name=reporter.get("displayName"),
        parent_key=parent.get("key"),
        summary=(f.get("summary") or "")[:1000] or None,
        created_at=to_utc(f["created"]),
        updated_at=to_utc(f["updated"]),
        resolved_at=to_utc(f.get("resolutiondate")),
        due_date=to_date(f.get("duedate")),
        original_estimate_sec=f.get("timeoriginalestimate"),
        remaining_estimate_sec=f.get("timeestimate"),
        time_spent_sec=f.get("timespent"),
        custom_values=tuple(custom),
        changelog=tuple(parse_changelog(changelog.get("histories") or [])),
        changelog_total=int(changelog.get("total", 0)),
    )
```

- [ ] **Step 6: 테스트 통과 확인**

Run: `pytest tests/unit/test_parser.py -v`
Expected: 26 passed

- [ ] **Step 7: Commit**

```bash
git add jira_dashboard/jira/ tests/unit/
git commit -m "feat: pure Jira JSON parser with field type extraction"
```

---

### Task 5: `JiraClient` 프로토콜 + `FakeJiraClient` + 합성 픽스처

**사외 개발의 축이다.** 파이프라인이 HTTP를 모르게 만들고, spec §10의 코너케이스를 파이썬으로 재현한다.

**Files:**
- Create: `jira_dashboard/jira/protocol.py`, `jira_dashboard/jira/fake.py`
- Create: `tests/fixtures/synthetic/{fields,projects,statuses,issues}.json`, `README.md`
- Create: `tests/conftest.py`
- Create: `tests/unit/test_fake_client.py`
- Delete: `tests/unit/conftest.py` 의 인라인 픽스처 (파일 기반으로 이전)

**Interfaces:**
- Consumes: `parse_field_defs` (T4)
- Produces:
  - `SearchPage(start_at, max_results, total, issues)`, `ChangelogPage(start_at, max_results, total, histories)`
  - `JiraTransientError(status)`, `JiraAuthError`
  - `JiraClient` Protocol — `get_fields`, `get_projects`, `get_statuses`, `search_issues`, `get_issue_changelog`, `get_issue`
  - `FakeJiraClient(fixture_dir, *, server_max_results=100, changelog_inline_limit=100)`
  - 훅: `.fail_on_call(n, status)`, `.mutate_before_page(n, fn)`, `.move_issue(id, project_jira_id, new_key, *, whitelisted)`, `.delete_issue(id)`, `.truncate_changelog(id, keep)`
  - 픽스처 `fixture_dir`, `fake_jira`, `field_index`, `category_of`, `sample_issue`

- [ ] **Step 1: 합성 픽스처 작성**

`tests/fixtures/synthetic/README.md`를 **먼저** 쓴다. 이 파일들의 성격을 오해하면 안 된다.

```markdown
# 합성 픽스처

이 디렉터리의 JSON은 **실제 Jira 응답이 아니다.** `docs/design.md` §4.0의 A1~A12 가정을
그대로 구현한 것이며, 검증된 사실이 아니다.

사내에서 `cli capture`로 만든 `../captured/`를 대상으로 같은 테스트를 돌려
(`JIRA_FIXTURES=captured pytest`) 가정이 실제와 맞는지 판정한다.
```

`fields.json` — 시스템 필드 + 커스텀 필드 4종(옵션/숫자/날짜/멀티옵션):

```json
[
  {"id": "summary", "name": "Summary", "custom": false, "schema": {"type": "string"}},
  {"id": "issuetype", "name": "Issue Type", "custom": false, "schema": {"type": "issuetype"}},
  {"id": "status", "name": "Status", "custom": false, "schema": {"type": "status"}},
  {"id": "priority", "name": "Priority", "custom": false, "schema": {"type": "priority"}},
  {"id": "resolution", "name": "Resolution", "custom": false, "schema": {"type": "resolution"}},
  {"id": "assignee", "name": "Assignee", "custom": false, "schema": {"type": "user"}},
  {"id": "reporter", "name": "Reporter", "custom": false, "schema": {"type": "user"}},
  {"id": "created", "name": "Created", "custom": false, "schema": {"type": "datetime"}},
  {"id": "updated", "name": "Updated", "custom": false, "schema": {"type": "datetime"}},
  {"id": "resolutiondate", "name": "Resolved", "custom": false, "schema": {"type": "datetime"}},
  {"id": "duedate", "name": "Due Date", "custom": false, "schema": {"type": "date"}},
  {"id": "timespent", "name": "Time Spent", "custom": false, "schema": {"type": "number"}},
  {"id": "labels", "name": "Labels", "custom": false,
   "schema": {"type": "array", "items": "string"}},
  {"id": "customfield_10001", "name": "결함원인", "custom": true,
   "schema": {"type": "option",
              "custom": "com.atlassian.jira.plugin.system.customfieldtypes:select"}},
  {"id": "customfield_10002", "name": "Story Points", "custom": true,
   "schema": {"type": "number",
              "custom": "com.atlassian.jira.plugin.system.customfieldtypes:float"}},
  {"id": "customfield_10003", "name": "발견일", "custom": true,
   "schema": {"type": "date",
              "custom": "com.atlassian.jira.plugin.system.customfieldtypes:datepicker"}},
  {"id": "customfield_10004", "name": "영향모듈", "custom": true,
   "schema": {"type": "array", "items": "option",
              "custom": "com.atlassian.jira.plugin.system.customfieldtypes:multiselect"}}
]
```

`statuses.json` — **`statusCategory.key`가 핵심(A9)**. 상태명은 한국어로 두어 인스턴스 간 차이를 재현한다:

```json
[
  {"id": "1",  "name": "To Do",  "statusCategory": {"id": 2, "key": "new"}},
  {"id": "3",  "name": "개발중", "statusCategory": {"id": 4, "key": "indeterminate"}},
  {"id": "4",  "name": "리뷰중", "statusCategory": {"id": 4, "key": "indeterminate"}},
  {"id": "10", "name": "완료",   "statusCategory": {"id": 3, "key": "done"}}
]
```

`projects.json`:

```json
[
  {"id": "10000", "key": "PROJ",  "name": "Main Project"},
  {"id": "10001", "key": "OTHER", "name": "Other Project"}
]
```

`issues.json` — **8건. 아래를 반드시 전부 포함한다.**

| # | 이슈 | 목적 |
|---|---|---|
| 1 | 커스텀 필드 4개 + labels 2개 전부 채워짐 | 파서 기본 경로, 다중값 `val_seq` |
| 2 | 커스텀 필드 전부 `null`, labels `[]` | 값 없음 → EAV 행 0개 |
| 3 | `resolution` 없이 `status`만 `완료` | `first_done_at`이 필요한 이유 |
| 4 | changelog 없음 | 구간 1개 |
| 5 | 같은 `created`에 status 변경 2건 | 길이 0 구간 제거 |
| 6 | `To Do → 개발중 → 리뷰중 → 완료` | `status_category` 연속 구간 병합 |
| 7 | `changelog.total = 150`, `histories` 150개 | 보충 호출 (A3) |
| 8 | `timespent` `null`, `duedate` `null` | Time Tracking 미사용 프로파일링 |

`updated`는 8건이 서로 다르게, **오름차순으로 정렬 가능하게** 둔다. 각 이슈의 `changelog`는 `{"startAt":0,"maxResults":100,"total":N,"histories":[...]}` 형태이고 `histories`는 `created` **오름차순**이다(A4 가정).

- [ ] **Step 2: 실패하는 Fake 테스트 작성**

```python
# tests/unit/test_fake_client.py
import pytest

from jira_dashboard.jira.fake import FakeJiraClient
from jira_dashboard.jira.protocol import JiraTransientError

JQL = "project = PROJ ORDER BY updated ASC"


def test_search_paginates_by_start_at(fake_jira):
    p1 = fake_jira.search_issues(JQL, 0, 3, True)
    p2 = fake_jira.search_issues(JQL, 3, 3, True)
    assert len(p1.issues) == 3
    assert p1.total == p2.total
    assert {i["id"] for i in p1.issues}.isdisjoint({i["id"] for i in p2.issues})


def test_search_orders_by_updated_ascending(fake_jira):
    page = fake_jira.search_issues(JQL, 0, 100, True)
    updated = [i["fields"]["updated"] for i in page.issues]
    assert updated == sorted(updated)


def test_search_filters_by_updated_watermark(fake_jira):
    everything = fake_jira.search_issues(JQL, 0, 100, True)
    cutoff = everything.issues[-1]["fields"]["updated"][:16]
    page = fake_jira.search_issues(
        f'project = PROJ AND updated >= "{cutoff}" ORDER BY updated ASC', 0, 100, True
    )
    assert 0 < len(page.issues) < everything.total


def test_server_may_shrink_max_results(fixture_dir):
    """A7: 요청 100인데 서버가 2로 줄여 응답할 수 있다."""
    client = FakeJiraClient(fixture_dir, server_max_results=2)
    page = client.search_issues(JQL, 0, 100, True)
    assert page.max_results == 2
    assert len(page.issues) <= 2


def test_changelog_over_limit_is_truncated_inline(fake_jira):
    page = fake_jira.search_issues(JQL, 0, 100, True)
    big = [i for i in page.issues
           if i["changelog"]["total"] > i["changelog"]["maxResults"]]
    assert big, "fixture must contain an issue with >100 changelog entries"
    assert len(big[0]["changelog"]["histories"]) == big[0]["changelog"]["maxResults"]


def test_get_issue_changelog_continues_from_start_at(fake_jira):
    page = fake_jira.search_issues(JQL, 0, 100, True)
    big = next(i for i in page.issues if i["changelog"]["total"] > 100)
    rest = fake_jira.get_issue_changelog(big["key"], start_at=100)
    assert rest.start_at == 100
    assert rest.total == big["changelog"]["total"]
    assert len(rest.histories) > 0


def test_mutate_before_page_hook_runs_once(fake_jira):
    seen = []

    def bump(issues):
        issues[0]["fields"]["updated"] = "2099-01-01T00:00:00.000+0900"
        seen.append(True)

    fake_jira.mutate_before_page(2, bump)
    fake_jira.search_issues(JQL, 0, 2, True)
    fake_jira.search_issues(JQL, 2, 2, True)
    assert seen == [True]


def test_fail_on_call_raises_transient(fake_jira):
    fake_jira.fail_on_call(1, 429)
    with pytest.raises(JiraTransientError) as exc:
        fake_jira.search_issues(JQL, 0, 2, True)
    assert exc.value.status == 429


def test_moved_issue_leaves_source_project(fake_jira):
    before = fake_jira.search_issues(JQL, 0, 100, True)
    target = before.issues[0]
    fake_jira.move_issue(str(target["id"]), "10001", "OTHER-99", whitelisted=True)
    after = fake_jira.search_issues(JQL, 0, 100, True)
    assert str(target["id"]) not in {str(i["id"]) for i in after.issues}
    moved = fake_jira.get_issue(str(target["id"]), ["project"])
    assert moved["fields"]["project"]["id"] == "10001"


def test_deleted_issue_returns_none(fake_jira):
    page = fake_jira.search_issues(JQL, 0, 100, True)
    victim = str(page.issues[0]["id"])
    fake_jira.delete_issue(victim)
    assert fake_jira.get_issue(victim, ["project"]) is None


def test_moved_out_issue_is_still_resolvable(fake_jira):
    page = fake_jira.search_issues(JQL, 0, 100, True)
    target = str(page.issues[0]["id"])
    fake_jira.move_issue(target, "99999", "ARCHIVE-1", whitelisted=False)
    assert fake_jira.get_issue(target, ["project"]) is not None
```

- [ ] **Step 3: 테스트 실패 확인**

Run: `pytest tests/unit/test_fake_client.py -v`
Expected: FAIL — `ModuleNotFoundError: jira_dashboard.jira.fake`

- [ ] **Step 4: protocol.py 구현**

```python
# jira_dashboard/jira/protocol.py
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SearchPage:
    start_at: int
    max_results: int       # 서버가 실제로 적용한 값 (요청값이 아니다 — A7)
    total: int
    issues: list[dict]


@dataclass(frozen=True)
class ChangelogPage:
    start_at: int
    max_results: int
    total: int
    histories: list[dict]


class JiraTransientError(RuntimeError):
    """429/503 등 재시도 가능한 오류."""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(f"HTTP {status} {message}".strip())
        self.status = status


class JiraAuthError(RuntimeError):
    """401/403 — 재시도하지 않고 즉시 중단한다."""


class JiraClient(Protocol):
    def get_fields(self) -> list[dict]: ...
    def get_projects(self) -> list[dict]: ...
    def get_statuses(self) -> list[dict]: ...
    def search_issues(self, jql: str, start_at: int, max_results: int,
                      expand_changelog: bool) -> SearchPage: ...
    def get_issue_changelog(self, issue_key: str, start_at: int) -> ChangelogPage: ...
    def get_issue(self, jira_issue_id: str, fields: list[str]) -> dict | None: ...
```

- [ ] **Step 5: fake.py 구현**

```python
# jira_dashboard/jira/fake.py
import copy
import json
import re
from collections.abc import Callable
from pathlib import Path

from jira_dashboard.jira.protocol import ChangelogPage, JiraTransientError, SearchPage

_PROJECT_RE = re.compile(r"project\s*=\s*(\w+)", re.IGNORECASE)
_UPDATED_RE = re.compile(r'updated\s*>=\s*"([^"]+)"', re.IGNORECASE)


class FakeJiraClient:
    """픽스처 기반 JiraClient. JQL의 최소 부분집합만 해석한다.

    지원: `project = X`, `updated >= "..."`, `ORDER BY updated ASC`
    그 외 절은 무시한다. 실제 JQL 파서 동작은 사내에서만 검증된다 (spec §11.8).
    """

    def __init__(self, fixture_dir: Path, *, server_max_results: int = 100,
                 changelog_inline_limit: int = 100) -> None:
        self._dir = Path(fixture_dir)
        self._server_max_results = server_max_results
        self._inline_limit = changelog_inline_limit
        self._fields = self._load("fields.json")
        self._projects = self._load("projects.json")
        self._statuses = self._load("statuses.json")
        self._issues = {str(i["id"]): i for i in self._load("issues.json")}
        self._deleted: set[str] = set()
        self._call_count = 0
        self._failures: dict[int, int] = {}
        self._page_hooks: dict[int, Callable[[list[dict]], None]] = {}

    def _load(self, name: str):
        return json.loads((self._dir / name).read_text(encoding="utf-8"))

    # ---- 시나리오 훅 (spec §7.2) ------------------------------------
    def fail_on_call(self, call_number: int, status: int) -> None:
        self._failures[call_number] = status

    def mutate_before_page(self, call_number: int,
                           fn: Callable[[list[dict]], None]) -> None:
        self._page_hooks[call_number] = fn

    def move_issue(self, jira_issue_id: str, project_jira_id: str,
                   new_key: str, *, whitelisted: bool) -> None:
        issue = self._issues[jira_issue_id]
        issue["key"] = new_key
        issue["fields"]["project"] = {"id": project_jira_id,
                                      "key": new_key.split("-")[0]}
        issue["fields"]["updated"] = "2099-01-01T00:00:00.000+0900"
        issue["_whitelisted"] = whitelisted

    def delete_issue(self, jira_issue_id: str) -> None:
        self._deleted.add(jira_issue_id)

    def truncate_changelog(self, jira_issue_id: str, keep: int) -> None:
        cl = self._issues[jira_issue_id]["changelog"]
        cl["histories"] = cl["histories"][:keep]

    # ---- JiraClient ------------------------------------------------
    def _tick(self) -> int:
        self._call_count += 1
        status = self._failures.pop(self._call_count, None)
        if status is not None:
            raise JiraTransientError(status)
        return self._call_count

    def get_fields(self) -> list[dict]:
        self._tick()
        return copy.deepcopy(self._fields)

    def get_projects(self) -> list[dict]:
        self._tick()
        return copy.deepcopy(self._projects)

    def get_statuses(self) -> list[dict]:
        self._tick()
        return copy.deepcopy(self._statuses)

    def search_issues(self, jql: str, start_at: int, max_results: int,
                      expand_changelog: bool) -> SearchPage:
        call = self._tick()
        effective = min(max_results, self._server_max_results)

        m = _PROJECT_RE.search(jql)
        project_key = m.group(1) if m else None
        m = _UPDATED_RE.search(jql)
        since = m.group(1) if m else None

        rows = [
            i for i in self._issues.values()
            if str(i["id"]) not in self._deleted
            and (project_key is None or i["fields"]["project"]["key"] == project_key)
            and (since is None or i["fields"]["updated"][:len(since)] >= since)
        ]
        rows.sort(key=lambda i: (i["fields"]["updated"], str(i["id"])))

        hook = self._page_hooks.pop(call, None)
        if hook is not None:
            hook(rows)
            rows.sort(key=lambda i: (i["fields"]["updated"], str(i["id"])))

        window = copy.deepcopy(rows[start_at:start_at + effective])
        for issue in window:
            issue.pop("_whitelisted", None)
            if expand_changelog:
                cl = issue.get("changelog") or {"total": 0, "histories": []}
                histories = cl.get("histories", [])
                issue["changelog"] = {
                    "startAt": 0,
                    "maxResults": self._inline_limit,
                    "total": cl.get("total", len(histories)),
                    "histories": histories[:self._inline_limit],
                }
            else:
                issue.pop("changelog", None)
        return SearchPage(start_at, effective, len(rows), window)

    def get_issue_changelog(self, issue_key: str, start_at: int) -> ChangelogPage:
        self._tick()
        issue = next(i for i in self._issues.values() if i["key"] == issue_key)
        cl = issue.get("changelog") or {"total": 0, "histories": []}
        histories = cl.get("histories", [])
        total = cl.get("total", len(histories))
        window = histories[start_at:start_at + self._inline_limit]
        return ChangelogPage(start_at, self._inline_limit, total,
                             copy.deepcopy(window))

    def get_issue(self, jira_issue_id: str, fields: list[str]) -> dict | None:
        self._tick()
        if jira_issue_id in self._deleted:
            return None
        issue = self._issues.get(jira_issue_id)
        if issue is None:
            return None
        return copy.deepcopy({
            "id": issue["id"],
            "key": issue["key"],
            "fields": {k: v for k, v in issue["fields"].items() if k in fields},
        })
```

- [ ] **Step 6: tests/conftest.py 작성 + 인라인 픽스처 제거**

```python
# tests/conftest.py
import os
from pathlib import Path

import pytest

from jira_dashboard.jira import parser
from jira_dashboard.jira.fake import FakeJiraClient

# 사내에서는 JIRA_FIXTURES=captured 로 같은 스위트를 실데이터에 돌린다 (spec §11.3)
_NAME = os.environ.get("JIRA_FIXTURES", "synthetic")


@pytest.fixture(scope="session")
def fixture_dir() -> Path:
    return Path(__file__).parent / "fixtures" / _NAME


@pytest.fixture
def fake_jira(fixture_dir) -> FakeJiraClient:
    return FakeJiraClient(fixture_dir)


@pytest.fixture
def field_index(fake_jira):
    return {fd.field_id: fd for fd in parser.parse_field_defs(fake_jira.get_fields())}


@pytest.fixture
def category_of(fake_jira):
    return {s["name"]: s["statusCategory"]["key"] for s in fake_jira.get_statuses()}


@pytest.fixture
def sample_issue(fake_jira):
    page = fake_jira.search_issues("project = PROJ ORDER BY updated ASC", 0, 100, True)
    return page.issues[0]
```

`tests/unit/conftest.py`를 삭제한다. T4의 파서 테스트가 이제 파일 픽스처를 쓴다.

- [ ] **Step 7: 테스트 통과 확인**

Run: `pytest tests/ -v`
Expected: T4 파서 + T5 Fake 전부 통과. 파서 테스트가 픽스처 교체 후에도 통과해야 한다 — 실패하면 픽스처가 spec §4.3 분기를 다 덮지 않는다는 뜻이다.

- [ ] **Step 8: Commit**

```bash
git add jira_dashboard/jira/protocol.py jira_dashboard/jira/fake.py tests/
git rm tests/unit/conftest.py
git commit -m "feat: JiraClient protocol and fake client with scenario injection"
```

---

### Task 6: 카탈로그 리포지토리 + `sync_catalog`

**첫 리포지토리다.** 여기서 T3의 정적 대조가 처음 실제 대상을 갖고, 파이프라인 스텁 테스트 패턴이 정해진다.

**Files:**
- Create: `jira_dashboard/db/repository/__init__.py`, `catalog.py`
- Create: `jira_dashboard/pipeline/__init__.py`, `sync_catalog.py`
- Create: `tests/stubs.py`, `tests/pipeline/test_sync_catalog.py`

**Interfaces:**
- Consumes: `SYSTEM_FIELD_MAP`/`SYNTHETIC_FIELDS`/`value_kind_of` (T4), `JiraClient` (T5)
- Produces:
  - `FieldChangeReport(value_kind_changed: list[str], key_changed_projects: list[int])`
  - `upsert_instance(conn, instance_key, base_url, auth_type, secret_ref) -> int`
  - `upsert_projects(conn, instance_id, projects) -> list[int]` — 키가 바뀐 project_id 목록
  - `project_id_by_jira_id(conn, instance_id) -> dict[str, int]`
  - `enabled_projects(conn, instance_id) -> list[tuple[int, str, str]]`
  - `upsert_fields(conn, instance_id, defs) -> list[str]` — value_kind가 바뀐 field_id 목록
  - `field_pk_by_field_id(conn, instance_id) -> dict[str, int]`
  - `storage_for(fd) -> tuple[str, str|None, str|None, str, str, str]`
  - `sync_catalog(conn, client, instance_id) -> FieldChangeReport`
  - `RecordingRepo` 스텁 패턴 (tests/stubs.py)

- [ ] **Step 1: 스텁 하네스 작성**

```python
# tests/stubs.py
"""리포지토리 함수를 기록용 스텁으로 바꿔치기한다.

DB가 없으므로 "무엇이 저장됐는가"는 검증할 수 없다. "무엇을 어떤 순서로
어떤 인자로 호출했는가"까지가 사외 검증의 한계다 (spec §11.3).
"""
from dataclasses import dataclass, field
from typing import Any


class Sentinel:
    """conn 자리에 넣는 더미. 실수로 SQL을 실행하면 AttributeError로 터진다."""

    def __repr__(self) -> str:
        return "<no-db>"


CONN = Sentinel()


@dataclass
class Recorder:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    returns: dict[str, Any] = field(default_factory=dict)

    def stub(self, name: str):
        """monkeypatch.setattr(module, name, recorder.stub(name)) 로 쓴다."""
        def _fn(*args, **kwargs):
            self.calls.append((name, {"args": args[1:], "kwargs": kwargs}))
            value = self.returns.get(name)
            return value(*args, **kwargs) if callable(value) else value
        return _fn

    def patch(self, monkeypatch, module, *names) -> None:
        for name in names:
            monkeypatch.setattr(module, name, self.stub(name))

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)

    def args_of(self, name: str) -> list[dict]:
        return [payload for n, payload in self.calls if n == name]

    def first(self, name: str) -> dict:
        return self.args_of(name)[0]

    def order_of(self, *names: str) -> list[int]:
        """지정한 이름들이 처음 등록된 위치. 순서 검증에 쓴다."""
        seen = self.names()
        return [seen.index(n) for n in names]
```

- [ ] **Step 2: 실패하는 테스트 작성**

```python
# tests/pipeline/test_sync_catalog.py
import pytest

from jira_dashboard.db.repository import catalog
from jira_dashboard.jira.models import FieldDef
from jira_dashboard.pipeline import sync_catalog as mod
from tests.stubs import CONN, Recorder


def _defs_by_id(recorder) -> dict[str, FieldDef]:
    payload = recorder.first("upsert_fields")
    defs = payload["args"][1] if len(payload["args"]) > 1 else payload["kwargs"]["defs"]
    return {fd.field_id: fd for fd in defs}


@pytest.fixture
def rec(monkeypatch):
    r = Recorder()
    r.returns["upsert_fields"] = lambda *a, **k: []
    r.returns["upsert_projects"] = lambda *a, **k: []
    r.patch(monkeypatch, mod, "upsert_fields", "upsert_projects")
    return r


def test_calls_fields_before_projects(rec, fake_jira):
    mod.sync_catalog(CONN, fake_jira, 1)
    assert rec.names() == ["upsert_fields", "upsert_projects"]


def test_synthetic_fields_are_appended(rec, fake_jira):
    """/field 응답에 없는 status_category와 first_done_at을 직접 넣는다 (spec 4.1)."""
    mod.sync_catalog(CONN, fake_jira, 1)
    defs = _defs_by_id(rec)
    assert "status_category" in defs
    assert "first_done_at" in defs
    assert defs["status_category"].is_custom is False


def test_report_carries_both_change_lists(monkeypatch, fake_jira):
    r = Recorder()
    r.returns["upsert_fields"] = lambda *a, **k: ["customfield_10002"]
    r.returns["upsert_projects"] = lambda *a, **k: [42]
    r.patch(monkeypatch, mod, "upsert_fields", "upsert_projects")
    report = mod.sync_catalog(CONN, fake_jira, 1)
    assert report.value_kind_changed == ["customfield_10002"]
    assert report.key_changed_projects == [42]


# --- storage_for 분류 (순수 함수, 완전 검증) ---

def _fd(field_id, schema_type, items=None):
    return FieldDef(field_id, "F", not field_id.isalpha(), schema_type, items, None)


def test_system_field_gets_column_storage():
    storage, column, label, kind, dim, msr = catalog.storage_for(_fd("status", "status"))
    assert (storage, column, kind, dim) == ("COLUMN", "status_name", "STR", "Y")


def test_custom_field_gets_eav_storage():
    storage, column, label, kind, dim, msr = catalog.storage_for(
        _fd("customfield_10001", "option")
    )
    assert (storage, column, label) == ("EAV", None, None)


def test_multi_value_system_field_goes_to_eav():
    """labels는 시스템 필드지만 다중값이라 고정 컬럼에 담을 수 없다 (spec 4.1)."""
    storage, column, label, kind, dim, msr = catalog.storage_for(
        _fd("labels", "array", "string")
    )
    assert (storage, column, kind) == ("EAV", None, "MULTI")


def test_assignee_gets_label_column():
    storage, column, label, kind, dim, msr = catalog.storage_for(_fd("assignee", "user"))
    assert (column, label) == ("assignee_user_key", "assignee_display_name")


def test_measure_fields_are_not_dimensions():
    storage, column, label, kind, dim, msr = catalog.storage_for(
        _fd("timespent", "number")
    )
    assert (kind, dim, msr) == ("NUM", "N", "Y")


def test_summary_is_column_but_not_dimension():
    storage, column, label, kind, dim, msr = catalog.storage_for(_fd("summary", "string"))
    assert (storage, dim) == ("COLUMN", "N")


def test_column_and_eav_invariant_holds_for_every_field(fake_jira):
    """ck_jira_field_col: COLUMN이면 컬럼명이 있고 EAV면 없다. DB 제약과 같은 규칙."""
    from jira_dashboard.jira.parser import parse_field_defs

    for fd in parse_field_defs(fake_jira.get_fields()):
        storage, column, _, _, _, _ = catalog.storage_for(fd)
        assert (storage == "COLUMN") == (column is not None), fd.field_id
```

- [ ] **Step 3: 테스트 실패 확인**

Run: `pytest tests/pipeline/test_sync_catalog.py -v`
Expected: FAIL — `ModuleNotFoundError: jira_dashboard.db.repository.catalog`

- [ ] **Step 4: catalog.py 구현**

```python
# jira_dashboard/db/repository/catalog.py
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
```

- [ ] **Step 5: sync_catalog.py 구현**

```python
# jira_dashboard/pipeline/sync_catalog.py
from jira_dashboard.db.repository.catalog import (
    FieldChangeReport, upsert_fields, upsert_projects,
)
from jira_dashboard.jira.fieldmap import SYNTHETIC_FIELDS
from jira_dashboard.jira.models import FieldDef
from jira_dashboard.jira.parser import parse_field_defs
from jira_dashboard.jira.protocol import JiraClient

_SYNTHETIC_SCHEMA = {"status_category": "string", "first_done_at": "datetime"}


def _synthetic_defs() -> list[FieldDef]:
    """/rest/api/2/field 에 없는 필드. 쿼리 API가 다른 필드와 똑같이 참조하려면 필요하다."""
    return [
        FieldDef(field_id=field_id, field_name=name, is_custom=False,
                 schema_type=_SYNTHETIC_SCHEMA[field_id],
                 schema_items=None, custom_type=None)
        for field_id, name in SYNTHETIC_FIELDS.items()
    ]


def sync_catalog(conn, client: JiraClient, instance_id: int) -> FieldChangeReport:
    defs = parse_field_defs(client.get_fields()) + _synthetic_defs()
    value_kind_changed = upsert_fields(conn, instance_id, defs)
    key_changed = upsert_projects(conn, instance_id, client.get_projects())
    return FieldChangeReport(value_kind_changed=value_kind_changed,
                            key_changed_projects=key_changed)
```

- [ ] **Step 6: 테스트 통과 확인**

Run: `pytest tests/pipeline/test_sync_catalog.py -v`
Expected: 10 passed

- [ ] **Step 7: 정적 대조가 이제 실제로 검사하는지 확인**

Run: `pytest tests/static/ -v`
Expected: 전부 통과. **일부러 컬럼명을 틀려 실패를 확인한다** — `_SELECT_ENABLED`의 `project_key`를 `project_keyy`로 바꾸고 돌리면 `test_every_referenced_column_exists`가 실패해야 한다. 확인 후 되돌린다.

- [ ] **Step 8: Commit**

```bash
git add jira_dashboard/db/repository/ jira_dashboard/pipeline/ tests/
git commit -m "feat: catalog repository and sync with storage_kind classification"
```

---

### Task 7: 이슈 리포지토리 + `sync_issues` (증분 수집)

가장 큰 태스크다. **spec §5.2의 적재 순서 6단계를 정확히 지킨다** — `TEST_ISSUE_RAW`가 `TEST_JIRA_ISSUE`를 FK로 참조하므로 raw를 먼저 쓸 수 없고, 해시 비교를 먼저 하려면 **쓰기가 아니라 읽기**를 먼저 해야 한다. DB가 없으므로 **순서를 스텁 호출 순서로 검증한다.**

**Files:**
- Create: `jira_dashboard/db/repository/issue.py`, `jira_dashboard/pipeline/sync_issues.py`
- Create: `tests/pipeline/test_sync_issues.py`

**Interfaces:**
- Consumes: `field_pk_by_field_id` (T6), `parse_issue` (T4), `JiraClient` (T5)
- Produces:
  - `ExistingIssue(issue_id: int, payload_hash: str | None)`
  - `load_existing(conn, instance_id, jira_issue_ids) -> dict[str, ExistingIssue]`
  - `next_issue_ids(conn, n) -> list[int]`
  - `upsert_issues(conn, rows) -> None`
  - `touch_synced_at(conn, issue_ids) -> None`
  - `upsert_raw(conn, table, rows) -> None` — `table` ∈ {`test_issue_raw`, `test_changelog_raw`}
  - `replace_field_values(conn, issue_id, values) -> None`
  - `gzip_json(obj) -> bytes`, `sha256_hex(data) -> str`
  - `SyncResult(fetched, upserted, skipped, parse_failures, max_updated, changed_issue_ids)`
  - `build_jql(project_key, since) -> str`, `next_watermark(max_updated, previous) -> datetime | None`
  - `sync_issues(conn, client, instance_id, project_id, project_key, since, *, page_size=100) -> SyncResult`
  - 상수 `OVERLAP = timedelta(minutes=5)`, `EPOCH`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/pipeline/test_sync_issues.py
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
    monkeypatch.setattr(CONN, "commit", lambda: None, raising=False)

    client = FakeJiraClient(fixture_dir, server_max_results=2)
    total = client.search_issues("project = PROJ", 0, 500, False).total
    result = mod.sync_issues(CONN, client, 1, 7, "PROJ", None, page_size=100)
    assert result.fetched == total


def test_overlap_window_appears_in_jql(rec, fake_jira):
    """겹침 구간이 살아있는지 사외에서 확인하는 방법. OVERLAP=0이면 실패해야 한다."""
    since = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
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
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/pipeline/test_sync_issues.py -v`
Expected: FAIL — `ModuleNotFoundError: jira_dashboard.pipeline.sync_issues`

- [ ] **Step 3: issue.py 리포지토리 구현**

SQL은 모듈 수준 상수로 둔다 — T3의 정적 대조가 읽을 수 있어야 한다.

```python
# jira_dashboard/db/repository/issue.py
import gzip
import hashlib
import json
from dataclasses import dataclass

import oracledb

_RAW_TABLES = {"test_issue_raw", "test_changelog_raw"}

_SELECT_EXISTING = """
SELECT i.jira_issue_id, i.issue_id, r.payload_hash
FROM   test_jira_issue i
LEFT   JOIN test_issue_raw r ON r.issue_id = i.issue_id
WHERE  i.instance_id = :instance_id AND i.jira_issue_id IN ({placeholders})
"""

_NEXT_IDS = """
SELECT test_seq_issue_id.NEXTVAL FROM dual CONNECT BY LEVEL <= :n
"""

_MERGE_ISSUE = """
MERGE INTO test_jira_issue t
USING (SELECT :instance_id AS instance_id,
              :jira_issue_id AS jira_issue_id FROM dual) s
ON (t.instance_id = s.instance_id AND t.jira_issue_id = s.jira_issue_id)
WHEN MATCHED THEN UPDATE SET
  t.project_id = :project_id, t.issue_key = :issue_key,
  t.issue_type_name = :issue_type_name, t.status_name = :status_name,
  t.status_category = :status_category, t.priority_name = :priority_name,
  t.resolution_name = :resolution_name,
  t.assignee_user_key = :assignee_user_key,
  t.assignee_display_name = :assignee_display_name,
  t.reporter_user_key = :reporter_user_key,
  t.reporter_display_name = :reporter_display_name,
  t.parent_key = :parent_key, t.summary = :summary,
  t.created_at = :created_at, t.updated_at = :updated_at,
  t.resolved_at = :resolved_at, t.due_date = :due_date,
  t.original_estimate_sec = :original_estimate_sec,
  t.remaining_estimate_sec = :remaining_estimate_sec,
  t.time_spent_sec = :time_spent_sec,
  t.synced_at = SYS_EXTRACT_UTC(SYSTIMESTAMP),
  t.deleted_at = NULL, t.delete_reason = NULL
WHEN NOT MATCHED THEN
  INSERT (issue_id, instance_id, project_id, jira_issue_id, issue_key,
          issue_type_name, status_name, status_category, priority_name,
          resolution_name, assignee_user_key, assignee_display_name,
          reporter_user_key, reporter_display_name, parent_key, summary,
          created_at, updated_at, resolved_at, due_date,
          original_estimate_sec, remaining_estimate_sec, time_spent_sec)
  VALUES (:issue_id, :instance_id, :project_id, :jira_issue_id, :issue_key,
          :issue_type_name, :status_name, :status_category, :priority_name,
          :resolution_name, :assignee_user_key, :assignee_display_name,
          :reporter_user_key, :reporter_display_name, :parent_key, :summary,
          :created_at, :updated_at, :resolved_at, :due_date,
          :original_estimate_sec, :remaining_estimate_sec, :time_spent_sec)
"""

_TOUCH_SYNCED = """
UPDATE test_jira_issue SET synced_at = SYS_EXTRACT_UTC(SYSTIMESTAMP)
WHERE  issue_id = :issue_id
"""

_MERGE_RAW = """
MERGE INTO {table} t
USING (SELECT :issue_id AS issue_id FROM dual) s
ON (t.issue_id = s.issue_id)
WHEN MATCHED THEN UPDATE SET t.payload = :payload,
     t.payload_hash = :payload_hash,
     t.fetched_at = SYS_EXTRACT_UTC(SYSTIMESTAMP)
WHEN NOT MATCHED THEN INSERT (issue_id, payload, payload_hash)
                      VALUES (:issue_id, :payload, :payload_hash)
"""

_DELETE_VALUES = "DELETE FROM test_issue_field_value WHERE issue_id = :issue_id"

_INSERT_VALUE = """
INSERT INTO test_issue_field_value
       (issue_id, field_pk, val_seq, val_str, val_num, val_date, val_id)
VALUES (:issue_id, :field_pk, :val_seq, :val_str, :val_num, :val_date, :val_id)
"""


@dataclass(frozen=True)
class ExistingIssue:
    issue_id: int
    payload_hash: str | None


def gzip_json(obj) -> bytes:
    return gzip.compress(
        json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
    )


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_existing(conn, instance_id: int,
                  jira_issue_ids: list[str]) -> dict[str, ExistingIssue]:
    """적재 ①단계. FK 때문에 raw를 먼저 쓸 수 없으므로 읽기를 먼저 한다 (spec §5.2)."""
    if not jira_issue_ids:
        return {}
    binds = {f"b{i}": v for i, v in enumerate(jira_issue_ids)}
    sql = _SELECT_EXISTING.format(
        placeholders=", ".join(f":{k}" for k in binds)
    )
    cur = conn.cursor()
    cur.execute(sql, instance_id=instance_id, **binds)
    return {j: ExistingIssue(iid, h) for j, iid, h in cur.fetchall()}


def next_issue_ids(conn, n: int) -> list[int]:
    """MERGE가 RETURNING을 못 쓰므로 시퀀스에서 미리 받는다 (spec §3.3.0)."""
    if n <= 0:
        return []
    cur = conn.cursor()
    cur.execute(_NEXT_IDS, n=n)
    return [r[0] for r in cur.fetchall()]


def upsert_issues(conn, rows: list[dict]) -> None:
    if not rows:
        return
    conn.cursor().executemany(_MERGE_ISSUE, rows, batcherrors=False)


def touch_synced_at(conn, issue_ids: list[int]) -> None:
    if not issue_ids:
        return
    conn.cursor().executemany(
        _TOUCH_SYNCED, [{"issue_id": i} for i in issue_ids], batcherrors=False
    )


def upsert_raw(conn, table: str, rows: list[dict]) -> None:
    if table not in _RAW_TABLES:        # 식별자는 화이트리스트에서만 온다
        raise ValueError(f"unknown raw table: {table}")
    if not rows:
        return
    cur = conn.cursor()
    cur.setinputsizes(payload=oracledb.DB_TYPE_BLOB)
    cur.executemany(_MERGE_RAW.format(table=table), rows, batcherrors=False)


def replace_field_values(conn, issue_id: int, values: list[dict]) -> None:
    """부분 갱신하지 않는다. 이슈 단위로 지우고 다시 넣는다 (spec §5.2)."""
    cur = conn.cursor()
    cur.execute(_DELETE_VALUES, issue_id=issue_id)
    if values:
        cur.executemany(_INSERT_VALUE, values, batcherrors=False)
```

`_MERGE_ISSUE`의 UPDATE 절에 `deleted_at = NULL, delete_reason = NULL`이 있는 것이 중요하다 — spec §5.6의 "MOVED_OUT 되살리기"가 이 한 줄로 동작한다.

- [ ] **Step 4: sync_issues.py 구현**

```python
# jira_dashboard/pipeline/sync_issues.py
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from jira_dashboard.db.repository import history as history_repo
from jira_dashboard.db.repository import issue as issue_repo
from jira_dashboard.db.repository.catalog import field_pk_by_field_id
from jira_dashboard.jira.parser import parse_field_defs, parse_issue
from jira_dashboard.jira.protocol import JiraClient

log = logging.getLogger(__name__)

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
OVERLAP = timedelta(minutes=5)


@dataclass
class SyncResult:
    fetched: int = 0
    upserted: int = 0
    skipped: int = 0
    parse_failures: int = 0
    max_updated: datetime | None = None
    changed_issue_ids: list[int] = field(default_factory=list)


def build_jql(project_key: str, since: datetime | None) -> str:
    start = (since or EPOCH).strftime("%Y-%m-%d %H:%M")
    return f'project = {project_key} AND updated >= "{start}" ORDER BY updated ASC'


def next_watermark(max_updated: datetime | None,
                   previous: datetime | None) -> datetime | None:
    """다음 시작점 = 이번 최대 updated - 5분 (의도적 중복 구간, spec §5.2)."""
    if max_updated is None:
        return previous
    return max_updated - OVERLAP


def _iter_pages(client: JiraClient, jql: str, page_size: int):
    start_at = 0
    while True:
        page = client.search_issues(jql, start_at, page_size, True)
        if not page.issues:
            return
        yield page
        # 요청값이 아니라 서버가 응답한 max_results로 전진한다 (A7)
        start_at += page.max_results
        if start_at >= page.total:
            return


def _full_changelog(client: JiraClient, raw_issue: dict) -> list[dict]:
    """인라인 상한을 넘는 이력을 보충한다 (A3). 오름차순 가정은 A4."""
    cl = raw_issue.get("changelog") or {}
    histories = list(cl.get("histories") or [])
    total = int(cl.get("total", len(histories)))
    start = len(histories)
    while start < total:
        page = client.get_issue_changelog(raw_issue["key"], start)
        if not page.histories:
            break
        histories.extend(page.histories)
        start += len(page.histories)
    return histories


def sync_issues(conn, client: JiraClient, instance_id: int, project_id: int,
                project_key: str, since: datetime | None,
                *, page_size: int = 100) -> SyncResult:
    field_index = {fd.field_id: fd for fd in parse_field_defs(client.get_fields())}
    category_of = {s["name"]: s["statusCategory"]["key"] for s in client.get_statuses()}
    field_pks = field_pk_by_field_id(conn, instance_id)

    result = SyncResult()
    jql = build_jql(project_key, since)

    for page in _iter_pages(client, jql, page_size):
        result.fetched += len(page.issues)
        jira_ids = [str(i["id"]) for i in page.issues]

        # ① 기존 상태를 먼저 읽는다
        existing = issue_repo.load_existing(conn, instance_id, jira_ids)

        # ② 해시로 분류
        pending, unchanged_ids = [], []
        for raw in page.issues:
            jid = str(raw["id"])
            cl = raw.get("changelog") or {}
            raw["changelog"] = {**cl, "histories": _full_changelog(client, raw)}
            payload = issue_repo.gzip_json(raw)
            digest = issue_repo.sha256_hex(payload)
            prior = existing.get(jid)
            if prior is not None and prior.payload_hash == digest:
                unchanged_ids.append(prior.issue_id)
                continue
            pending.append((raw, payload, digest, prior))

        issue_repo.touch_synced_at(conn, unchanged_ids)
        result.skipped += len(unchanged_ids)

        # 파싱을 먼저 해서 실패한 것을 걸러낸 뒤 채번한다 (spec §5.8)
        parsed_rows = []
        for raw, payload, digest, prior in pending:
            try:
                parsed = parse_issue(raw, field_index, category_of)
            except Exception:
                log.exception("failed to parse issue %s; skipping", raw.get("key"))
                result.parse_failures += 1
                continue
            parsed_rows.append((raw, payload, digest, prior, parsed))

        new_count = sum(1 for *_, prior, _ in parsed_rows if prior is None)
        fresh_ids = iter(issue_repo.next_issue_ids(conn, new_count))

        issue_rows, raw_rows, cl_raw_rows = [], [], []
        eav_by_issue: dict[int, list[dict]] = {}
        changelog_by_issue: dict[int, list] = {}

        for raw, payload, digest, prior, parsed in parsed_rows:
            issue_id = prior.issue_id if prior is not None else next(fresh_ids)

            issue_rows.append({
                "issue_id": issue_id, "instance_id": instance_id,
                "project_id": project_id, "jira_issue_id": parsed.jira_issue_id,
                "issue_key": parsed.issue_key,
                "issue_type_name": parsed.issue_type_name,
                "status_name": parsed.status_name,
                "status_category": parsed.status_category,
                "priority_name": parsed.priority_name,
                "resolution_name": parsed.resolution_name,
                "assignee_user_key": parsed.assignee_user_key,
                "assignee_display_name": parsed.assignee_display_name,
                "reporter_user_key": parsed.reporter_user_key,
                "reporter_display_name": parsed.reporter_display_name,
                "parent_key": parsed.parent_key, "summary": parsed.summary,
                "created_at": parsed.created_at, "updated_at": parsed.updated_at,
                "resolved_at": parsed.resolved_at, "due_date": parsed.due_date,
                "original_estimate_sec": parsed.original_estimate_sec,
                "remaining_estimate_sec": parsed.remaining_estimate_sec,
                "time_spent_sec": parsed.time_spent_sec,
            })
            raw_rows.append({"issue_id": issue_id, "payload": payload,
                             "payload_hash": digest})
            cl_payload = issue_repo.gzip_json(raw.get("changelog") or {})
            cl_raw_rows.append({
                "issue_id": issue_id, "payload": cl_payload,
                "payload_hash": issue_repo.sha256_hex(cl_payload),
            })
            eav_by_issue[issue_id] = [
                {"issue_id": issue_id, "field_pk": field_pks[v.field_id],
                 "val_seq": v.val_seq, "val_str": v.val_str, "val_num": v.val_num,
                 "val_date": v.val_date, "val_id": v.val_id}
                for v in parsed.custom_values if v.field_id in field_pks
            ]
            changelog_by_issue[issue_id] = list(parsed.changelog)

            if result.max_updated is None or parsed.updated_at > result.max_updated:
                result.max_updated = parsed.updated_at

        # ③ 이슈 → ④ raw → ⑤ EAV → ⑥ changelog. FK 때문에 순서를 바꿀 수 없다.
        issue_repo.upsert_issues(conn, issue_rows)
        issue_repo.upsert_raw(conn, "test_issue_raw", raw_rows)
        issue_repo.upsert_raw(conn, "test_changelog_raw", cl_raw_rows)
        for issue_id, values in eav_by_issue.items():
            issue_repo.replace_field_values(conn, issue_id, values)
        for issue_id, items in changelog_by_issue.items():
            history_repo.upsert_changelog(conn, issue_id, items, field_pks)

        result.upserted += len(issue_rows)
        result.changed_issue_ids.extend(r["issue_id"] for r in issue_rows)
        conn.commit()

    return result
```

- [ ] **Step 5: `history_repo.upsert_changelog` 구현 (T8에서 이어감)**

`sync_issues`가 changelog를 적재해야 하므로 여기서 먼저 만든다. T8은 구간 파생만 다룬다.

```python
# jira_dashboard/db/repository/history.py
_MERGE_CHANGELOG = """
MERGE INTO test_issue_changelog t
USING (SELECT :issue_id AS issue_id, :jira_history_id AS jira_history_id,
              :item_seq AS item_seq FROM dual) s
ON (t.issue_id = s.issue_id AND t.jira_history_id = s.jira_history_id
    AND t.item_seq = s.item_seq)
WHEN MATCHED THEN UPDATE SET t.field_pk = :field_pk, t.field_name = :field_name,
     t.from_id = :from_id, t.from_str = :from_str,
     t.to_id = :to_id, t.to_str = :to_str
WHEN NOT MATCHED THEN
  INSERT (issue_id, jira_history_id, item_seq, author_user_key,
          author_display_name, changed_at, field_pk, field_name,
          from_id, from_str, to_id, to_str)
  VALUES (:issue_id, :jira_history_id, :item_seq, :author_user_key,
          :author_display_name, :changed_at, :field_pk, :field_name,
          :from_id, :from_str, :to_id, :to_str)
"""


def upsert_changelog(conn, issue_id: int, items, field_pks: dict[str, int]) -> None:
    """A5: fieldId가 있으면 그것으로 매칭. 없거나 모호하면 field_pk를 NULL로 둔다."""
    if not items:
        return
    rows = [{
        "issue_id": issue_id, "jira_history_id": item.history_id,
        "item_seq": item.item_seq,
        "author_user_key": item.author_user_key,
        "author_display_name": item.author_display_name,
        "changed_at": item.changed_at,
        "field_pk": field_pks.get(item.field_id) if item.field_id else None,
        "field_name": item.field_name,
        "from_id": item.from_id, "from_str": item.from_str or None,
        "to_id": item.to_id, "to_str": item.to_str or None,
    } for item in items]
    conn.cursor().executemany(_MERGE_CHANGELOG, rows, batcherrors=False)
```

- [ ] **Step 6: 테스트 통과 확인**

Run: `pytest tests/pipeline/test_sync_issues.py -v`
Expected: 18 passed

- [ ] **Step 7: 겹침 구간이 살아있는지 확인**

`OVERLAP`을 `timedelta(0)`으로 바꾸고 `test_next_watermark_subtracts_overlap`과 `test_overlap_window_appears_in_jql`을 돌린다. **둘 다 실패해야 한다.** 실패하지 않으면 이 장치가 아무것도 하고 있지 않다는 뜻이다. 확인 후 5분으로 되돌린다.

- [ ] **Step 8: 정적 대조 통과 확인**

Run: `pytest tests/static/ -v`
Expected: 전부 통과. `_MERGE_ISSUE`의 컬럼 23개가 전부 DDL에 있는지 여기서 검증된다.

- [ ] **Step 9: Commit**

```bash
git add jira_dashboard/db/repository/issue.py jira_dashboard/db/repository/history.py jira_dashboard/pipeline/sync_issues.py tests/pipeline/test_sync_issues.py
git commit -m "feat: incremental issue sync with hash skip and FK-safe load order"
```

---

### Task 8: 이력 파생 — 구간 테이블 + `status_category` + `first_done_at`

**로직의 핵심이 순수 함수이므로 사외에서 완전히 검증되는 유일한 파이프라인 단계다.** 동시에 가장 틀리기 쉽다.

**Files:**
- Modify: `jira_dashboard/db/repository/history.py` (T7에서 만든 파일에 추가)
- Create: `jira_dashboard/pipeline/derive_history.py`
- Create: `tests/unit/test_derive_history.py`, `tests/pipeline/test_derive_history.py`

**Interfaces:**
- Consumes: `ChangelogItem`/`SENTINEL` (T4), `upsert_changelog` (T7)
- Produces:
  - `Interval(field_id, valid_from, valid_to, val_str, val_id)`
  - `build_intervals(created_at, current_values, changes, tracked_field_ids) -> list[Interval]`
  - `merge_categories(status_intervals, category_of) -> list[Interval]`
  - `STATUS_CATEGORY_FIELD = "status_category"`
  - `status_category_map(conn, instance_id) -> dict[str, str]`
  - `load_issue_states(conn, issue_ids) -> dict[int, dict]`
  - `load_changes(conn, issue_ids) -> dict[int, list[ChangelogItem]]`
  - `replace_history(conn, issue_id, rows) -> None`
  - `update_first_done_at(conn, issue_ids) -> int` (리포지토리)
  - `derive_history(conn, instance_id, issue_ids, *, batch=1000) -> int`
  - `update_first_done_at(conn, issue_ids, *, batch=1000) -> int` (파이프라인)

- [ ] **Step 1: 실패하는 단위 테스트 작성 — spec §5.3 경계 조건 전부**

```python
# tests/unit/test_derive_history.py
from datetime import datetime, timezone

from jira_dashboard.jira.models import SENTINEL, ChangelogItem
from jira_dashboard.pipeline.derive_history import (
    Interval, build_intervals, merge_categories,
)

CREATED = datetime(2026, 1, 1, tzinfo=timezone.utc)
TRACKED = {"status"}


def _c(at, frm, to, seq=0, field_id="status"):
    return ChangelogItem(
        history_id=f"h{at.day}{seq}", item_seq=seq,
        author_user_key="u", author_display_name="U", changed_at=at,
        field_name="status", field_id=field_id,
        from_id=None, from_str=frm, to_id=None, to_str=to,
    )


def _shape(intervals):
    return [(i.valid_from, i.valid_to, i.val_str) for i in intervals]


def test_no_changes_produces_single_sentinel_interval():
    out = build_intervals(CREATED, {"status": ("완료", None)}, [], TRACKED)
    assert _shape(out) == [(CREATED, SENTINEL, "완료")]


def test_first_interval_uses_from_str_of_first_change():
    t1 = datetime(2026, 1, 5, tzinfo=timezone.utc)
    out = build_intervals(CREATED, {"status": ("개발중", None)},
                          [_c(t1, "To Do", "개발중")], TRACKED)
    assert _shape(out) == [(CREATED, t1, "To Do"), (t1, SENTINEL, "개발중")]


def test_zero_length_interval_is_dropped():
    """같은 changed_at에 같은 필드가 두 번 바뀌면 앞의 것은 길이 0이다.
    ck_ifh_range가 valid_from < valid_to를 강제하므로 그냥 넣으면 DB가 거부한다."""
    t = datetime(2026, 1, 5, tzinfo=timezone.utc)
    out = build_intervals(
        CREATED, {"status": ("C", None)},
        [_c(t, "A", "B", seq=0), _c(t, "B", "C", seq=1)], TRACKED,
    )
    assert _shape(out) == [(CREATED, t, "A"), (t, SENTINEL, "C")]
    assert all(i.valid_from < i.valid_to for i in out)


def test_cleared_value_produces_null_interval():
    t = datetime(2026, 1, 5, tzinfo=timezone.utc)
    out = build_intervals(CREATED, {"status": (None, None)},
                          [_c(t, "To Do", None)], TRACKED)
    assert out[-1].val_str is None


def test_first_from_str_none_stays_none():
    """현재값으로 채우지 않는다 — 그 값은 나중에 설정된 것이다."""
    t = datetime(2026, 1, 5, tzinfo=timezone.utc)
    out = build_intervals(CREATED, {"status": ("완료", None)},
                          [_c(t, None, "완료")], TRACKED)
    assert out[0].val_str is None


def test_change_before_created_is_clamped():
    early = datetime(2025, 12, 1, tzinfo=timezone.utc)
    out = build_intervals(CREATED, {"status": ("B", None)},
                          [_c(early, "A", "B")], TRACKED)
    assert all(i.valid_from >= CREATED for i in out)
    assert out[0].valid_from == CREATED


def test_history_endpoint_mismatch_is_overwritten_by_current_value(caplog):
    """이력이 유실된 경우. 현재 시점 값이 틀리는 것이 최악이므로 현재값을 신뢰한다."""
    t = datetime(2026, 1, 5, tzinfo=timezone.utc)
    out = build_intervals(CREATED, {"status": ("완료", None)},
                          [_c(t, "To Do", "개발중")], TRACKED)
    assert out[-1].val_str == "완료"


def test_untracked_fields_are_ignored():
    t = datetime(2026, 1, 5, tzinfo=timezone.utc)
    out = build_intervals(CREATED, {"status": ("완료", None)},
                          [_c(t, "a", "b", field_id="summary")], TRACKED)
    assert {i.field_id for i in out} == {"status"}


def test_changes_without_field_id_are_skipped():
    """field_pk가 NULL인 changelog 행은 구간 테이블에 넣을 수 없다 (NOT NULL)."""
    t = datetime(2026, 1, 5, tzinfo=timezone.utc)
    out = build_intervals(CREATED, {"status": ("완료", None)},
                          [_c(t, "a", "b", field_id=None)], TRACKED)
    assert _shape(out) == [(CREATED, SENTINEL, "완료")]


def test_val_id_is_carried_through():
    t = datetime(2026, 1, 5, tzinfo=timezone.utc)
    change = ChangelogItem(
        history_id="h1", item_seq=0, author_user_key=None, author_display_name=None,
        changed_at=t, field_name="status", field_id="status",
        from_id="1", from_str="To Do", to_id="10", to_str="완료",
    )
    out = build_intervals(CREATED, {"status": ("완료", "10")}, [change], TRACKED)
    assert (out[0].val_id, out[1].val_id) == ("1", "10")


# --- status_category 병합 ---

def test_merge_categories_collapses_consecutive_same_category():
    """개발중 → 리뷰중은 둘 다 indeterminate이므로 구간 1개다."""
    t1 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 3, tzinfo=timezone.utc)
    t3 = datetime(2026, 1, 4, tzinfo=timezone.utc)
    status = build_intervals(
        CREATED, {"status": ("완료", None)},
        [_c(t1, "To Do", "개발중"), _c(t2, "개발중", "리뷰중"), _c(t3, "리뷰중", "완료")],
        TRACKED,
    )
    merged = merge_categories(status, {
        "To Do": "new", "개발중": "indeterminate",
        "리뷰중": "indeterminate", "완료": "done",
    })
    assert _shape(merged) == [
        (CREATED, t1, "new"),
        (t1, t3, "indeterminate"),
        (t3, SENTINEL, "done"),
    ]
    assert all(i.field_id == "status_category" for i in merged)


def test_merge_categories_maps_unknown_status_to_undefined():
    out = build_intervals(CREATED, {"status": ("Weird", None)}, [], TRACKED)
    assert merge_categories(out, {})[0].val_str == "undefined"


def test_merge_categories_ignores_non_status_fields():
    other = [Interval("customfield_1", CREATED, SENTINEL, "x", None)]
    assert merge_categories(other, {}) == []


def test_merge_categories_handles_reopened_issue():
    """완료 → 재오픈 → 완료. 구간이 셋으로 남아야 한다."""
    t1 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 3, tzinfo=timezone.utc)
    status = build_intervals(
        CREATED, {"status": ("완료", None)},
        [_c(t1, "To Do", "완료"), _c(t2, "완료", "개발중"),
         _c(datetime(2026, 1, 4, tzinfo=timezone.utc), "개발중", "완료")],
        TRACKED,
    )
    merged = merge_categories(status, {
        "To Do": "new", "개발중": "indeterminate", "완료": "done",
    })
    assert [i.val_str for i in merged] == ["new", "done", "indeterminate", "done"]
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/unit/test_derive_history.py -v`
Expected: FAIL — `ModuleNotFoundError: jira_dashboard.pipeline.derive_history`

- [ ] **Step 3: 순수 함수 구현**

```python
# jira_dashboard/pipeline/derive_history.py
import logging
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from jira_dashboard.db.repository import history as history_repo
from jira_dashboard.jira.models import SENTINEL, ChangelogItem

log = logging.getLogger(__name__)

STATUS_CATEGORY_FIELD = "status_category"


@dataclass(frozen=True)
class Interval:
    field_id: str
    valid_from: datetime
    valid_to: datetime
    val_str: str | None
    val_id: str | None


def build_intervals(
    created_at: datetime,
    current_values: Mapping[str, tuple[str | None, str | None]],
    changes: Sequence[ChangelogItem],
    tracked_field_ids: set[str],
) -> list[Interval]:
    """이슈 하나 안에서 완결된다. spec §5.3의 경계 조건을 전부 여기서 처리한다."""
    by_field: dict[str, list[ChangelogItem]] = defaultdict(list)
    for c in changes:
        if c.field_id and c.field_id in tracked_field_ids:
            by_field[c.field_id].append(c)

    fields = sorted((set(tracked_field_ids) & set(current_values)) | set(by_field))
    out: list[Interval] = []

    for field_id in fields:
        current_str, current_id = current_values.get(field_id, (None, None))
        items = sorted(by_field.get(field_id, []),
                       key=lambda c: (c.changed_at, c.item_seq))

        if not items:
            out.append(Interval(field_id, created_at, SENTINEL, current_str, current_id))
            continue

        # (시각, 값, 값id) 경계 목록. 첫 구간의 값은 첫 변경의 from_str이다.
        boundaries: list[tuple[datetime, str | None, str | None]] = [
            (created_at, items[0].from_str, items[0].from_id)
        ]
        for item in items:
            stamp = max(item.changed_at, created_at)   # 생성보다 이른 변경은 clamp
            if boundaries[-1][0] == stamp:
                boundaries[-1] = (stamp, item.to_str, item.to_id)  # 길이 0 구간 제거
            else:
                boundaries.append((stamp, item.to_str, item.to_id))

        # 이력 종점 != 현재값이면 이력이 유실된 것이다. 현재값을 신뢰한다.
        if boundaries[-1][1] != current_str:
            log.warning(
                "history endpoint mismatch on %s: history=%r current=%r",
                field_id, boundaries[-1][1], current_str,
            )
            boundaries[-1] = (boundaries[-1][0], current_str, current_id)

        for idx, (start, val_str, val_id) in enumerate(boundaries):
            end = boundaries[idx + 1][0] if idx + 1 < len(boundaries) else SENTINEL
            if start < end:
                out.append(Interval(field_id, start, end, val_str, val_id))
    return out


def merge_categories(
    status_intervals: Sequence[Interval],
    category_of: Mapping[str, str],
) -> list[Interval]:
    """상태명 구간 → 카테고리 구간. 연속한 같은 카테고리는 하나로 합친다.

    이게 없으면 인스턴스마다 상태명이 달라 과거 시점 교차 분석이 불가능하다 (spec §6.6).
    """
    ordered = sorted(
        (i for i in status_intervals if i.field_id == "status"),
        key=lambda i: i.valid_from,
    )
    merged: list[Interval] = []
    for interval in ordered:
        category = category_of.get(interval.val_str or "", "undefined")
        if merged and merged[-1].val_str == category:
            merged[-1] = Interval(
                STATUS_CATEGORY_FIELD, merged[-1].valid_from,
                interval.valid_to, category, None,
            )
        else:
            merged.append(Interval(
                STATUS_CATEGORY_FIELD, interval.valid_from,
                interval.valid_to, category, None,
            ))
    return merged
```

- [ ] **Step 4: 단위 테스트 통과 확인**

Run: `pytest tests/unit/test_derive_history.py -v`
Expected: 14 passed

- [ ] **Step 5: Commit (순수 함수부터)**

```bash
git add jira_dashboard/pipeline/derive_history.py tests/unit/test_derive_history.py
git commit -m "feat: SCD Type-2 interval derivation with boundary-condition handling"
```

- [ ] **Step 6: 실패하는 파이프라인 테스트 작성**

```python
# tests/pipeline/test_derive_history.py
from datetime import datetime, timezone

import pytest

from jira_dashboard.jira.models import SENTINEL, ChangelogItem
from jira_dashboard.pipeline import derive_history as mod
from tests.stubs import CONN, Recorder

CREATED = datetime(2026, 1, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 1, 5, tzinfo=timezone.utc)
FIELD_PKS = {"status": 1, "status_category": 2}


@pytest.fixture
def rec(monkeypatch):
    r = Recorder()
    r.returns["status_category_map"] = lambda *a, **k: {
        "To Do": "new", "개발중": "indeterminate", "완료": "done",
    }
    r.returns["load_issue_states"] = lambda conn, ids: {
        i: {"created_at": CREATED, "current_values": {"status": ("완료", "10")}}
        for i in ids
    }
    r.returns["load_changes"] = lambda conn, ids: {
        ids[0]: [ChangelogItem("h1", 0, None, None, T1, "status", "status",
                               None, "To Do", None, "완료")]
    }
    r.returns["update_first_done_at"] = lambda *a, **k: 1
    r.patch(monkeypatch, mod.history_repo,
            "status_category_map", "load_issue_states", "load_changes",
            "replace_history", "update_first_done_at")
    monkeypatch.setattr(mod, "_tracked_fields", lambda conn, i: FIELD_PKS)
    monkeypatch.setattr(CONN, "commit", lambda: None, raising=False)
    return r


def _rows(rec, issue_id):
    for call in rec.args_of("replace_history"):
        if call["args"][0] == issue_id:
            return call["args"][1]
    raise AssertionError(f"replace_history not called for {issue_id}")


def test_writes_both_status_and_category_intervals(rec):
    """status 구간과 status_category 구간이 함께 생성되어야 한다 (spec 5.3)."""
    mod.derive_history(CONN, 1, [500])
    field_pks = {row["field_pk"] for row in _rows(rec, 500)}
    assert field_pks == {1, 2}


def test_replaces_instead_of_appending(rec):
    """이슈 단위로 DELETE 후 재생성한다. 두 번 돌려도 같은 결과여야 한다."""
    mod.derive_history(CONN, 1, [500])
    first = _rows(rec, 500)
    rec.calls.clear()
    mod.derive_history(CONN, 1, [500])
    assert _rows(rec, 500) == first


def test_sentinel_is_used_for_the_open_interval(rec):
    mod.derive_history(CONN, 1, [500])
    assert any(row["valid_to"] == SENTINEL for row in _rows(rec, 500))


def test_all_intervals_have_positive_length(rec):
    """ck_ifh_range를 위반하는 행을 만들면 사내에서 적재가 실패한다."""
    mod.derive_history(CONN, 1, [500])
    for row in _rows(rec, 500):
        assert row["valid_from"] < row["valid_to"]


def test_untracked_fields_are_not_written(rec, monkeypatch):
    monkeypatch.setattr(mod, "_tracked_fields", lambda conn, i: {"status": 1})
    mod.derive_history(CONN, 1, [500])
    assert {row["field_pk"] for row in _rows(rec, 500)} == {1}


def test_commits_per_batch(rec, monkeypatch):
    """전체 재수집 시 100만 이슈를 한 트랜잭션에 넣으면 UNDO가 터진다 (spec 5.3)."""
    commits = []
    monkeypatch.setattr(CONN, "commit", lambda: commits.append(1), raising=False)
    mod.derive_history(CONN, 1, list(range(500, 2600)), batch=1000)
    assert len(commits) == 3


def test_empty_issue_list_is_a_noop(rec):
    assert mod.derive_history(CONN, 1, []) == 0
    assert rec.names() == []


def test_first_done_at_batches(rec, monkeypatch):
    commits = []
    monkeypatch.setattr(CONN, "commit", lambda: commits.append(1), raising=False)
    mod.update_first_done_at(CONN, list(range(1, 2501)), batch=1000)
    assert rec.count("update_first_done_at") == 3
```

- [ ] **Step 7: 적재 함수 구현**

```python
# jira_dashboard/pipeline/derive_history.py 에 추가

def _tracked_fields(conn, instance_id: int) -> dict[str, int]:
    """is_dimension='Y'인 필드만 추적한다. summary 이력까지 담으면 테이블만 부푼다."""
    return history_repo.dimension_field_pks(conn, instance_id)


def derive_history(conn, instance_id: int, issue_ids: list[int],
                   *, batch: int = 1000) -> int:
    """변경된 이슈만 DELETE 후 재생성. 이슈 단위로 닫혀 있어 중간 커밋이 안전하다."""
    if not issue_ids:
        return 0
    field_pks = _tracked_fields(conn, instance_id)
    tracked = set(field_pks)
    category_of = history_repo.status_category_map(conn, instance_id)
    written = 0

    for start in range(0, len(issue_ids), batch):
        chunk = issue_ids[start:start + batch]
        states = history_repo.load_issue_states(conn, chunk)
        changes = history_repo.load_changes(conn, chunk)
        for issue_id in chunk:
            state = states.get(issue_id)
            if state is None:
                continue
            intervals = build_intervals(
                state["created_at"], state["current_values"],
                changes.get(issue_id, []), tracked,
            )
            intervals = intervals + merge_categories(intervals, category_of)
            rows = [
                {"issue_id": issue_id, "field_pk": field_pks[i.field_id],
                 "valid_from": i.valid_from, "valid_to": i.valid_to,
                 "val_str": i.val_str, "val_id": i.val_id}
                for i in intervals if i.field_id in field_pks
            ]
            history_repo.replace_history(conn, issue_id, rows)
            written += len(rows)
        conn.commit()
    return written


def update_first_done_at(conn, issue_ids: list[int], *, batch: int = 1000) -> int:
    """status_category 구간에서 'done'인 첫 구간의 valid_from. 재오픈 이슈는 MIN 유지."""
    if not issue_ids:
        return 0
    total = 0
    for start in range(0, len(issue_ids), batch):
        total += history_repo.update_first_done_at(conn, issue_ids[start:start + batch])
        conn.commit()
    return total
```

```python
# jira_dashboard/db/repository/history.py 에 추가
from jira_dashboard.jira.models import ChangelogItem

_SELECT_DIMENSION_FIELDS = """
SELECT field_id, field_pk FROM test_jira_field
WHERE  instance_id = :instance_id AND is_dimension = 'Y'
"""

_SELECT_STATUS_CATEGORIES = """
SELECT DISTINCT status_name, status_category FROM test_jira_issue
WHERE  instance_id = :instance_id AND status_name IS NOT NULL
"""

_SELECT_ISSUE_STATES = """
SELECT issue_id, created_at, status_name FROM test_jira_issue
WHERE  issue_id IN ({placeholders})
"""

_SELECT_CHANGES = """
SELECT c.issue_id, c.jira_history_id, c.item_seq, c.changed_at,
       f.field_id, c.field_name, c.from_id, c.from_str, c.to_id, c.to_str
FROM   test_issue_changelog c
LEFT   JOIN test_jira_field f ON f.field_pk = c.field_pk
WHERE  c.issue_id IN ({placeholders})
ORDER  BY c.issue_id, c.changed_at, c.item_seq
"""

_DELETE_HISTORY = "DELETE FROM test_issue_field_history WHERE issue_id = :issue_id"

_INSERT_HISTORY = """
INSERT INTO test_issue_field_history
       (issue_id, field_pk, valid_from, valid_to, val_str, val_id)
VALUES (:issue_id, :field_pk, :valid_from, :valid_to, :val_str, :val_id)
"""

_MERGE_FIRST_DONE = """
MERGE INTO test_jira_issue t
USING (
  SELECT h.issue_id, MIN(h.valid_from) AS first_done_at
  FROM   test_issue_field_history h
  JOIN   test_jira_field f ON f.field_pk = h.field_pk
                          AND f.field_id = 'status_category'
  WHERE  h.val_str = 'done' AND h.issue_id IN ({placeholders})
  GROUP  BY h.issue_id
) s ON (t.issue_id = s.issue_id)
WHEN MATCHED THEN UPDATE SET t.first_done_at = s.first_done_at
"""


def _binds(issue_ids: list[int]) -> tuple[str, dict]:
    binds = {f"b{i}": v for i, v in enumerate(issue_ids)}
    return ", ".join(f":{k}" for k in binds), binds


def dimension_field_pks(conn, instance_id: int) -> dict[str, int]:
    cur = conn.cursor()
    cur.execute(_SELECT_DIMENSION_FIELDS, instance_id=instance_id)
    return {f: pk for f, pk in cur.fetchall()}


def status_category_map(conn, instance_id: int) -> dict[str, str]:
    """이미 적재된 이슈에서 상태명 → 카테고리 대응을 만든다.

    /rest/api/2/status 를 다시 부르지 않아도 되게 DB에서 뽑는다. 아직 본 적 없는
    상태는 merge_categories에서 'undefined'로 떨어진다.
    """
    cur = conn.cursor()
    cur.execute(_SELECT_STATUS_CATEGORIES, instance_id=instance_id)
    return {name: cat for name, cat in cur.fetchall()}


def load_issue_states(conn, issue_ids: list[int]) -> dict[int, dict]:
    placeholders, binds = _binds(issue_ids)
    cur = conn.cursor()
    cur.execute(_SELECT_ISSUE_STATES.format(placeholders=placeholders), **binds)
    return {
        iid: {"created_at": created, "current_values": {"status": (status, None)}}
        for iid, created, status in cur.fetchall()
    }


def load_changes(conn, issue_ids: list[int]) -> dict[int, list[ChangelogItem]]:
    placeholders, binds = _binds(issue_ids)
    cur = conn.cursor()
    cur.execute(_SELECT_CHANGES.format(placeholders=placeholders), **binds)
    out: dict[int, list[ChangelogItem]] = {}
    for (iid, hid, seq, at, field_id, field_name,
         from_id, from_str, to_id, to_str) in cur.fetchall():
        out.setdefault(iid, []).append(ChangelogItem(
            history_id=hid, item_seq=seq, author_user_key=None,
            author_display_name=None, changed_at=at, field_name=field_name,
            field_id=field_id, from_id=from_id, from_str=from_str,
            to_id=to_id, to_str=to_str,
        ))
    return out


def replace_history(conn, issue_id: int, rows: list[dict]) -> None:
    cur = conn.cursor()
    cur.execute(_DELETE_HISTORY, issue_id=issue_id)
    if rows:
        cur.executemany(_INSERT_HISTORY, rows, batcherrors=False)


def update_first_done_at(conn, issue_ids: list[int]) -> int:
    placeholders, binds = _binds(issue_ids)
    cur = conn.cursor()
    cur.execute(_MERGE_FIRST_DONE.format(placeholders=placeholders), **binds)
    return cur.rowcount
```

- [ ] **Step 8: 테스트 통과 확인**

Run: `pytest tests/ -v`
Expected: 전부 통과. 정적 대조가 `history.py`의 새 SQL 7개를 함께 검증한다.

- [ ] **Step 9: Commit**

```bash
git add jira_dashboard/db/repository/history.py jira_dashboard/pipeline/derive_history.py tests/pipeline/test_derive_history.py
git commit -m "feat: derive status and status_category interval history with first_done_at"
```

---

### Task 9: 필드 프로파일링 + 삭제 감지

**Files:**
- Create: `jira_dashboard/pipeline/profile_fields.py`, `jira_dashboard/pipeline/detect_deleted.py`
- Create: `tests/pipeline/test_profile_fields.py`, `tests/pipeline/test_detect_deleted.py`

**Interfaces:**
- Consumes: `enabled_projects`/`project_id_by_jira_id` (T6), `JiraClient.get_issue` (T5)
- Produces:
  - `COLUMN_FIELDS: list[tuple[str, str]]` — 프로파일링 대상 (field_id, column_name)
  - `profile_fields(conn, instance_id) -> int`
  - `axis_candidates(conn, project_id) -> list[tuple[str, str, int]]`
  - `DeleteVerdict(jira_issue_id, issue_id, reason)` — reason ∈ {`DELETED`, `MOVED_OUT`, `MOVED_IN`}
  - `live_issue_ids(client, project_key) -> set[str]`
  - `classify(raw, whitelist) -> str`
  - `detect_deleted(conn, client, instance_id) -> list[DeleteVerdict]`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/pipeline/test_detect_deleted.py
import pytest

from jira_dashboard.pipeline import detect_deleted as mod
from tests.stubs import CONN, Recorder

WHITELIST = {"10000": 7, "10001": 8}


def test_missing_and_gone_is_deleted():
    assert mod.classify(None, WHITELIST) == "DELETED"


def test_missing_but_moved_inside_whitelist_is_moved_in():
    raw = {"key": "OTHER-9", "fields": {"project": {"id": "10001"}}}
    assert mod.classify(raw, WHITELIST) == "MOVED_IN"


def test_missing_and_moved_outside_whitelist_is_moved_out():
    """되살릴 수 있어야 하므로 DELETED와 구분한다 (spec 3.3.4)."""
    raw = {"key": "ARCHIVE-1", "fields": {"project": {"id": "99999"}}}
    assert mod.classify(raw, WHITELIST) == "MOVED_OUT"


def test_live_ids_advance_by_response_max_results(fake_jira):
    """A7: 서버가 maxResults를 줄일 수 있으므로 응답값으로 페이징한다."""
    ids = mod.live_issue_ids(fake_jira, "PROJ")
    total = fake_jira.search_issues("project = PROJ", 0, 1000, False).total
    assert len(ids) == total


def test_live_ids_with_shrunk_pages(fixture_dir):
    from jira_dashboard.jira.fake import FakeJiraClient

    client = FakeJiraClient(fixture_dir, server_max_results=2)
    ids = mod.live_issue_ids(client, "PROJ")
    total = client.search_issues("project = PROJ", 0, 1000, False).total
    assert len(ids) == total


def test_deleted_and_moved_out_are_marked(monkeypatch, fake_jira):
    r = Recorder()
    r.returns["enabled_projects"] = lambda conn, i: [(7, "10000", "PROJ")]
    r.returns["project_id_by_jira_id"] = lambda conn, i: WHITELIST
    r.returns["load_undeleted"] = lambda conn, p: {"1001": 501, "1002": 502}
    r.patch(monkeypatch, mod, "enabled_projects", "project_id_by_jira_id")
    monkeypatch.setattr(mod, "load_undeleted", r.stub("load_undeleted"))
    monkeypatch.setattr(mod, "mark_deleted", r.stub("mark_deleted"))
    monkeypatch.setattr(mod, "relocate_issue", r.stub("relocate_issue"))
    monkeypatch.setattr(CONN, "commit", lambda: None, raising=False)
    monkeypatch.setattr(mod, "live_issue_ids", lambda c, k: set())

    calls = {"1001": None,
             "1002": {"key": "OTHER-3", "fields": {"project": {"id": "10001"}}}}
    monkeypatch.setattr(fake_jira, "get_issue", lambda jid, fields: calls[jid])

    verdicts = mod.detect_deleted(CONN, fake_jira, 1)
    by_id = {v.jira_issue_id: v.reason for v in verdicts}
    assert by_id == {"1001": "DELETED", "1002": "MOVED_IN"}
    assert r.count("relocate_issue") == 1
    marked = r.first("mark_deleted")["args"][0]
    assert [m["reason"] for m in marked] == ["DELETED"]
```

```python
# tests/pipeline/test_profile_fields.py
from jira_dashboard.jira.fieldmap import SYSTEM_FIELD_MAP
from jira_dashboard.pipeline import profile_fields as mod


def test_column_fields_cover_every_system_field():
    assert {f for f, _ in mod.COLUMN_FIELDS} == set(SYSTEM_FIELD_MAP)


def test_column_names_come_from_the_map_only():
    """식별자를 SQL에 조립하므로, 출처가 화이트리스트임이 보장되어야 한다."""
    allowed = {spec.column_name for spec in SYSTEM_FIELD_MAP.values()}
    assert {c for _, c in mod.COLUMN_FIELDS} <= allowed


def test_column_names_are_safe_identifiers():
    import re
    for _, column in mod.COLUMN_FIELDS:
        assert re.fullmatch(r"[a-z_][a-z0-9_]*", column), column


def test_eav_profile_sql_resets_before_merge():
    """값을 비운 필드의 옛 카운트가 남으면 축 후보에 계속 뜬다 (spec 5.5)."""
    assert "issue_count = 0" in mod.RESET_COUNTS
    assert "MERGE INTO test_jira_project_field" in mod.MERGE_EAV_COUNTS


def test_eav_profile_is_a_single_group_by():
    """필드마다 COUNT를 돌리면 15,000 쿼리가 된다 (spec 5.5)."""
    assert mod.MERGE_EAV_COUNTS.upper().count("SELECT") <= 2
    assert "GROUP  BY" in mod.MERGE_EAV_COUNTS or "GROUP BY" in mod.MERGE_EAV_COUNTS
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/pipeline/test_profile_fields.py tests/pipeline/test_detect_deleted.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: profile_fields.py 구현**

```python
# jira_dashboard/pipeline/profile_fields.py
import logging

from jira_dashboard.jira.fieldmap import SYSTEM_FIELD_MAP

log = logging.getLogger(__name__)

# 컬럼명은 SYSTEM_FIELD_MAP에서만 온다 — SQL 조립이 안전한 이유
COLUMN_FIELDS: list[tuple[str, str]] = [
    (field_id, spec.column_name) for field_id, spec in SYSTEM_FIELD_MAP.items()
]

RESET_COUNTS = """
UPDATE test_jira_project_field SET issue_count = 0, distinct_value_count = 0
WHERE  project_id IN (SELECT project_id FROM test_jira_project
                      WHERE instance_id = :instance_id)
"""

MERGE_EAV_COUNTS = """
MERGE INTO test_jira_project_field t
USING (
  SELECT i.project_id, v.field_pk,
         COUNT(DISTINCT v.issue_id) AS issue_count,
         COUNT(DISTINCT COALESCE(v.val_str, TO_CHAR(v.val_num),
                                 TO_CHAR(v.val_date))) AS distinct_value_count
  FROM   test_issue_field_value v
  JOIN   test_jira_issue i ON i.issue_id = v.issue_id
  WHERE  i.instance_id = :instance_id AND i.deleted_at IS NULL
  GROUP  BY i.project_id, v.field_pk
) s ON (t.project_id = s.project_id AND t.field_pk = s.field_pk)
WHEN MATCHED THEN UPDATE SET
  t.issue_count = s.issue_count,
  t.distinct_value_count = s.distinct_value_count,
  t.last_profiled_at = SYS_EXTRACT_UTC(SYSTIMESTAMP)
WHEN NOT MATCHED THEN
  INSERT (project_id, field_pk, issue_count, distinct_value_count, last_profiled_at)
  VALUES (s.project_id, s.field_pk, s.issue_count, s.distinct_value_count,
          SYS_EXTRACT_UTC(SYSTIMESTAMP))
"""

MERGE_COLUMN_COUNTS = """
MERGE INTO test_jira_project_field t
USING (SELECT :project_id AS project_id, :field_pk AS field_pk FROM dual) s
ON (t.project_id = s.project_id AND t.field_pk = s.field_pk)
WHEN MATCHED THEN UPDATE SET t.issue_count = :issue_count,
     t.distinct_value_count = :distinct_value_count,
     t.last_profiled_at = SYS_EXTRACT_UTC(SYSTIMESTAMP)
WHEN NOT MATCHED THEN
  INSERT (project_id, field_pk, issue_count, distinct_value_count, last_profiled_at)
  VALUES (:project_id, :field_pk, :issue_count, :distinct_value_count,
          SYS_EXTRACT_UTC(SYSTIMESTAMP))
"""

SELECT_FIELD_PKS = """
SELECT field_id, field_pk FROM test_jira_field WHERE instance_id = :instance_id
"""

SELECT_AXIS_CANDIDATES = """
SELECT f.field_id, f.field_name, pf.issue_count
FROM   test_jira_project_field pf
JOIN   test_jira_field f ON f.field_pk = pf.field_pk
WHERE  pf.project_id = :project_id AND f.is_dimension = 'Y'
ORDER  BY pf.issue_count DESC, f.field_name
"""


def _column_scan_sql() -> str:
    counts = ",\n       ".join(
        f"COUNT({column}) AS c_{i}" for i, (_, column) in enumerate(COLUMN_FIELDS)
    )
    distincts = ",\n       ".join(
        f"COUNT(DISTINCT {column}) AS d_{i}"
        for i, (_, column) in enumerate(COLUMN_FIELDS)
    )
    return (
        f"SELECT project_id,\n       {counts},\n       {distincts}\n"
        "FROM   test_jira_issue\n"
        "WHERE  instance_id = :instance_id AND deleted_at IS NULL\n"
        "GROUP  BY project_id"
    )


def profile_fields(conn, instance_id: int) -> int:
    cur = conn.cursor()
    # ① 옛 카운트를 0으로. 안 하면 값을 비운 필드가 계속 축 후보에 뜬다.
    cur.execute(RESET_COUNTS, instance_id=instance_id)
    # ② EAV 전체를 한 번만 훑는다
    cur.execute(MERGE_EAV_COUNTS, instance_id=instance_id)
    updated = cur.rowcount

    # ③ 고정 컬럼은 JIRA_ISSUE 1회 스캔으로 전부 계산
    cur.execute(_column_scan_sql(), instance_id=instance_id)
    rows = cur.fetchall()
    cur.execute(SELECT_FIELD_PKS, instance_id=instance_id)
    field_pks = dict(cur.fetchall())

    n = len(COLUMN_FIELDS)
    payload = []
    for row in rows:
        project_id = row[0]
        for idx, (field_id, _) in enumerate(COLUMN_FIELDS):
            field_pk = field_pks.get(field_id)
            if field_pk is None:
                continue
            payload.append({
                "project_id": project_id, "field_pk": field_pk,
                "issue_count": row[1 + idx],
                "distinct_value_count": row[1 + n + idx],
            })
    if payload:
        cur.executemany(MERGE_COLUMN_COUNTS, payload, batcherrors=False)
        updated += len(payload)

    for field_id in ("timespent", "timeoriginalestimate", "timeestimate"):
        field_pk = field_pks.get(field_id)
        if field_pk is None:
            continue
        if all(p["issue_count"] == 0 for p in payload if p["field_pk"] == field_pk):
            log.info("field %s has no values — Time Tracking may be disabled", field_id)

    conn.commit()
    return updated


def axis_candidates(conn, project_id: int) -> list[tuple[str, str, int]]:
    cur = conn.cursor()
    cur.execute(SELECT_AXIS_CANDIDATES, project_id=project_id)
    return list(cur.fetchall())
```

- [ ] **Step 4: detect_deleted.py 구현**

```python
# jira_dashboard/pipeline/detect_deleted.py
import logging
from dataclasses import dataclass

from jira_dashboard.db.repository.catalog import enabled_projects, project_id_by_jira_id
from jira_dashboard.jira.protocol import JiraClient

log = logging.getLogger(__name__)

SELECT_UNDELETED = """
SELECT jira_issue_id, issue_id FROM test_jira_issue
WHERE  project_id = :project_id AND deleted_at IS NULL
"""

MARK_DELETED = """
UPDATE test_jira_issue
SET    deleted_at = SYS_EXTRACT_UTC(SYSTIMESTAMP), delete_reason = :reason
WHERE  issue_id = :issue_id
"""

RELOCATE_ISSUE = """
UPDATE test_jira_issue
SET    project_id = :project_id, issue_key = :issue_key,
       deleted_at = NULL, delete_reason = NULL
WHERE  issue_id = :issue_id
"""


@dataclass(frozen=True)
class DeleteVerdict:
    jira_issue_id: str
    issue_id: int
    reason: str            # DELETED | MOVED_OUT | MOVED_IN


def live_issue_ids(client: JiraClient, project_key: str) -> set[str]:
    """fields=id 로 전체 id만 가볍게 훑는다. maxResults는 응답값을 믿는다 (A7)."""
    seen, start_at = set(), 0
    while True:
        page = client.search_issues(
            f"project = {project_key} ORDER BY updated ASC", start_at, 1000, False
        )
        if not page.issues:
            break
        seen.update(str(i["id"]) for i in page.issues)
        start_at += page.max_results
        if start_at >= page.total:
            break
    return seen


def classify(raw: dict | None, whitelist: dict[str, int]) -> str:
    """후보를 바로 지우지 않는다. 삭제와 이동은 대응이 다르다 (spec §3.3.4)."""
    if raw is None:
        return "DELETED"
    project_jira_id = str((raw.get("fields", {}).get("project") or {}).get("id", ""))
    return "MOVED_IN" if project_jira_id in whitelist else "MOVED_OUT"


def load_undeleted(conn, project_id: int) -> dict[str, int]:
    cur = conn.cursor()
    cur.execute(SELECT_UNDELETED, project_id=project_id)
    return {jid: iid for jid, iid in cur.fetchall()}


def mark_deleted(conn, rows: list[dict]) -> None:
    if rows:
        conn.cursor().executemany(MARK_DELETED, rows, batcherrors=False)


def relocate_issue(conn, issue_id: int, project_id: int, issue_key: str) -> None:
    conn.cursor().execute(RELOCATE_ISSUE, issue_id=issue_id,
                          project_id=project_id, issue_key=issue_key)


def detect_deleted(conn, client: JiraClient, instance_id: int) -> list[DeleteVerdict]:
    whitelist = project_id_by_jira_id(conn, instance_id)
    verdicts: list[DeleteVerdict] = []

    for project_id, _, project_key in enabled_projects(conn, instance_id):
        live = live_issue_ids(client, project_key)
        for jira_issue_id, issue_id in load_undeleted(conn, project_id).items():
            if jira_issue_id in live:
                continue
            raw = client.get_issue(jira_issue_id, ["project"])
            reason = classify(raw, whitelist)
            verdicts.append(DeleteVerdict(jira_issue_id, issue_id, reason))
            if reason == "MOVED_IN":
                # 이동 직후 양쪽 워터마크 틈에 빠진 이슈. 여기서 잡지 않으면
                # Jira에는 있는데 대시보드에서 사라진다 (spec §5.6)
                target = str((raw["fields"]["project"]).get("id"))
                relocate_issue(conn, issue_id, whitelist[target], raw["key"])

    mark_deleted(conn, [
        {"issue_id": v.issue_id, "reason": v.reason}
        for v in verdicts if v.reason in ("DELETED", "MOVED_OUT")
    ])
    conn.commit()
    return verdicts
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `pytest tests/ -v`
Expected: 전부 통과

- [ ] **Step 6: Commit**

```bash
git add jira_dashboard/pipeline/profile_fields.py jira_dashboard/pipeline/detect_deleted.py tests/pipeline/
git commit -m "feat: single-pass field profiling and delete/move disambiguation"
```

---

### Task 10: 워터마크 + 러너 + `cli sync`

**Files:**
- Create: `jira_dashboard/db/repository/sync.py`, `jira_dashboard/pipeline/runner.py`
- Modify: `jira_dashboard/cli.py`
- Create: `tests/pipeline/test_runner.py`, `README.md`

**Interfaces:**
- Consumes: 모든 파이프라인 단계 (T6~T9)
- Produces:
  - `read_watermark(conn, project_id) -> tuple[datetime | None, bool]` — (since, full_resync_requested)
  - `write_watermark(conn, project_id, since, status) -> None`
  - `request_full_resync(conn, project_id) -> None`, `clear_full_resync(conn, project_id) -> None`
  - `start_run(conn, instance_id, project_id, step) -> int`, `finish_run(conn, run_id, status, fetched, upserted, error) -> None`
  - `reclaim_zombies(conn, older_than_hours=6) -> int`
  - `RunSummary(projects_ok, projects_failed, issues_upserted, errors)`
  - `run_instance(conn, client, instance_id, *, dry_run=False, daily=False) -> RunSummary`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/pipeline/test_runner.py
from datetime import datetime, timezone

import pytest

from jira_dashboard.db.repository.catalog import FieldChangeReport
from jira_dashboard.jira.protocol import JiraAuthError, JiraTransientError
from jira_dashboard.pipeline import runner as mod
from jira_dashboard.pipeline.sync_issues import SyncResult
from tests.stubs import CONN, Recorder

MAX_UPDATED = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def rec(monkeypatch):
    r = Recorder()
    r.returns["reclaim_zombies"] = lambda *a, **k: 0
    r.returns["start_run"] = lambda *a, **k: 1
    r.returns["read_watermark"] = lambda conn, p: (None, False)
    r.returns["sync_catalog"] = lambda *a, **k: FieldChangeReport()
    r.returns["enabled_projects"] = lambda conn, i: [(7, "10000", "PROJ")]
    r.returns["sync_issues"] = lambda *a, **k: SyncResult(
        fetched=3, upserted=3, max_updated=MAX_UPDATED, changed_issue_ids=[1, 2, 3]
    )
    r.returns["derive_history"] = lambda *a, **k: 9
    r.returns["update_first_done_at"] = lambda *a, **k: 3
    r.patch(monkeypatch, mod, "sync_catalog", "enabled_projects", "sync_issues",
            "derive_history", "update_first_done_at", "profile_fields",
            "detect_deleted")
    r.patch(monkeypatch, mod.sync_repo, "reclaim_zombies", "start_run", "finish_run",
            "read_watermark", "write_watermark", "request_full_resync",
            "clear_full_resync")
    monkeypatch.setattr(CONN, "commit", lambda: None, raising=False)
    monkeypatch.setattr(CONN, "rollback", lambda: None, raising=False)
    return r


def test_reclaims_zombies_before_anything_else(rec):
    mod.run_instance(CONN, object(), 1)
    assert rec.names()[0] == "reclaim_zombies"


def test_catalog_runs_before_issues(rec):
    mod.run_instance(CONN, object(), 1)
    i_cat, i_iss = rec.order_of("sync_catalog", "sync_issues")
    assert i_cat < i_iss


def test_history_runs_after_issues(rec):
    mod.run_instance(CONN, object(), 1)
    i_iss, i_hist, i_done = rec.order_of(
        "sync_issues", "derive_history", "update_first_done_at"
    )
    assert i_iss < i_hist < i_done


def test_watermark_is_max_updated_minus_overlap(rec):
    from jira_dashboard.pipeline.sync_issues import OVERLAP

    mod.run_instance(CONN, object(), 1)
    payload = rec.first("write_watermark")
    assert payload["args"][1] == MAX_UPDATED - OVERLAP


def test_successful_run_reports_counts(rec):
    summary = mod.run_instance(CONN, object(), 1)
    assert (summary.projects_ok, summary.projects_failed) == (1, 0)
    assert summary.issues_upserted == 3


def test_project_failure_is_isolated(monkeypatch, rec):
    rec.returns["enabled_projects"] = lambda conn, i: [
        (7, "10000", "PROJ"), (8, "10001", "OTHER")
    ]
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise JiraTransientError(503)
        return SyncResult(fetched=1, upserted=1, max_updated=MAX_UPDATED,
                          changed_issue_ids=[9])

    monkeypatch.setattr(mod, "sync_issues", flaky)
    summary = mod.run_instance(CONN, object(), 1)
    assert (summary.projects_ok, summary.projects_failed) == (1, 1)
    assert "PROJ" in summary.errors


def test_failed_project_does_not_advance_watermark(monkeypatch, rec):
    monkeypatch.setattr(
        mod, "sync_issues", lambda *a, **k: (_ for _ in ()).throw(JiraTransientError(503))
    )
    mod.run_instance(CONN, object(), 1)
    payload = rec.first("write_watermark")
    assert payload["args"][1] is None      # since=None → NVL로 기존값 유지
    assert payload["args"][2] == "FAILED"


def test_auth_error_aborts_the_whole_instance(monkeypatch, rec):
    monkeypatch.setattr(
        mod, "sync_issues", lambda *a, **k: (_ for _ in ()).throw(JiraAuthError("401"))
    )
    with pytest.raises(JiraAuthError):
        mod.run_instance(CONN, object(), 1)
    assert rec.count("write_watermark") == 0


def test_full_resync_flag_cleared_only_on_success(rec):
    rec.returns["read_watermark"] = lambda conn, p: (None, True)
    mod.run_instance(CONN, object(), 1)
    assert rec.count("clear_full_resync") == 1


def test_full_resync_flag_survives_failure(monkeypatch, rec):
    rec.returns["read_watermark"] = lambda conn, p: (None, True)
    monkeypatch.setattr(
        mod, "sync_issues", lambda *a, **k: (_ for _ in ()).throw(JiraTransientError(503))
    )
    mod.run_instance(CONN, object(), 1)
    assert rec.count("clear_full_resync") == 0


def test_project_key_change_requests_full_resync(rec):
    rec.returns["sync_catalog"] = lambda *a, **k: FieldChangeReport(
        key_changed_projects=[7]
    )
    mod.run_instance(CONN, object(), 1)
    assert rec.count("request_full_resync") >= 1


def test_value_kind_change_requests_full_resync_for_all_projects(rec):
    rec.returns["sync_catalog"] = lambda *a, **k: FieldChangeReport(
        value_kind_changed=["customfield_10002"]
    )
    mod.run_instance(CONN, object(), 1)
    assert rec.count("request_full_resync") >= 1


def test_dry_run_rolls_back_and_skips_history(monkeypatch, rec):
    rolled = []
    monkeypatch.setattr(CONN, "rollback", lambda: rolled.append(1), raising=False)
    mod.run_instance(CONN, object(), 1, dry_run=True)
    assert rolled
    assert rec.count("derive_history") == 0
    assert rec.count("write_watermark") == 0


def test_daily_flag_runs_profiling_and_delete_detection(rec):
    mod.run_instance(CONN, object(), 1, daily=True)
    assert rec.count("profile_fields") == 1
    assert rec.count("detect_deleted") == 1


def test_daily_steps_are_skipped_by_default(rec):
    mod.run_instance(CONN, object(), 1)
    assert rec.count("profile_fields") == 0


def test_dry_run_skips_daily_steps(rec):
    mod.run_instance(CONN, object(), 1, dry_run=True, daily=True)
    assert rec.count("detect_deleted") == 0
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/pipeline/test_runner.py -v`
Expected: FAIL — `ModuleNotFoundError: jira_dashboard.db.repository.sync`

- [ ] **Step 3: sync.py 리포지토리 구현**

```python
# jira_dashboard/db/repository/sync.py
from datetime import datetime

_SELECT_WATERMARK = """
SELECT last_synced_updated_at, full_resync_requested
FROM   test_sync_watermark WHERE project_id = :project_id
"""

_MERGE_WATERMARK = """
MERGE INTO test_sync_watermark t
USING (SELECT :project_id AS project_id FROM dual) s
ON (t.project_id = s.project_id)
WHEN MATCHED THEN UPDATE SET
  t.last_synced_updated_at = NVL(:since, t.last_synced_updated_at),
  t.last_run_at = SYS_EXTRACT_UTC(SYSTIMESTAMP), t.last_status = :last_status
WHEN NOT MATCHED THEN
  INSERT (project_id, last_synced_updated_at, last_run_at, last_status)
  VALUES (:project_id, :since, SYS_EXTRACT_UTC(SYSTIMESTAMP), :last_status)
"""

_MERGE_REQUEST_RESYNC = """
MERGE INTO test_sync_watermark t
USING (SELECT :project_id AS project_id FROM dual) s
ON (t.project_id = s.project_id)
WHEN MATCHED THEN UPDATE SET t.full_resync_requested = 'Y'
WHEN NOT MATCHED THEN INSERT (project_id, full_resync_requested)
                      VALUES (:project_id, 'Y')
"""

_CLEAR_RESYNC = """
UPDATE test_sync_watermark SET full_resync_requested = 'N' WHERE project_id = :project_id
"""

_INSERT_RUN = """
INSERT INTO test_sync_run (instance_id, project_id, step)
VALUES (:instance_id, :project_id, :step) RETURNING run_id INTO :out_run_id
"""

_FINISH_RUN = """
UPDATE test_sync_run
SET    finished_at = SYS_EXTRACT_UTC(SYSTIMESTAMP), status = :status,
       issues_fetched = :issues_fetched, issues_upserted = :issues_upserted,
       error_msg = :error_msg
WHERE  run_id = :run_id
"""

_RECLAIM_RUNS = """
UPDATE test_sync_run
SET    status = 'FAILED', finished_at = SYS_EXTRACT_UTC(SYSTIMESTAMP),
       error_msg = 'reclaimed: process died while RUNNING'
WHERE  status = 'RUNNING'
AND    started_at < SYS_EXTRACT_UTC(SYSTIMESTAMP) - NUMTODSINTERVAL(:hours, 'HOUR')
"""

_RECLAIM_WATERMARKS = """
UPDATE test_sync_watermark SET last_status = 'FAILED' WHERE last_status = 'RUNNING'
"""


def read_watermark(conn, project_id: int) -> tuple[datetime | None, bool]:
    """신규 프로젝트는 행이 없다 → (None, False)로 전체 수집 (spec §5.10)."""
    cur = conn.cursor()
    cur.execute(_SELECT_WATERMARK, project_id=project_id)
    row = cur.fetchone()
    if row is None:
        return None, False
    since, full = row
    return (None if full == "Y" else since), full == "Y"


def write_watermark(conn, project_id: int, since: datetime | None, status: str) -> None:
    """since=None이면 NVL이 기존 값을 유지한다 — 실패한 프로젝트는 전진하지 않는다."""
    conn.cursor().execute(_MERGE_WATERMARK, project_id=project_id,
                          since=since, last_status=status)


def request_full_resync(conn, project_id: int) -> None:
    conn.cursor().execute(_MERGE_REQUEST_RESYNC, project_id=project_id)
    conn.commit()


def clear_full_resync(conn, project_id: int) -> None:
    """성공 직후에만 부른다. 실패 시 그대로 두어 다음 배치가 다시 시도한다."""
    conn.cursor().execute(_CLEAR_RESYNC, project_id=project_id)


def start_run(conn, instance_id: int, project_id: int | None, step: str) -> int:
    cur = conn.cursor()
    out = cur.var(int)
    cur.execute(_INSERT_RUN, instance_id=instance_id, project_id=project_id,
                step=step, out_run_id=out)
    conn.commit()
    return out.getvalue()[0]


def finish_run(conn, run_id: int, status: str, fetched: int = 0,
               upserted: int = 0, error: str | None = None) -> None:
    conn.cursor().execute(
        _FINISH_RUN, run_id=run_id, status=status, issues_fetched=fetched,
        issues_upserted=upserted, error_msg=(error or "")[:4000] or None,
    )
    conn.commit()


def reclaim_zombies(conn, older_than_hours: int = 6) -> int:
    """프로세스가 죽으면 RUNNING이 영원히 남는다. 표시만 정리하고 실행 제어에는
    쓰지 않는다 — 중복 실행 방지는 cron의 flock이 한다 (spec §5.10)."""
    cur = conn.cursor()
    cur.execute(_RECLAIM_RUNS, hours=older_than_hours)
    reclaimed = cur.rowcount
    cur.execute(_RECLAIM_WATERMARKS)
    conn.commit()
    return reclaimed
```

- [ ] **Step 4: runner.py 구현**

```python
# jira_dashboard/pipeline/runner.py
import logging
from dataclasses import dataclass, field

from jira_dashboard.db.repository import sync as sync_repo
from jira_dashboard.db.repository.catalog import enabled_projects
from jira_dashboard.jira.protocol import JiraAuthError
from jira_dashboard.pipeline.derive_history import derive_history, update_first_done_at
from jira_dashboard.pipeline.detect_deleted import detect_deleted
from jira_dashboard.pipeline.profile_fields import profile_fields
from jira_dashboard.pipeline.sync_catalog import sync_catalog
from jira_dashboard.pipeline.sync_issues import next_watermark, sync_issues

log = logging.getLogger(__name__)


@dataclass
class RunSummary:
    projects_ok: int = 0
    projects_failed: int = 0
    issues_upserted: int = 0
    errors: dict[str, str] = field(default_factory=dict)


def run_instance(conn, client, instance_id: int, *,
                 dry_run: bool = False, daily: bool = False) -> RunSummary:
    """인스턴스 내 프로젝트는 순차 처리한다. 병렬은 인스턴스 단위다 (spec §5.0)."""
    sync_repo.reclaim_zombies(conn)
    summary = RunSummary()

    run_id = sync_repo.start_run(conn, instance_id, None, "CATALOG")
    try:
        report = sync_catalog(conn, client, instance_id)
        conn.commit()
        sync_repo.finish_run(conn, run_id, "SUCCESS")
    except Exception as exc:
        conn.rollback()
        sync_repo.finish_run(conn, run_id, "FAILED", error=repr(exc))
        raise

    projects = enabled_projects(conn, instance_id)

    for project_id in report.key_changed_projects:
        log.warning("project key changed (project_id=%s); full resync queued", project_id)
        sync_repo.request_full_resync(conn, project_id)
    if report.value_kind_changed:
        log.warning("value_kind changed for %s; full resync queued for all projects",
                    report.value_kind_changed)
        for project_id, _, _ in projects:
            sync_repo.request_full_resync(conn, project_id)

    for project_id, _, project_key in projects:
        since, full_resync = sync_repo.read_watermark(conn, project_id)
        run_id = sync_repo.start_run(conn, instance_id, project_id, "ISSUES")
        try:
            result = sync_issues(conn, client, instance_id, project_id,
                                 project_key, since)
            if dry_run:
                conn.rollback()
                sync_repo.finish_run(conn, run_id, "SUCCESS", result.fetched, 0)
                summary.projects_ok += 1
                continue

            derive_history(conn, instance_id, result.changed_issue_ids)
            update_first_done_at(conn, result.changed_issue_ids)
            sync_repo.write_watermark(
                conn, project_id, next_watermark(result.max_updated, since), "SUCCESS"
            )
            if full_resync:
                sync_repo.clear_full_resync(conn, project_id)   # 성공 직후에만
            conn.commit()
            sync_repo.finish_run(conn, run_id, "SUCCESS",
                                 result.fetched, result.upserted)
            summary.projects_ok += 1
            summary.issues_upserted += result.upserted
        except JiraAuthError:
            conn.rollback()
            sync_repo.finish_run(conn, run_id, "FAILED", error="auth failed")
            raise
        except Exception as exc:
            conn.rollback()
            log.exception("project %s failed", project_key)
            sync_repo.write_watermark(conn, project_id, None, "FAILED")
            conn.commit()
            sync_repo.finish_run(conn, run_id, "FAILED", error=repr(exc))
            summary.projects_failed += 1
            summary.errors[project_key] = repr(exc)

    if daily and not dry_run:
        run_id = sync_repo.start_run(conn, instance_id, None, "PROFILE")
        profile_fields(conn, instance_id)
        sync_repo.finish_run(conn, run_id, "SUCCESS")

        run_id = sync_repo.start_run(conn, instance_id, None, "DETECT_DELETED")
        detect_deleted(conn, client, instance_id)
        sync_repo.finish_run(conn, run_id, "SUCCESS")

    return summary
```

- [ ] **Step 5: cli.py 구현**

`db` 서브커맨드는 없다 — DDL은 사람이 실행한다 (spec §11.1).

```python
# jira_dashboard/cli.py
import argparse
import logging
import sys

SELECT_INSTANCE = """
SELECT instance_id, base_url, auth_type, secret_ref
FROM   test_jira_instance WHERE instance_key = :instance_key
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jira_dashboard")
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="run the collection pipeline")
    sync.add_argument("--instance", required=True, help="instance_key")
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--daily", action="store_true",
                      help="also run profiling and delete detection")

    doctor = sub.add_parser("doctor", help="verify environment assumptions (read-only)")
    doctor.add_argument("--db", action="store_true")
    doctor.add_argument("--jira", action="store_true")
    doctor.add_argument("--skip-schema", action="store_true",
                        help="skip DDL/schema comparison (use before DDL is applied)")
    doctor.add_argument("--instance", help="instance_key (required with --jira)")
    doctor.add_argument("--project", help="probe project key (required with --jira)")

    cap = sub.add_parser("capture", help="save real API responses (on-prem only)")
    cap.add_argument("--instance", required=True)
    cap.add_argument("--project", required=True)
    cap.add_argument("--limit", type=int, default=200)
    cap.add_argument("--anonymize", action="store_true")
    cap.add_argument("--out", default="tests/fixtures/captured")
    return parser


def _client_for(conn, instance_key: str):
    from jira_dashboard.jira.client import HttpJiraClient

    cur = conn.cursor()
    cur.execute(SELECT_INSTANCE, instance_key=instance_key)
    row = cur.fetchone()
    if row is None:
        raise SystemExit(f"unknown instance: {instance_key}")
    instance_id, base_url, auth_type, secret_ref = row
    return instance_id, HttpJiraClient.from_config(base_url, auth_type, secret_ref)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = _build_parser().parse_args(argv)

    from jira_dashboard.db.pool import db_conn

    if args.command == "sync":
        from jira_dashboard.pipeline.runner import run_instance

        with db_conn() as conn:
            instance_id, client = _client_for(conn, args.instance)
            summary = run_instance(conn, client, instance_id,
                                   dry_run=args.dry_run, daily=args.daily)
        print(f"ok={summary.projects_ok} failed={summary.projects_failed} "
              f"upserted={summary.issues_upserted}")
        for key, err in summary.errors.items():
            print(f"  {key}: {err}")
        return 1 if summary.projects_failed else 0

    if args.command == "doctor":
        from jira_dashboard.doctor.db_checks import format_report, run_db_checks

        failed = False
        if args.db or not args.jira:
            with db_conn() as conn:
                results = run_db_checks(conn, skip_schema=args.skip_schema)
            print("=== DB ===")
            print(format_report(results))
            failed |= any(r.verdict == "FAIL" for r in results)
        if args.jira:
            from jira_dashboard.doctor.jira_checks import run_jira_checks

            with db_conn() as conn:
                _, client = _client_for(conn, args.instance)
            results = run_jira_checks(client, args.project)
            print("=== JIRA ===")
            print(format_report(results))
            failed |= any(r.verdict == "FAIL" for r in results)
        return 1 if failed else 0

    if args.command == "capture":
        from pathlib import Path

        from jira_dashboard.capture import capture_fixtures

        with db_conn() as conn:
            _, client = _client_for(conn, args.instance)
        counts = capture_fixtures(client, args.project, Path(args.out),
                                  limit=args.limit, anonymize=args.anonymize)
        print(counts)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: README.md에 cron 등록 문서화**

**`flock -n`이 없으면 배치가 주기보다 오래 걸릴 때 겹친다** (spec §5.10). DB 상태 컬럼으로 막으면 프로세스 사망 시 락이 영원히 남는다.

```
0 * * * *  flock -n /var/run/jira_sync.lock \
           /opt/jira_dashboard/.venv/bin/python -m jira_dashboard.cli sync --instance SITE_A
30 2 * * * flock -n /var/run/jira_sync.lock \
           /opt/jira_dashboard/.venv/bin/python -m jira_dashboard.cli sync --instance SITE_A --daily
```

- [ ] **Step 7: 테스트 통과 확인**

Run: `pytest tests/ -v`
Expected: 전부 통과

- [ ] **Step 8: Commit**

```bash
git add jira_dashboard/db/repository/sync.py jira_dashboard/pipeline/runner.py jira_dashboard/cli.py README.md tests/pipeline/test_runner.py
git commit -m "feat: pipeline runner with per-project isolation and watermark management"
```

---

### Task 11: `HttpJiraClient` + `doctor`

`doctor`가 spec §4.0의 A1~A12와 §2.4 DB 전제를 **실행 가능한 검사**로 바꾼다. 사외에서는 판정 로직만 검증되고, 결과가 맞는지는 사내에서만 안다.

**Files:**
- Create: `jira_dashboard/jira/client.py`, `jira_dashboard/doctor/db_checks.py`, `jira_dashboard/doctor/jira_checks.py`
- Create: `tests/unit/test_doctor_jira.py`, `tests/unit/test_doctor_db.py`

**Interfaces:**
- Consumes: `JiraClient` 프로토콜 (T5), `schema_map.parse_ddl` (T3)
- Produces:
  - `HttpJiraClient.from_config(base_url, auth_type, secret_ref)`
  - `CheckResult(id, title, verdict, observed, impact)` — verdict ∈ {`PASS`, `FAIL`, `WARN`}
  - `run_db_checks(conn, *, skip_schema=False) -> list[CheckResult]`
  - `run_jira_checks(client, probe_project_key) -> list[CheckResult]`
  - `format_report(results) -> str`

- [ ] **Step 1: 실패하는 테스트 작성**

DB 검사는 **가짜 커서**로 판정 로직만 본다.

```python
# tests/unit/test_doctor_db.py
import pytest

from jira_dashboard.doctor.db_checks import run_db_checks


class FakeCursor:
    def __init__(self, answers): self._answers, self._rows = answers, []
    def execute(self, sql, **binds):
        for key, rows in self._answers.items():
            if key in sql:
                self._rows = rows
                return
        self._rows = []
    def fetchone(self): return self._rows[0] if self._rows else None
    def fetchall(self): return self._rows


class FakeConn:
    def __init__(self, answers): self._answers = answers
    def cursor(self): return FakeCursor(self._answers)


def _conn(**overrides):
    answers = {
        "banner_full": [("Oracle Database 19c Enterprise Edition",)],
        "max_string_size": [("STANDARD",)],
        "v$timezone_names": [(1,)],
        "NLS_CHARACTERSET": [("AL32UTF8",)],
        "db_block_size": [("8192",)],
        "user_sys_privs": [("CREATE TABLE",), ("CREATE SEQUENCE",), ("CREATE VIEW",)],
        "user_tables": [(16,)],
    }
    answers.update(overrides)
    return FakeConn(answers)


def _by_id(results): return {r.id: r for r in results}


def test_passes_on_19c():
    r = _by_id(run_db_checks(_conn(), skip_schema=True))
    assert r["DB1"].verdict == "PASS"


def test_fails_on_23ai():
    conn = _conn(banner_full=[("Oracle Database 23ai Free",)])
    r = _by_id(run_db_checks(conn, skip_schema=True))
    assert r["DB1"].verdict == "FAIL"


def test_warns_on_extended_string_size():
    conn = _conn(max_string_size=[("EXTENDED",)])
    r = _by_id(run_db_checks(conn, skip_schema=True))
    assert r["DB2"].verdict == "WARN"


def test_fails_when_seoul_timezone_missing():
    r = _by_id(run_db_checks(_conn(**{"v$timezone_names": [(0,)]}), skip_schema=True))
    assert r["DB3"].verdict == "FAIL"


def test_fails_on_missing_ddl_privileges():
    conn = _conn(user_sys_privs=[("CREATE TABLE",)])
    r = _by_id(run_db_checks(conn, skip_schema=True))
    assert r["DB5"].verdict == "FAIL"
    assert "CREATE SEQUENCE" in r["DB5"].impact


def test_skip_schema_omits_the_schema_check():
    ids = {r.id for r in run_db_checks(_conn(), skip_schema=True)}
    assert "DB7" not in ids


def test_schema_check_reports_missing_tables(monkeypatch):
    """DDL을 아직 안 돌렸으면 FAIL이어야 한다 — 런북 4단계의 게이트."""
    from jira_dashboard.doctor import db_checks

    monkeypatch.setattr(db_checks, "_actual_schema", lambda conn: {})
    r = _by_id(db_checks.run_db_checks(_conn()))
    assert r["DB7"].verdict == "FAIL"


def test_every_result_carries_impact_text():
    """FAIL일 때 무엇을 고쳐야 하는지 알려주지 않으면 도구가 아니다."""
    for result in run_db_checks(_conn(), skip_schema=True):
        assert result.impact, result.id
```

```python
# tests/unit/test_doctor_jira.py
from jira_dashboard.doctor.jira_checks import run_jira_checks


def _by_id(results): return {r.id: r for r in results}


def test_fake_client_passes_every_assumption(fake_jira):
    """Fake는 A1~A12 가정을 그대로 구현했으므로 전부 PASS여야 한다.
    사내에서 FAIL이 나오면 그것이 정확히 가정이 깨진 지점이다."""
    failures = [r for r in run_jira_checks(fake_jira, "PROJ") if r.verdict == "FAIL"]
    assert failures == [], [(r.id, r.observed) for r in failures]


def test_detects_descending_changelog(fake_jira):
    """A4가 뒤집힌 상황. 구간 테이블이 통째로 틀리는 케이스다."""
    for issue in fake_jira._issues.values():
        cl = issue.get("changelog") or {}
        cl["histories"] = list(reversed(cl.get("histories") or []))
    assert _by_id(run_jira_checks(fake_jira, "PROJ"))["A4"].verdict == "FAIL"


def test_detects_missing_status_category_key(fake_jira):
    """A9가 깨지면 인스턴스 간 교차라는 설계 전제가 사라진다."""
    for s in fake_jira._statuses:
        s["statusCategory"].pop("key", None)
    assert _by_id(run_jira_checks(fake_jira, "PROJ"))["A9"].verdict == "FAIL"


def test_detects_missing_schema_in_field_response(fake_jira):
    for f in fake_jira._fields:
        f.pop("schema", None)
    assert _by_id(run_jira_checks(fake_jira, "PROJ"))["A8"].verdict == "FAIL"


def test_warns_on_shrunk_max_results(fixture_dir):
    from jira_dashboard.jira.fake import FakeJiraClient

    r = _by_id(run_jira_checks(FakeJiraClient(fixture_dir, server_max_results=50), "PROJ"))
    assert (r["A7"].observed, r["A7"].verdict) == ("50", "WARN")


def test_warns_when_field_id_is_absent_from_changelog(fake_jira):
    for issue in fake_jira._issues.values():
        for h in (issue.get("changelog") or {}).get("histories") or []:
            for item in h.get("items") or []:
                item.pop("fieldId", None)
    assert _by_id(run_jira_checks(fake_jira, "PROJ"))["A5"].verdict == "WARN"
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/unit/test_doctor_db.py tests/unit/test_doctor_jira.py -v`
Expected: FAIL — `ModuleNotFoundError: jira_dashboard.doctor.db_checks`

- [ ] **Step 3: client.py 구현**

```python
# jira_dashboard/jira/client.py
import os
import time

import httpx

from jira_dashboard.jira.protocol import (
    ChangelogPage, JiraAuthError, JiraTransientError, SearchPage,
)

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 5


class HttpJiraClient:
    def __init__(self, base_url: str, token: str, *, auth_type: str = "PAT",
                 timeout: float = 60.0) -> None:
        scheme = "Bearer" if auth_type == "PAT" else "Basic"
        self._c = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"{scheme} {token}"},
            timeout=timeout,
        )

    @classmethod
    def from_config(cls, base_url: str, auth_type: str, secret_ref: str):
        token = os.environ.get(secret_ref)
        if not token:
            raise JiraAuthError(f"environment variable {secret_ref} is not set")
        return cls(base_url, token, auth_type=auth_type)

    def _request(self, method: str, path: str, **kw):
        for attempt in range(1, MAX_ATTEMPTS + 1):
            resp = self._c.request(method, path, **kw)
            if resp.status_code in (401, 403):
                raise JiraAuthError(f"HTTP {resp.status_code} on {path}")
            if resp.status_code == 404:
                return None
            if resp.status_code in RETRY_STATUSES:
                if attempt == MAX_ATTEMPTS:
                    raise JiraTransientError(resp.status_code, path)
                time.sleep(min(2 ** attempt, 30))
                continue
            resp.raise_for_status()
            return resp.json()
        raise JiraTransientError(0, "unreachable")

    def get_fields(self) -> list[dict]:
        return self._request("GET", "/rest/api/2/field") or []

    def get_projects(self) -> list[dict]:
        return self._request("GET", "/rest/api/2/project") or []

    def get_statuses(self) -> list[dict]:
        return self._request("GET", "/rest/api/2/status") or []

    def search_issues(self, jql: str, start_at: int, max_results: int,
                      expand_changelog: bool) -> SearchPage:
        body = {"jql": jql, "startAt": start_at, "maxResults": max_results,
                "fields": ["*all"]}
        if expand_changelog:
            body["expand"] = ["changelog"]   # renderedFields는 넣지 않는다 (응답이 배로 커짐)
        data = self._request("POST", "/rest/api/2/search", json=body) or {}
        return SearchPage(
            start_at=data.get("startAt", start_at),
            max_results=data.get("maxResults", max_results),   # 서버 응답값을 믿는다 (A7)
            total=data.get("total", 0),
            issues=data.get("issues", []),
        )

    def get_issue_changelog(self, issue_key: str, start_at: int) -> ChangelogPage:
        # A3: /issue/{key} 가 changelog에 startAt을 지원하는지는 T1에서 확인한다.
        # 지원하면 params에 넘기고, 아니면 아래처럼 전체를 받아 슬라이스한다.
        # >>> T1 판정 결과를 여기에 기록할 것 <<<
        data = self._request(
            "GET", f"/rest/api/2/issue/{issue_key}",
            params={"expand": "changelog", "fields": "id"},
        ) or {}
        cl = data.get("changelog") or {}
        histories = cl.get("histories") or []
        return ChangelogPage(
            start_at=start_at,
            max_results=cl.get("maxResults", len(histories)),
            total=cl.get("total", len(histories)),
            histories=histories[start_at:],
        )

    def get_issue(self, jira_issue_id: str, fields: list[str]) -> dict | None:
        return self._request("GET", f"/rest/api/2/issue/{jira_issue_id}",
                             params={"fields": ",".join(fields)})
```

- [ ] **Step 4: db_checks.py 구현**

```python
# jira_dashboard/doctor/db_checks.py
from dataclasses import dataclass
from pathlib import Path

from jira_dashboard.db import schema_map

DDL_DIR = Path(__file__).parents[1] / "db" / "ddl"

_SCHEMA_TABLES = """
SELECT table_name FROM user_tables WHERE table_name LIKE 'TEST\\_%' ESCAPE '\\'
"""
_SCHEMA_COLUMNS = """
SELECT table_name, column_name FROM user_tab_columns
WHERE  table_name LIKE 'TEST\\_%' ESCAPE '\\'
"""


@dataclass(frozen=True)
class CheckResult:
    id: str
    title: str
    verdict: str        # PASS | FAIL | WARN
    observed: str
    impact: str


def _one(conn, sql, **binds):
    cur = conn.cursor()
    cur.execute(sql, **binds)
    row = cur.fetchone()
    return row[0] if row else None


def _actual_schema(conn) -> dict[str, set[str]]:
    cur = conn.cursor()
    cur.execute(_SCHEMA_COLUMNS)
    out: dict[str, set[str]] = {}
    for table, column in cur.fetchall():
        out.setdefault(table.upper(), set()).add(column.upper())
    return out


def run_db_checks(conn, *, skip_schema: bool = False) -> list[CheckResult]:
    out: list[CheckResult] = []

    banner = _one(conn, "SELECT banner_full FROM v$version") or ""
    out.append(CheckResult(
        "DB1", "Oracle 버전이 19c인가", "PASS" if "19" in banner else "FAIL",
        banner, "상위 버전이면 spec §2.4의 문법 전제를 재검토해야 한다",
    ))

    mss = _one(conn, "SELECT value FROM v$parameter WHERE name = 'max_string_size'") or "?"
    out.append(CheckResult(
        "DB2", "max_string_size", "PASS" if mss == "STANDARD" else "WARN", mss,
        "EXTENDED면 VARCHAR2 상한이 32767이 되어 컬럼 폭 설계를 다시 볼 수 있다",
    ))

    tz = _one(conn, "SELECT COUNT(*) FROM v$timezone_names WHERE tzname = 'Asia/Seoul'")
    out.append(CheckResult(
        "DB3", "Asia/Seoul 타임존 파일", "PASS" if tz else "FAIL", str(tz),
        "없으면 AT TIME ZONE 버킷팅이 실패한다. 고정 오프셋으로 폴백해야 한다",
    ))

    charset = _one(
        conn,
        "SELECT value FROM nls_database_parameters "
        "WHERE parameter = 'NLS_CHARACTERSET'",
    ) or "?"
    block = _one(conn, "SELECT value FROM v$parameter WHERE name = 'db_block_size'") or "?"
    out.append(CheckResult(
        "DB4", "캐릭터셋 / 블록 크기", "PASS", f"{charset} / {block}",
        "기록용. VARCHAR2 BYTE 세만틱 전제(spec §2.2)의 근거가 된다",
    ))

    cur = conn.cursor()
    cur.execute(
        "SELECT privilege FROM user_sys_privs "
        "WHERE privilege IN ('CREATE TABLE', 'CREATE SEQUENCE', 'CREATE VIEW')"
    )
    granted = {r[0] for r in cur.fetchall()}
    needed = {"CREATE TABLE", "CREATE SEQUENCE", "CREATE VIEW"}
    missing = needed - granted
    out.append(CheckResult(
        "DB5", "DDL 권한", "PASS" if not missing else "FAIL",
        ", ".join(sorted(granted)) or "(none)",
        f"부족: {', '.join(sorted(missing)) or '없음'} — DDL 실행이 실패한다",
    ))

    existing = _one(
        conn,
        r"SELECT COUNT(*) FROM user_tables WHERE table_name LIKE 'TEST\_%' ESCAPE '\'",
    )
    out.append(CheckResult(
        "DB6", "기존 TEST_ 객체", "PASS", f"{existing} tables",
        "0이면 DDL을 아직 안 돌린 것이다. drop_all.sql 전에 목록을 눈으로 확인할 것",
    ))

    if not skip_schema:
        expected = schema_map.parse_ddl(DDL_DIR)
        actual = _actual_schema(conn)
        problems = []
        for table, columns in expected.items():
            if table not in actual:
                problems.append(f"missing table {table}")
                continue
            for column in sorted(columns - actual[table]):
                problems.append(f"{table}.{column} missing")
        out.append(CheckResult(
            "DB7", "DDL 파일 ↔ 실제 스키마 대조",
            "PASS" if not problems else "FAIL",
            "matches" if not problems else "; ".join(problems[:5]),
            "DDL을 다시 적용해야 한다 (docs/ddl-apply.md). 자동으로 고치지 않는다",
        ))
    return out


def format_report(results: list[CheckResult]) -> str:
    lines = []
    for r in results:
        lines.append(f"{r.id:<4} {r.title:<34} {r.verdict:<5} {r.observed}")
        if r.verdict != "PASS":
            lines.append(f"     → {r.impact}")
    return "\n".join(lines)
```

- [ ] **Step 5: jira_checks.py 구현**

```python
# jira_dashboard/doctor/jira_checks.py
from jira_dashboard.doctor.db_checks import CheckResult, format_report

__all__ = ["run_jira_checks", "format_report"]

_VALID_CATEGORIES = {"new", "indeterminate", "done", "undefined"}


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

    cl = sample.get("changelog") or {}
    limit = cl.get("maxResults")
    out.append(CheckResult(
        "A3", "인라인 changelog 상한", "PASS" if limit else "WARN", str(limit),
        "total/maxResults로 보충 호출 여부를 판별할 수 없으면 로직을 바꿔야 한다",
    ))

    ascending = None
    for issue in page.issues:
        hist = (issue.get("changelog") or {}).get("histories") or []
        if len(hist) >= 2:
            stamps = [h["created"] for h in hist]
            ascending = stamps == sorted(stamps)
            break
    out.append(CheckResult(
        "A4", "changelog 오름차순", "PASS" if ascending else "FAIL",
        f"ascending={ascending}",
        "구간 테이블이 통째로 뒤집힌다. sync_issues 보충 호출과 "
        "derive_history 정렬을 함께 수정할 것 (spec §5.2, §5.3)",
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
```

- [ ] **Step 6: 테스트 통과 확인**

Run: `pytest tests/ -v`
Expected: 전부 통과

- [ ] **Step 7: Commit**

```bash
git add jira_dashboard/jira/client.py jira_dashboard/doctor/ tests/unit/test_doctor_db.py tests/unit/test_doctor_jira.py
git commit -m "feat: HTTP Jira client and doctor checks for DB and API assumptions"
```

---

### Task 12: `capture` + 오프라인 wheel 번들

**Files:**
- Create: `jira_dashboard/capture.py`, `requirements.txt`, `Makefile`
- Create: `tests/unit/test_capture.py`

**Interfaces:**
- Consumes: `JiraClient` (T5)
- Produces:
  - `capture_fixtures(client, project_key, out_dir, *, limit=200, anonymize=False) -> dict[str, int]`
  - `make vendor`, `make verify-vendor`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/unit/test_capture.py
import json

from jira_dashboard.capture import capture_fixtures


def test_writes_all_four_fixture_files(fake_jira, tmp_path):
    counts = capture_fixtures(fake_jira, "PROJ", tmp_path, limit=100)
    for name in ("fields.json", "projects.json", "statuses.json", "issues.json"):
        assert (tmp_path / name).exists()
    assert counts["issues"] > 0


def test_captured_fixtures_drive_the_same_fake_client(fake_jira, tmp_path):
    """사외 픽스처와 사내 픽스처에 같은 테스트를 돌릴 수 있어야 한다 (spec §11.3)."""
    from jira_dashboard.jira.fake import FakeJiraClient

    capture_fixtures(fake_jira, "PROJ", tmp_path, limit=100)
    replayed = FakeJiraClient(tmp_path)
    original = fake_jira.search_issues("project = PROJ ORDER BY updated ASC", 0, 100, True)
    copy = replayed.search_issues("project = PROJ ORDER BY updated ASC", 0, 100, True)
    assert copy.total == original.total


def test_includes_an_issue_with_oversized_changelog(fake_jira, tmp_path):
    """보충 호출 경로를 사내 픽스처로도 테스트할 수 있어야 한다."""
    capture_fixtures(fake_jira, "PROJ", tmp_path, limit=100)
    issues = json.loads((tmp_path / "issues.json").read_text(encoding="utf-8"))
    assert any(i["changelog"]["total"] > 100 for i in issues)


def test_anonymize_replaces_user_keys_and_summaries(fake_jira, tmp_path):
    capture_fixtures(fake_jira, "PROJ", tmp_path, limit=100, anonymize=True)
    issues = json.loads((tmp_path / "issues.json").read_text(encoding="utf-8"))
    for issue in issues:
        assert issue["fields"]["summary"].startswith("issue-")


def test_anonymize_is_off_by_default(fake_jira, tmp_path):
    """반출 금지 상황에서 기본 익명화는 '가져가도 되겠지'를 부른다 (spec §11.6)."""
    capture_fixtures(fake_jira, "PROJ", tmp_path, limit=100)
    issues = json.loads((tmp_path / "issues.json").read_text(encoding="utf-8"))
    assert not issues[0]["fields"]["summary"].startswith("issue-")


def test_respects_limit(fake_jira, tmp_path):
    counts = capture_fixtures(fake_jira, "PROJ", tmp_path, limit=3)
    assert counts["issues"] == 3
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/unit/test_capture.py -v`
Expected: FAIL — `ModuleNotFoundError: jira_dashboard.capture`

- [ ] **Step 3: capture.py 구현**

```python
# jira_dashboard/capture.py
import hashlib
import json
import logging
from pathlib import Path

from jira_dashboard.jira.protocol import JiraClient

log = logging.getLogger(__name__)


def _pseudonym(prefix: str, value: str | None) -> str | None:
    if value is None:
        return None
    return f"{prefix}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:8]}"


def _anonymize_issue(issue: dict) -> dict:
    fields = issue.get("fields") or {}
    fields["summary"] = _pseudonym("issue", issue.get("key"))
    for role in ("assignee", "reporter", "creator"):
        user = fields.get(role)
        if isinstance(user, dict):
            key = user.get("key") or user.get("name")
            user["displayName"] = _pseudonym("user", key)
            user["key"] = _pseudonym("key", key)
            user["name"] = user["key"]
    for history in (issue.get("changelog") or {}).get("histories") or []:
        author = history.get("author")
        if isinstance(author, dict):
            key = author.get("key") or author.get("name")
            author["displayName"] = _pseudonym("user", key)
            author["key"] = _pseudonym("key", key)
    return issue


def capture_fixtures(client: JiraClient, project_key: str, out_dir: Path,
                     *, limit: int = 200, anonymize: bool = False) -> dict[str, int]:
    """읽기 전용. 사내에서 실행하고 사내에만 남긴다.

    anonymize는 기본값이 꺼져 있다 — 반출이 금지된 상황에서 익명화 옵션이 켜져 있으면
    "익명화했으니 가져가도 되겠지"라는 판단을 부른다. 이 옵션은 사내 보관 데이터의
    가독성 조절용이지 반출 허가가 아니다 (spec §11.6).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def dump(name: str, payload) -> int:
        (out_dir / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return len(payload)

    counts = {
        "fields": dump("fields.json", client.get_fields()),
        "projects": dump("projects.json", client.get_projects()),
        "statuses": dump("statuses.json", client.get_statuses()),
    }

    jql = f"project = {project_key} ORDER BY updated ASC"
    issues, start_at, oversized = [], 0, False
    while len(issues) < limit:
        page = client.search_issues(jql, start_at, 100, True)
        if not page.issues:
            break
        for issue in page.issues:
            cl = issue.get("changelog") or {}
            if cl.get("total", 0) > cl.get("maxResults", 100):
                oversized = True
            issues.append(_anonymize_issue(issue) if anonymize else issue)
        start_at += page.max_results
        if start_at >= page.total:
            break

    if not oversized:
        log.warning(
            "no issue with changelog.total > maxResults was captured — "
            "the supplemental-fetch path stays untested against real data"
        )

    counts["issues"] = dump("issues.json", issues[:limit])
    return counts
```

- [ ] **Step 4: requirements.txt + Makefile 작성**

`pyproject.toml`의 의존성을 버전 고정으로 옮긴다.

```makefile
# Makefile
.PHONY: test vendor verify-vendor

test:
	pytest -v

vendor:
	pip download -r requirements.txt -d vendor/ \
	  --only-binary=:all: --python-version 3.12 --platform manylinux2014_x86_64

verify-vendor:
	python -m venv /tmp/offline-check
	/tmp/offline-check/bin/pip install --no-index --find-links vendor/ -r requirements.txt
	/tmp/offline-check/bin/python -c "import oracledb, httpx, pydantic_settings; print('ok')"
	rm -rf /tmp/offline-check
```

**`oracledb`를 thin 모드로 고른 것이 여기서 값을 한다** — 순수 Python이라 Instant Client 설치나 네이티브 컴파일이 필요 없고, 반입 대상이 wheel 몇 개로 끝난다.

- [ ] **Step 5: 오프라인 설치 검증**

Run: `make vendor && make verify-vendor`
Expected: `ok`. 실패하는 패키지가 있으면 **그 패키지를 의존성에서 뺄 수 있는지 먼저 검토한다** — 반입 대상을 늘리는 것보다 낫다.

- [ ] **Step 6: 전체 테스트 통과 확인**

Run: `pytest -v`
Expected: 전부 통과

- [ ] **Step 7: Commit**

```bash
git add jira_dashboard/capture.py requirements.txt Makefile tests/unit/test_capture.py
git commit -m "feat: fixture capture and offline wheel bundle"
```

---

## 사내 반입 체크리스트

코드가 아니라 절차다. spec §11.7 런북을 그대로 따른다. **순서가 중요하다 — 읽기 전용 검사를 통과한 뒤에 DDL을 돌리고, DDL이 끝난 뒤에 코드를 돌린다.**

### 사외에서 반입 전

- [ ] `pytest -v` 전부 통과 (`JIRA_FIXTURES=synthetic`)
- [ ] `pytest tests/static/ -v` 통과 — DDL↔SQL 대조, 금지 문법, `ESCAPE` 검사
- [ ] `make vendor && make verify-vendor` 성공
- [ ] `docs/api-verification.md` 의 A1~A12 판정이 최신
- [ ] `jira/client.py`의 `get_issue_changelog`에 **A3 판정 결과가 주석으로 기록됨**
- [ ] 사내 Git에 push

### 사내에서

- [ ] `pip install --no-index --find-links vendor/ -r requirements.txt`
- [ ] `cli doctor --db --skip-schema` — **권한이 부족하면 여기서 멈춘다**
- [ ] `docs/ddl-apply.md` 대로 `01_catalog.sql` ~ `06_ops.sql` 실행
- [ ] `cli doctor --db` — DB7(스키마 대조)까지 PASS
- [ ] `cli doctor --jira --instance SITE_A --project TEST` — 결과를 `docs/api-verification.md`에 반영
- [ ] `cli capture --instance SITE_A --project TEST --limit 200`
- [ ] `JIRA_FIXTURES=captured pytest -v` — 사외 스위트를 실데이터로 재실행
- [ ] `cli sync --instance SITE_A --dry-run`
- [ ] `cli sync --instance SITE_A` — **모든 SQL이 처음 실행되는 순간**
- [ ] `cli sync --instance SITE_A` 2회차 — 행 수 불변
- [ ] `cli sync --instance SITE_A` 3회차 — 행 수 불변
- [ ] 이슈 1건 상태 변경 후 재동기화 — changelog 신규 행 + 구간 분할 확인

### 멱등성 확인 쿼리 (2·3회차에서 값이 같아야 한다)

```sql
SELECT 'issue'   t, COUNT(*) c FROM TEST_JIRA_ISSUE          UNION ALL
SELECT 'eav',       COUNT(*)   FROM TEST_ISSUE_FIELD_VALUE   UNION ALL
SELECT 'changelog', COUNT(*)   FROM TEST_ISSUE_CHANGELOG     UNION ALL
SELECT 'history',   COUNT(*)   FROM TEST_ISSUE_FIELD_HISTORY;
```

**첫 `sync`가 이 프로젝트에서 가장 위험한 순간이다.** 사외에 DB가 없었으므로 모든 `MERGE`·`INSERT`가 여기서 처음 실행된다. 실패하면 대개 셋 중 하나다.

| 증상 | 원인 | 대응 |
|---|---|---|
| `ORA-00904: invalid identifier` | 컬럼명 오타 | 정적 대조를 통과했다면 T3 파서가 놓친 케이스다. `schema_map.py`를 고치고 테스트를 추가한다 |
| `ORA-00001: unique constraint violated` | MERGE의 `ON` 절 키가 잘못됨 | 그 테이블의 UNIQUE 제약과 `ON` 절을 대조한다 |
| `ORA-02291: integrity constraint` | 적재 순서가 FK를 위반 | spec §5.2의 6단계 순서를 확인한다 (T7 Step 1의 순서 테스트가 통과했는지도) |

**FAIL이 나오면 사외로 돌아가 고치고 다시 반입한다.** 사내에서 즉석 수정하면 사외 테스트와 어긋나고, 단방향 반입이라 되돌리기 어렵다.
