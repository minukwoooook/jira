BEGIN
  -- 1) 테이블 (FK 무시하고 순서 상관없이)
  FOR r IN (SELECT table_name FROM user_tables WHERE table_name LIKE 'TEST\_%' ESCAPE '\')
  LOOP
    EXECUTE IMMEDIATE 'DROP TABLE "' || r.table_name || '" CASCADE CONSTRAINTS PURGE';
  END LOOP;

  -- 2) 뷰
  FOR r IN (SELECT view_name FROM user_views WHERE view_name LIKE 'TEST\_%' ESCAPE '\')
  LOOP
    EXECUTE IMMEDIATE 'DROP VIEW "' || r.view_name || '"';
  END LOOP;

  -- 3) 시퀀스
  FOR r IN (SELECT sequence_name FROM user_sequences
            WHERE sequence_name LIKE 'TEST\_%' ESCAPE '\')
  LOOP
    EXECUTE IMMEDIATE 'DROP SEQUENCE "' || r.sequence_name || '"';
  END LOOP;
END;
/
