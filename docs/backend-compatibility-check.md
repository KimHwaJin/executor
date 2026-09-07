# PostgreSQL / Redis 단일 명령 호환성 점검

실행할 서비스와 **같은 계정·연결 주소**로 현재 Executor 코드가 PostgreSQL과
Redis를 사용할 수 있는지 점검한다. Jupyter, Agent, 실행 중인 Executor API 서버는
필요 없다. Redis 6.0.8용 별도 구현이 아니라 **현재 서비스의 구현을 직접 호출**한다.

## 실행

저장소를 클론하고 프로젝트 Python 환경을 준비한 뒤 루트에서 실행한다.
기존 `.env` 또는 프로세스 환경변수에 `DATABASE_URL`, `REDIS_URL`을 반드시 지정한다.
실수로 기본 localhost 설정을 검사하지 않도록 두 값이 없으면 실패한다.

```dotenv
DATABASE_URL=postgresql+psycopg://<user>:<password>@<host>:5432/<database>
REDIS_URL=redis://<user>:<password>@<host>:6379/<db-number>
```

Redis ACL 사용자 없이 비밀번호만 쓰면 `redis://:<password>@<host>:6379/0`이다.
사용자명·비밀번호의 `@`, `:`, `/`, `#` 등은 URL 인코딩한다.
TLS 환경에서는 기존 `sslmode` 등 PostgreSQL 연결 옵션과 `rediss://`를 유지한다.
비밀번호가 들어간 `.env`와 실제 주소는 Git에 올리지 않는다.

```bash
uv run python scripts/backend_compatibility_check.py
```

JSON 결과도 남기려면:

```bash
uv run python scripts/backend_compatibility_check.py --report /tmp/backend-check.json
```

선택 옵션:

- `--env-file /path/to/check.env`: 다른 설정 파일. 환경변수가 파일보다 우선한다.
- `--service-schema public`: 현재 서비스 테이블이 설치된 스키마.
- `--redis-prefix executor:compatibility`: 점검 키 접두사. 항상 고유 UUID가 추가된다.
- `--report /path/to/report.json`: 결과 파일. 상위 디렉토리는 미리 준비한다.

연결 문자열은 명령행 인자로 받지 않는다. 콘솔/JSON에는 비밀번호나 SQL 파라미터를
출력하지 않고 서버 버전, 단계별 성공/실패, 테스트 ID, 이벤트 종류·순번을 남긴다.
스크립트는 `scripts/backend_compatibility_flow.py`를 함께 사용하므로 단독 파일만
복사하지 말고 저장소에서 실행한다. 서비스 코드·의존성도 같은 리비전이어야 한다.

## 실제로 확인하는 내용

1. **기존 DB 읽기 전용 검사**: 접속, 서버 버전, 현재 Alembic HEAD 일치,
   ORM에서 요구하는 테이블·컬럼 존재. 같은 revision인데 컬럼이 빠진 경우도 실패한다.
2. **Redis 실동작**: PING, INFO, COMMAND INFO, EVAL 및 Lua 안에서 필요한 명령 권한.
   Streams 그룹 생성, 쓰기, 수신, ACK, Pending 회수, 본문이 삭제된 Pending 정리,
   미처리·미수신 작업 보호 및 보존기간 삭제를 실제 서비스 함수로 확인한다.
3. **격리 DB 초기화**: 같은 DB 안에 임시 스키마를 만들고 실제 Alembic 마이그레이션을
   처음부터 실행한다. 운영 스키마로 fallback하지 않는 독립 `search_path`를 사용한다.
4. **실제 서비스 처리 한 바퀴**:
   `submit → DB/Outbox → Redis work → Worker 수신/ACK → cancel → DB CANCELLED
   → Outbox → Redis events → 외부 소비자 수신/ACK`.
   Submit/cancel 멱등성, Operation/Step 상태, 이벤트 ID·내용·순번과 DB 원본의 일치도
   검증한다. 런타임을 등록하지 않아 최초 작업은 QUEUED로 남고, 해당 작업을 취소한다.
5. **잘못된 메시지 및 정리**: 실제 Worker의 DLQ 저장 후 ACK, 보존기간 관리자의
   DB lease와 정리 SQL, 최신 이력 보호 및 만료된 테스트 이력 삭제까지 검증한다.
6. 성공/실패와 무관하게 **이번 실행에서 만든 스키마·Redis 키·로컬 임시 파일만 정리**한다.

Redis 명령 범위는 PING, INFO, COMMAND INFO, EVAL, EXISTS, XGROUP CREATE,
XADD, XREADGROUP, XPENDING, XCLAIM, XRANGE, XACK, XINFO GROUPS, XDEL이다.
진단 데이터 정리를 위해 서비스 명령 외에 DEL 권한도 필요하다.
실제 `claim_pending` / `trim_before` Lua를 실행하며 `XAUTOCLAIM`은 사용하지 않는다.

## 안전 범위와 필요한 권한

- 현재 서비스 스키마에는 읽기 전용 트랜잭션만 사용한다. 운영 테이블 수정·초기화,
  자동 업그레이드, FLUSHDB/FLUSHALL, 운영 Stream 소비는 하지 않는다.
- 점검용 스키마는 `executor_probe_<UUID>`, Redis 키는
  `<접두사>:<UUID>:{work,events,dlq,events-dlq,recovery}`다. UUID가 충돌하거나 기존 키가
  있으면 재사용/삭제하지 않는다. 서비스 설정의 실제 Stream 이름은 사용하지 않는다.
- **DB를 새로 생성하지 않으므로 CREATEDB는 불필요**하다. 단 해당 DB에서 임시 스키마를
  만들 CREATE 권한 및 본인 스키마의 테이블/인덱스 생성·삭제 권한이 필요하다.
  이 진단용 DDL 권한 부족과 서비스의 일반 DML 호환성 문제는 구분해야 한다.
  운영 정책상 허용되지 않으면 DBA와 점검 환경을 협의한다. 권한 없이 우회하지 않는다.
- 마이그레이션은 서비스와 같은 DB 단위 advisory lock을 짧게 사용한다.
  배포 마이그레이션과 동시에 실행하지 않는다. SQL/연결/단계별 timeout이 있다.
  기존 `DATABASE_URL`의 `options`는 테스트 격리·timeout 강제를 위해 대체한다.
- Redis는 같은 계정으로 점검 접두사에 접근 가능해야 한다. 관리 명령으로 ACL을 바꾸지
  않는다. ACL이 `executor.*`만 허용한다면 관리자가 허용한 별도 접두사를 협의한다.
- 정리 실패도 전체 FAIL이다. 프로세스 강제 종료(SIGKILL), 머신 종료, 연결 단절로
  정리가 안 되면 콘솔/JSON의 **정확한 테스트 스키마·접두사**를 기준으로 잔여물을 확인한다.
  UUID를 모르는 상태에서 광범위한 삭제를 하지 않는다.

## 결과 해석

전체 통과 시 마지막에 `PASS: PostgreSQL / Redis compatibility check`, 종료 코드 `0`.
하나라도 실패하거나 중단/정리가 실패하면 종료 코드 `1`이다. 최초 실패 후 나머지
본 점검은 진행하지 않고 정리한다. 정리 PASS가 전체 점검 PASS를 의미하지 않는다.

| 실패 단계 | 먼저 확인할 것 |
| --- | --- |
| configuration | 환경변수/설정 파싱, URL 형식, 로컬 임시 디렉토리 쓰기 |
| service schema | 접속 주소·인증·TLS, 지정 스키마, Alembic revision, 빠진 테이블/컬럼 |
| Redis commands / Lua | Redis 6.0.8 이상, 인증, EVAL과 Lua 내부 명령의 ACL, 테스트 키 ACL |
| isolated schema / migration | DB CREATE 권한, 마이그레이션 파일, 다른 배포가 잡은 lock |
| submit / Outbox / consume | DB 저장, Redis 발행·ACK 권한, Pending/DLQ, 이벤트 계약 불일치 |
| cleanup | 연결 복구, DEL/스키마 삭제 권한, 출력된 테스트 대상의 잔여 여부 |

예를 들어 서비스 DB가 `0004`, 코드 HEAD가 `0005`이면 FAIL로 알려주며
**자동으로 0005로 올리지 않는다**. 별도의 승인된 배포/마이그레이션 절차를 따른다.

## 이 결과가 보장하지 않는 것

- 코드 실행 성공/실패·재시도, SINGLE/MULTI의 모든 시나리오, Jupyter 연결 및 출력,
  공유 PV, HTTP/MCP·BFF 연결은 별도 E2E 범위다. 테스트 Execution의 CANCELLED는 정상이다.
- 모든 SQL 분기, 기존 DB의 전체 제약/인덱스/타입 동일성, 운영 테이블별 쓰기 권한,
  RLS 정책, 실제 운영 Stream 접두사별 ACL, 다중 Worker 경쟁·부하·5일 장기 실행까지
  보장하지 않는다. 운영 스키마/키를 변경하지 않기 때문에 생기는 의도적인 한계다.
- **기능 호환성 통과는 Redis 6.0.8의 보안 패치 적합성이나 프로덕션 준비 완료 판정이 아니다.**

## 개발 검증

```bash
uv run pytest tests/test_backend_compatibility_check.py
```

실제 통합 테스트까지 실행하려면 로컬 테스트 DB/Redis를 대상으로
`EXECUTOR_COMPAT_TEST_DATABASE_URL`, `EXECUTOR_REDIS_TEST_URL`을 설정한다.
설정하지 않으면 통합 항목은 skip된다. `EXECUTOR_COMPAT_TEST_ACL=1`은 **로컬 테스트에서만**
임시 Redis ACL 사용자를 만들어 EVAL/XINFO/XADD/DEL 거부를 확인하고 삭제하는 추가 검증이다.
배포 담당자용 스크립트 자체는 ACL 사용자를 생성하지 않는다.

초기 구현 검증에서는 로컬 Redis 6.0.8 / PostgreSQL 17의 임시 스키마에서
단일 CLI 실행, 전체 왕복, ACL 거부, 중단 후 정리, 기존 키 보호, 병렬 실행 격리,
마이그레이션 불일치·누락 컬럼 검출, 최신 스키마의 `alembic check`를 포함해
22개 테스트가 통과했다. 기존 서비스 DB의 revision `0004` 불일치는 탐지했으며,
해당 DB를 자동 수정하거나 초기화하지 않았다.
