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
