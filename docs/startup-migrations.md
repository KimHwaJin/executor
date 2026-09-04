# 앱 시작 시 Alembic 자동 적용

## 설정

| 환경변수 | 코드 기본값 | 의미 |
|---|---|---|
| `DB_AUTO_MIGRATE` | `false` | true면 기동 중 `upgrade head` 수행 |
| `DB_MIGRATIONS_PATH` | `migrations` | 프로세스 작업 디렉토리 기준 경로. 이미지에서는 `/app/migrations` |
| `DB_MIGRATION_LOCK_TIMEOUT_SECONDS` | `60` | 마이그레이션/DDL 잠금 대기 제한 |
| `DB_MIGRATION_STATEMENT_TIMEOUT_SECONDS` | `300` | SQL 문 하나의 실행시간 제한. 전체 기동 제한이 아님 |

`.env.example`, Compose, Kubernetes Deployment 예시는 자동 적용을 켠다.
기존 `.env`에 false가 있으면 Compose 기본값보다 우선하므로 true로 변경해야 한다.
false일 때는 기존처럼 앱 실행 전에 `uv run alembic upgrade head`를 수행한다.
이미 최신 head이면 스키마 변경 없이 진행한다. DB와 사용자를 생성하는 기능은 아니다.

기존 로컬 `.env`에는 `DB_AUTO_MIGRATE=true`를 추가하면 된다. PowerShell에서
일회성으로 설정하려면 `$env:DB_AUTO_MIGRATE="true"` 후 `uv run executor-service`를 실행한다.
DB/Redis/PVC 관련 기존 설정은 그대로 필요하다.

## 시작 순서와 안전장치

설정·로깅 → 마이그레이션 → maintenance/retention DB 초기화 → Outbox/Worker → HTTP 수신.
FastAPI lifespan에서 수행하므로 공식 실행 명령과 직접 ASGI로 실행할 때 모두 적용된다.
Windows에서도 이미 설정된 psycopg 호환 이벤트 루프를 재사용하고 중첩 `asyncio.run`을 하지 않는다.

동일 DB의 자동 실행과 새 CLI 모두 고정 PostgreSQL advisory transaction lock을 사용한다.
Alembic의 프로세스 전역 환경도 동일 프로세스 내 비동기 잠금으로 직렬화한다.
SQL/잠금 실패와 취소 시 트랜잭션을 롤백하고 커넥션을 반환한다. 커밋·롤백·연결 종료 시
DB 잠금은 해제된다. DB 연결 시간 제한은 `DATABASE_CONNECT_TIMEOUT_SECONDS`로 별도 관리한다.

실패하면 API/Worker를 시작하지 않고 오류 유형과 SQLSTATE를 기록한다. 자격증명,
SQL 파라미터, 임의 예외 메시지는 기동 오류에 노출하지 않는다.
자동 실행은 Alembic `fileConfig`를 건너뛰어 `logger.yml` 설정을 보존한다.

현재 마이그레이션은 PostgreSQL 트랜잭션 안에서 실행한다. 향후 `CREATE INDEX CONCURRENTLY`
같이 autocommit이 필요한 마이그레이션은 그대로 추가하면 안 된다. 트랜잭션 잠금이 풀릴 수
있으므로 별도 운영 절차 또는 잠금 설계 검토가 필요하다. 기존 head `0004`는 변경하지 않는다.

## Deployment만 사용할 수 있는 환경

`deploy/kubernetes/deployment.yaml`의 `env[].value`에 모든 값을 직접 지정한다.
ConfigMap/Secret/별도 migration Job 의존 파일은 제거했다. 실제 값은 CI 또는 Git 제외
`deployment.local.yaml`에 작성하고 저장소/빌드 로그에 남기지 않는다.
Deployment/Pod 조회 권한자는 평문 비밀값을 볼 수 있다는 보안상 제한이 있다.

기본 전략은 replicas=1, Recreate다. 구버전 종료 후 새 프로세스가 마이그레이션한다.
여러 별도 Deployment나 수동 프로세스가 같은 DB를 쓴다면 해당 구버전도 먼저 종료해야 한다.
DB 잠금은 구버전 애플리케이션의 쿼리와 호환성을 보장하지 않는다.
긴 실행은 사전 drain 및 완료 확인 후 배포한다. 강제 취소나 자동 재개는 하지 않는다.

startupProbe는 600초를 허용한다. 실제 마이그레이션의 전체 시간에 맞춰 조정한다.
Service/라우팅, PVC, PostgreSQL, Redis는 플랫폼에 미리 준비되어 있어야 한다.
Pod 내부에서 Kubernetes API를 호출하지 않는다. 상세 절차는
[Kubernetes README](../deploy/kubernetes/README.md)를 참고한다.

## 검증 범위

- true/false 설정 및 Worker/DB 초기화보다 먼저 실행되는지 확인.
- 실패 시 HTTP 기동 중단과 컨테이너 리소스 정리.
- 격리 PostgreSQL에서 빈 DB 초기화, 반복 실행, 다중 프로세스 경합.
- 잠금 시간초과, 취소 후 재기동, 실패 롤백 및 오류 정보 마스킹.
- Deployment의 inline 값, Recreate 전략, startupProbe 예산, 기존 외부 설정 참조 제거.

### 확인 결과

- 기본 테스트 548개 통과, 환경 의존 Docker 다운로드/Linux UID 테스트 4개 제외.
- 실제 Redis 테스트 10개, 격리 PostgreSQL/마이그레이션 테스트 30개 통과.
  자동 실행끼리뿐 아니라 자동 실행과 수동 CLI 경합도 검증했다.
- Ruff lint/format, ty, Compose config 및 Docker 이미지 빌드 통과.
- 빌드된 이미지에서 마이그레이션 모듈·`/app/migrations/env.py`·`logger.yml` 확인.
- 실제 Kubernetes 배포, Windows 머신 기동은 이번 환경에서 수행하지 않았다.
  로컬 운영 DB와 실행 중인 서비스도 이 변경 검증 과정에서 수정하지 않았다.
