# 실행별 메모리 상한, 커널 환경변수, 메모리 기준 대기

공유 Jupyter 서버 하나에서 여러 사용자의 Execution이 돌 때, 한 실행이 메모리를 다 써서
다른 실행까지 같이 죽는 일을 막기 위한 기능이다. 모든 기능은 설정하거나 요청에 넣을 때만
동작하며 기본값은 꺼짐이다.

| 기능 | 켜는 방법 | 꺼진 상태의 동작 |
|---|---|---|
| 실행별 메모리 상한 | `EXECUTION_DEFAULT_MEMORY_LIMIT_BYTES` 또는 요청의 `runtime.memory_limit_bytes` | 커널에 상한 없음 (이전과 같음) |
| 실행별 커널 환경변수 | 요청의 `runtime.env` | 커널은 Jupyter 서버의 환경만 받음 |
| 유저별 총량 쿼터 | `EXECUTION_USER_MEMORY_QUOTA_BYTES` | 유저별 제한 없음 |
| Jupyter 메모리 기준 대기 | `RUNTIME_MEMORY_OVERCOMMIT_RATIO` | 슬롯 수와 `RUNTIME_MEMORY_ADMISSION_LIMIT`만 적용 |

## 실행별 메모리 상한

Jupyter 이미지의 kernelspec은 `executor_resource_extension.kernel_launcher`를 거쳐 커널을
시작한다. Executor가 상한을 넘기면 런처가 커널 프로세스의 `RLIMIT_AS`(가상 메모리 상한)를
걸고 커널로 바뀐다. 상한을 넘는 할당은 그 실행 안에서 `MemoryError`로 실패하고, 커널과 다른
실행은 살아 있다. 실패한 Step은 기존과 같이 `TOOL_ERROR`이며 오류 메시지가
`MemoryError: ...`로 시작한다.

- `RLIMIT_DATA`가 아니라 `RLIMIT_AS`를 쓴다. Linux 4.7 미만(예: 3.10 노드 커널)은 큰 배열이
  쓰는 mmap 할당을 `RLIMIT_DATA`에 세지 않기 때문이다.
- 상한이 걸린 커널은 `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS`를
  `EXECUTOR_KERNEL_THREADS`(기본 4)로, `MALLOC_ARENA_MAX`를 2로 고정한다. 요청 env나
  서버 env가 이미 값을 주면 그 값이 이긴다. BLAS 스레드는 Pod의 CPU limit이 아니라 노드의
  코어 수만큼 생기고 스레드마다 가상 메모리를 잡아서, 고정하지 않으면 48코어 노드에서
  numpy import만으로 2GiB 상한을 채운다.
- 상한과 무관하게 모든 커널의 `oom_score_adj`를 1000으로 올린다. 그래도 Pod 메모리가 차면
  OOM killer가 Jupyter 서버 대신 커널을 고른다.
- 커널은 실제 사용량보다 약 0.5GiB 큰 주소 공간을 잡는다(4스레드 기준 실측: 빈 커널
  VmSize 287MiB/RSS 62MiB, 300MB 할당 후 VmSize 651MiB/RSS 362MiB). 실제로 N GiB가 필요한
  작업에는 N + 0.5GiB 정도를 준다.
- `multiprocessing`/`joblib`의 자식 프로세스는 상한을 물려받지만 프로세스마다 따로 센다.
  4개로 나누면 최대 4배까지 쓸 수 있다. 상한은 "이 프로세스 하나"의 제한이다.

| 설정 | 의미 |
|---|---|
| `EXECUTION_DEFAULT_MEMORY_LIMIT_BYTES` | 요청에 값이 없을 때 쓰는 상한. 비우면 상한 없음 |
| `EXECUTION_MAX_MEMORY_LIMIT_BYTES` | 요청이 줄 수 있는 최대값. 넘으면 제출이 400 `INVALID_EXECUTION_SPEC`으로 거절 |

요청 필드는 `runtime.memory_limit_bytes`(최소 256MiB)이다. 조회 API의 `runtime`에 실제
적용된 값이 나온다.

## 실행별 커널 환경변수

`runtime.env`에 넣은 값은 그 Execution의 커널에만 들어간다. 코드에서는
`os.environ["S3_SECRET_KEY"]`처럼 읽는다.

- 이름은 `[A-Z][A-Z0-9_]*`, 최대 64개, 값은 4096자 이하. `PATH`, `HOME`과 `EXECUTOR_`,
  `JPY_`, `JUPYTER`, `LD_`, `MALLOC_`, `PYTHON`으로 시작하는 이름은 런처나 상한을 우회할
  수 있어 거절한다.
- `RUNTIME_CREDENTIAL_KEY`(Runtime Target 자격증명과 같은 Fernet 키)로 암호화해 DB에만
  저장한다. 요청 fingerprint, metadata, Redis 메시지, 이벤트, 조회 응답에는 들어가지 않는다.
  `REST_ERROR_BODY_LOGGING_ENABLED` 로그에서도 값은 `***`로 바뀐다.
- 더 이상 재시도로 새 커널을 띄울 수 없게 되면(성공, 재시도 불가 실패, 취소) 암호문을
  지운다. 재시도할 수 있는 실패는 재시도 커널을 위해 남는다.
- 코드가 값을 `print`해도 Jupyter의 실행 기록 단계에서 `***`로 바뀐다(4자 미만 값 제외).
  이미지·파일·`repr` 변형까지 막지는 않는다.
- 같은 Jupyter 서버의 커널은 같은 OS 사용자로 돌기 때문에, 작정한 코드는 다른 커널의
  `/proc/<pid>/environ`을 읽을 수 있다. 우발적 노출을 막는 장치이고 악의적인 사용자 간
  격리는 아니다.

같은 `idempotency_key`로 env만 바꿔 다시 보내면 기존 Execution이 그대로 반환된다(env는
fingerprint에 들어가지 않는다).

## 메모리 기준 대기

두 설정 모두 "실행 중이거나 슬롯을 잡고 있는 Execution"(기존 슬롯 예약과 같은 기준)의
`memory_limit_bytes` 합으로 계산한다. 실제 사용량을 폴링하지 않는다. 들어갈 자리가 없으면
Execution은 `QUEUED`로 남고, 다른 실행이 끝나 합이 줄면 Worker가 다시 집어 간다. 기존 슬롯
제한(`max_concurrent_executions`)과 같은 대기 방식이다.

| 설정 | 의미 |
|---|---|
| `EXECUTION_USER_MEMORY_QUOTA_BYTES` | 한 `context.user_id`가 동시에 잡을 수 있는 상한 합. 이것보다 큰 단일 상한은 제출 때 거절 |
| `RUNTIME_MEMORY_OVERCOMMIT_RATIO` | Target에서 상한 합이 `memory_capacity_bytes × 비율`을 넘지 않을 때만 시작. 1보다 크면 과할당 |

- 둘 중 하나라도 쓰면 `EXECUTION_DEFAULT_MEMORY_LIMIT_BYTES`가 필요하다. 상한 없는
  Execution은 합에서 0으로 센다(기능을 켜기 전에 들어온 Execution 포함).
- Target의 `memory_capacity_bytes`는 리소스 probe가 관측한 값이다. 아직 관측이 없으면 그
  Target에는 메모리 기준을 적용하지 않는다.
- `EXECUTION_MAX_MEMORY_LIMIT_BYTES`는 `memory_capacity_bytes × 비율`보다 작게 둔다. 더 큰
  상한의 Execution은 그 Target에서 영원히 대기한다.
- PostgreSQL에서는 유저별 advisory lock으로 서로 다른 Target에 대한 동시 claim이 같은
  여유분을 나눠 갖지 않게 한다.

예: 32GiB Jupyter, 기본 상한 2GiB, 유저 쿼터 10GiB, 비율 1.0이면 한 유저는 2GiB 실행을
최대 5개까지 동시에 돌리고 6번째는 앞의 하나가 끝날 때까지 대기한다. Jupyter 전체로는 상한
합 32GiB까지 시작한다.

## 배포 순서

1. Jupyter 이미지를 먼저 올린다(kernelspec 래핑과 `/executor/kernels`가 들어 있다).
2. Executor를 올린다. 시작할 때 migration `0007`이 `executions`에 nullable 컬럼 두 개를
   추가한다.
3. 필요한 설정을 켠다.

Executor는 상한도 env도 없는 커널을 계속 Jupyter 기본 `/api/kernels`로 시작한다. 기능을
쓰지 않는 동안에는 새 Executor와 이전 Jupyter, 이전 Executor와 새 Jupyter 조합이 모두
동작한다.

## 되돌리기

| 범위 | 방법 |
|---|---|
| 기능만 끄기 | 위 설정을 비우고 재시작. 호출 측은 `runtime.memory_limit_bytes`/`runtime.env`를 보내지 않는다. 이미 저장된 상한·env가 있는 Execution은 그 값으로 끝까지 돈다 |
| Jupyter 이미지만 되돌리기 | 기능을 먼저 끈 뒤 이전 이미지로 배포. 이전 이미지는 `/executor/kernels`가 없어서, 상한이나 env가 있는 Execution은 커널 시작에서 실패한다 |
| Executor 코드 되돌리기 | 이전 코드는 모르는 revision `0007`에서 시작을 거부하므로, 먼저 `alembic downgrade 0006`을 실행한다. 두 컬럼만 지우며 저장된 상한과 env 암호문이 사라진다 |

커밋도 기능별로 나뉘어 있어 필요한 것만 revert할 수 있다: 커널 런처, journal 마스킹,
Executor 저장·전달(migration 포함), 메모리 기준 대기.

## 아직 없는 것

- 실행별 실제 최대 메모리 사용량 기록. 실패 원인은 Step 오류 메시지(`MemoryError`)로만 남는다.
- 커널이 상한 대신 Pod OOM으로 죽은 경우의 별도 실패 유형.
