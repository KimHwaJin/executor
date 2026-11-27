# Table 정의서

<!-- scripts/deliverables/schema.py가 만든다. 손으로 고치지 않는다. -->

| 항목 | 내용 |
|---|---|
| 시스템 | Executor (코드 실행 서비스) |
| 산출물 | 6. Table 정의서 |
| DBMS | PostgreSQL |
| 스키마 리비전(Alembic) | `0017` |
| Vector DB | 사용하지 않음 |

테이블 20개의 컬럼, 제약, 인덱스를 ORM 모델에서 뽑았다. 제출본은 같은 내용의 `dist/06-Table-정의서.xlsx`다.

## 1. 테이블 목록

| No | 테이블 ID | 테이블명 | 업무 영역 | 1건이 뜻하는 것 | 컬럼 수 | 정리 정책 |
|---|---|---|---|---|---|---|
| 1 | `executions` | 실행 | 실행 요청 | 호출자의 실행 요청 1건 | 54 | 없음 |
| 2 | `execution_operations` | 오퍼레이션 | 실행 요청 | 한 번에 제출된 Step 묶음 | 22 | 없음 |
| 3 | `execution_steps` | 스텝 | 실행 요청 | Step(노트북 셀 하나)의 현재 상태 | 33 | 없음 |
| 4 | `execution_attempts` | 실행 시도 | 실행 시도 | Worker의 claim 1회 | 25 | 없음 |
| 5 | `execution_step_attempts` | 스텝 시도 | 실행 시도 | Step 실행 시도 1회와 그 결과 위치 | 28 | 없음 |
| 6 | `execution_retries` | 재시도 요청 | 실행 시도 | 호출자의 재시도 요청 | 5 | 없음 |
| 7 | `execution_step_runs` | 스텝 저널 요청 | 인계 | Step 시도의 Jupyter 저널 요청 | 6 | 없음 |
| 8 | `execution_handovers` | 인계 대기 | 인계 | 다른 Worker를 기다리는 인계 표시 | 4 | 인수 시 삭제 |
| 9 | `execution_artifacts` | 산출물 | 결과 | 작업공간 산출물 파일 등록 | 25 | 없음 |
| 10 | `execution_diagnostics` | 진단 | 결과 | 실패·누락 관찰 기록 | 15 | 없음 |
| 11 | `execution_event_sequences` | 이벤트 순번 | 이벤트 | 실행별 이벤트 순번 카운터 | 2 | 없음 |
| 12 | `execution_events` | 실행 이벤트 | 이벤트 | 실행 타임라인의 이벤트 1건 | 12 | 종료 후 90일 |
| 13 | `outbox_events` | 발행 대기열 | 이벤트 | Redis 발행 대기·완료 1건 | 17 | 발행 후 7일 |
| 14 | `runtime_targets` | Runtime Target | Runtime | 등록된 Jupyter 서버 | 35 | 수동 |
| 15 | `runtime_target_purges` | 삭제된 Target | Runtime | 삭제된 Target의 흔적 | 12 | 없음 |
| 16 | `executor_maintenance` | 접수 상태 | 운영 | 전체 접수 상태(항상 1행) | 9 | - |
| 17 | `maintenance_runs` | 일괄 중지 | 운영 | 실행 일괄 중지 요청 | 18 | 없음 |
| 18 | `maintenance_run_targets` | 일괄 중지 대상 | 운영 | 일괄 중지 대상 실행 | 14 | 없음 |
| 19 | `event_retention_lease` | 주기 작업 lease | 운영 | 주기 작업 단일 실행 표시(작업 종류당 1행) | 8 | - |
| 20 | `command_receipts` | 명령 영수증 | 멱등 | 관리 명령·Artifact 생성의 멱등 응답 | 7 | 없음 |

## 2. 테이블별 정의

### 2.1 executions (실행)

실행 요청 영역. 호출자의 실행 요청 1건. 정리 정책: 없음

| No | 컬럼 ID | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|---|
| 1 | `id` | UUID | N | PK | `앱 생성` | 식별자 |
| 2 | `idempotency_key` | VARCHAR(255) | N | UK |  | 제출 멱등 키. 테넌트 안에서 유일 |
| 3 | `request_fingerprint` | VARCHAR(64) | N |  |  | 요청 본문 지문(sha256). 같은 키로 다른 요청이 오면 충돌 |
| 4 | `cancel_idempotency_key` | VARCHAR(255) | Y |  |  | 취소 요청 멱등 키 |
| 5 | `status` | VARCHAR(32) | N |  |  | Execution 상태 |
| 6 | `operation_mode` | VARCHAR(32) | N |  |  | SINGLE / MULTI |
| 7 | `operation_wait_timeout_seconds` | INTEGER | Y |  |  | MULTI가 다음 Operation을 기다리는 최대 초(30 이상) |
| 8 | `trigger_type` | VARCHAR(32) | N |  |  | INTERACTIVE / BATCH. 풀 선택에 쓰인다 |
| 9 | `runtime_type` | VARCHAR(32) | N |  | `JUPYTER` | Runtime 종류(JUPYTER) |
| 10 | `runtime_pool` | VARCHAR(32) | N |  |  | 배정 풀. trigger_type과 같은 값 |
| 11 | `runtime_profile` | VARCHAR(128) | N |  |  | 커널 프로필(kernelspec). RUNTIME_ALLOWED_PROFILES 중 하나 |
| 12 | `user_id` | VARCHAR(255) | N |  |  | 요청 사용자(X-User-Id) |
| 13 | `project_id` | VARCHAR(255) | Y |  |  | 요청 프로젝트(X-Project-Id) |
| 14 | `session_id` | VARCHAR(255) | Y |  |  | 호출자 대화 세션(X-Session-Id). 커널 세션과 무관 |
| 15 | `tenant_id` | VARCHAR(64) | N | UK |  | 테넌트. 이벤트 Stream과 작업공간 경로를 나눈다 |
| 16 | `metadata` | JSON | N |  | `앱 생성` | 호출자가 넣은 임의 JSON |
| 17 | `cancellation_reason` | TEXT | Y |  |  | 취소 사유 |
| 18 | `cancellation_lease_owner` | VARCHAR(255) | Y |  |  | 취소 처리 중인 Worker |
| 19 | `cancellation_lease_expires_at` | TIMESTAMPTZ | Y |  |  | 취소 lease 만료 시각 |
| 20 | `runtime_target_id` | UUID | Y |  |  | 배정된 Target. FK 없음(삭제된 Target은 runtime_target_purges로 해석) |
| 21 | `runtime_session_id` | VARCHAR(255) | Y |  |  | Jupyter 커널 세션 ID |
| 22 | `workspace_path` | TEXT | Y |  |  | Jupyter 작업공간 상대경로 |
| 23 | `notebook_path` | TEXT | Y |  |  | 실행 노트북 상대경로 |
| 24 | `notebook_projection_status` | VARCHAR(16) | N |  | `NOT_STARTED` | 노트북 반영 상태 |
| 25 | `notebook_projection_attempt_count` | INTEGER | N |  | `0` | 노트북 반영 시도 횟수 |
| 26 | `notebook_projection_error` | TEXT | Y |  |  | 노트북 반영 오류 |
| 27 | `notebook_projected_at` | TIMESTAMPTZ | Y |  |  | 노트북 반영 시각 |
| 28 | `error_message` | TEXT | Y |  |  | 마지막 오류 요약 |
| 29 | `failure_type` | VARCHAR(32) | Y |  |  | 실패 원인 분류 |
| 30 | `lease_owner` | VARCHAR(255) | Y |  |  | 실행 소유 Worker ID |
| 31 | `lease_expires_at` | TIMESTAMPTZ | Y |  |  | 실행 lease 만료 시각 |
| 32 | `heartbeat_at` | TIMESTAMPTZ | Y |  |  | 마지막 lease 갱신 |
| 33 | `fencing_token` | BIGINT | N |  | `0` | 소유권 세대 번호. claim마다 +1 |
| 34 | `retry_strategy` | VARCHAR(32) | N |  | `NOT_RETRYABLE` | 가능한 재시도 방식 |
| 35 | `retry_from_sequence` | INTEGER | Y |  |  | FROM_FAILED_STEP 재시도 시작 Step |
| 36 | `retained_runtime_session_until` | TIMESTAMPTZ | Y |  |  | 재시도용 실패 세션 보존 기한 |
| 37 | `retry_count` | INTEGER | N |  | `0` | 재시도 횟수 |
| 38 | `recovery_count` | INTEGER | N |  | `0` | Worker 장애 회수 횟수 |
| 39 | `runtime_session_cleanup_status` | VARCHAR(32) | N |  | `NOT_REQUIRED` | 커널 세션 정리 상태 |
| 40 | `runtime_abort_status` | VARCHAR(32) | N |  | `NOT_REQUIRED` | 시간 초과 등으로 중단한 결과 |
| 41 | `finalization_requested` | BOOLEAN | N |  | `False` | MULTI finalize 요청 여부 |
| 42 | `finalized_by` | VARCHAR(32) | Y |  |  | 종료 주체(AGENT, WAIT_TIMEOUT, MAX_RUNTIME) |
| 43 | `active_operation_id` | UUID | Y |  |  | 지금 처리 중이거나 대기 중인 Operation |
| 44 | `operation_wait_expires_at` | TIMESTAMPTZ | Y |  |  | MULTI 다음 Operation 대기 기한 |
| 45 | `execution_expires_at` | TIMESTAMPTZ | Y |  |  | 최대 수명 기한(첫 시작 + 7일) |
| 46 | `version` | INTEGER | N |  | `0` | 낙관적 동시성 버전 |
| 47 | `created_by_client` | VARCHAR(64) | Y |  |  | 행을 만든 호출 클라이언트 |
| 48 | `created_by` | VARCHAR(255) | Y |  |  | 행을 만든 사용자 |
| 49 | `updated_by_client` | VARCHAR(64) | Y |  |  | 마지막으로 고친 호출 클라이언트 |
| 50 | `updated_by` | VARCHAR(255) | Y |  |  | 마지막으로 고친 사용자 |
| 51 | `created_at` | TIMESTAMPTZ | N |  | `앱 생성` | 생성 시각 |
| 52 | `updated_at` | TIMESTAMPTZ | N |  | `앱 생성` | 마지막 변경 시각 |
| 53 | `started_at` | TIMESTAMPTZ | Y |  |  | 첫 시작 |
| 54 | `finished_at` | TIMESTAMPTZ | Y |  |  | 종료 |

| No | 제약·인덱스 이름 | 종류 | 컬럼 | 내용 |
|---|---|---|---|---|
| 1 | `ck_executions_non_negative_fencing_token` | CHECK |  | `fencing_token >= 0` |
| 2 | `ck_executions_non_negative_notebook_projection_attempt_count` | CHECK |  | `notebook_projection_attempt_count >= 0` |
| 3 | `ck_executions_non_negative_recovery_count` | CHECK |  | `recovery_count >= 0` |
| 4 | `ck_executions_non_negative_retry_count` | CHECK |  | `retry_count >= 0` |
| 5 | `ck_executions_non_negative_retry_from_sequence` | CHECK |  | `retry_from_sequence IS NULL OR retry_from_sequence >= 0` |
| 6 | `ck_executions_valid_execution_status` | CHECK |  | `status IN ('QUEUED', 'RUNNING', 'WAITING_FOR_OPERATION', 'FINALIZING', 'CANCEL_REQUESTED', 'CANCELLED', 'SUCCEEDED', 'FAILED')` |
| 7 | `ck_executions_valid_failure_type` | CHECK |  | `failure_type IS NULL OR failure_type IN ('TOOL_ERROR', 'INFRASTRUCTURE_ERROR', 'WORKER_SHUTDOWN', 'RUNTIME_UNAVAILABLE', 'LEASE_EXPIRED', 'INTERNAL_ERROR', 'OPERATION_WAIT_TIMEOUT', 'OPERATION_TIMEOUT', 'STEP_TIMEOUT', 'EXECUTION_TIMEOUT', 'QUEUE_TIMEOUT', 'OUTPUT_LIMIT_EXCEEDED', 'RUNTIME_SESSION_LOST', 'COMPLETION_FAILED')` |
| 8 | `ck_executions_valid_finalized_by` | CHECK |  | `finalized_by IS NULL OR finalized_by IN ('AGENT', 'WAIT_TIMEOUT', 'MAX_RUNTIME')` |
| 9 | `ck_executions_valid_notebook_projection_status` | CHECK |  | `notebook_projection_status IN ('NOT_STARTED', 'PENDING', 'SUCCEEDED', 'FAILED')` |
| 10 | `ck_executions_valid_operation_mode` | CHECK |  | `operation_mode IN ('SINGLE', 'MULTI')` |
| 11 | `ck_executions_valid_operation_wait_timeout` | CHECK |  | `(operation_mode = 'SINGLE' AND operation_wait_timeout_seconds IS NULL) OR (operation_mode = 'MULTI' AND operation_wait_timeout_seconds >= 30)` |
| 12 | `ck_executions_valid_retry_strategy` | CHECK |  | `retry_strategy IN ('NOT_RETRYABLE', 'FROM_FAILED_STEP', 'FROM_START')` |
| 13 | `ck_executions_valid_runtime_abort_status` | CHECK |  | `runtime_abort_status IN ('NOT_REQUIRED', 'PENDING', 'IDLE_CONFIRMED', 'SESSION_DELETED', 'SESSION_MISSING', 'FAILED')` |
| 14 | `ck_executions_valid_runtime_pool` | CHECK |  | `runtime_pool IN ('INTERACTIVE', 'BATCH')` |
| 15 | `ck_executions_valid_runtime_session_cleanup_status` | CHECK |  | `runtime_session_cleanup_status IN ('NOT_REQUIRED', 'PENDING', 'SUCCEEDED', 'FAILED')` |
| 16 | `ck_executions_valid_runtime_type` | CHECK |  | `runtime_type IN ('JUPYTER')` |
| 17 | `ck_executions_valid_trigger_type` | CHECK |  | `trigger_type IN ('INTERACTIVE', 'BATCH')` |
| 18 | `uq_executions_tenant_idempotency_key` | UNIQUE | tenant_id, idempotency_key |  |
| 19 | `ix_executions_active_operation_id` | INDEX | active_operation_id |  |
| 20 | `ix_executions_cancellation_lease` | INDEX | status, cancellation_lease_expires_at |  |
| 21 | `ix_executions_created_cursor` | INDEX | created_at, id |  |
| 22 | `ix_executions_execution_expires_at` | INDEX | execution_expires_at |  |
| 23 | `ix_executions_lease` | INDEX | status, lease_expires_at |  |
| 24 | `ix_executions_operation_wait_expires_at` | INDEX | operation_wait_expires_at |  |
| 25 | `ix_executions_project_created_cursor` | INDEX | project_id, created_at, id |  |
| 26 | `ix_executions_retained_session_cleanup` | INDEX | status, retry_strategy, retained_runtime_session_until |  |
| 27 | `ix_executions_runtime_target_id` | INDEX | runtime_target_id |  |
| 28 | `ix_executions_session_created_cursor` | INDEX | session_id, created_at, id |  |
| 29 | `ix_executions_status_created_at` | INDEX | status, created_at, id |  |
| 30 | `ix_executions_tenant_created_cursor` | INDEX | tenant_id, created_at, id |  |
| 31 | `ix_executions_user_created_cursor` | INDEX | user_id, created_at, id |  |

### 2.2 execution_operations (오퍼레이션)

실행 요청 영역. 한 번에 제출된 Step 묶음. 정리 정책: 없음

| No | 컬럼 ID | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|---|
| 1 | `id` | UUID | N | PK | `앱 생성` | 식별자 |
| 2 | `execution_id` | UUID | N | FK,UK |  | 소속 Execution |
| 3 | `operation_number` | INTEGER | N | UK |  | Execution 안 순번(1부터) |
| 4 | `schema_version` | VARCHAR(16) | N |  | `1.0` | 요청 스펙 버전(1.0) |
| 5 | `first_sequence` | INTEGER | N |  |  | 첫 Step 순번 |
| 6 | `last_sequence` | INTEGER | N |  |  | 마지막 Step 순번 |
| 7 | `operation_timeout_seconds` | INTEGER | Y |  |  | Operation 시간 한도 |
| 8 | `metadata` | JSON | N |  | `앱 생성` | 호출자 JSON |
| 9 | `idempotency_key` | VARCHAR(255) | N | UK |  | Operation 멱등 키. Execution 안에서 유일 |
| 10 | `request_fingerprint` | VARCHAR(64) | N |  |  | 요청 지문 |
| 11 | `status` | VARCHAR(32) | N |  |  | Operation 상태 |
| 12 | `execution_attempt_id` | UUID | Y | FK |  | 이 Operation을 실행한 Attempt |
| 13 | `error_message` | TEXT | Y |  |  | 오류 요약 |
| 14 | `failure_type` | VARCHAR(32) | Y |  |  | 실패 원인 |
| 15 | `created_by_client` | VARCHAR(64) | Y |  |  | 행을 만든 호출 클라이언트 |
| 16 | `created_by` | VARCHAR(255) | Y |  |  | 행을 만든 사용자 |
| 17 | `updated_by_client` | VARCHAR(64) | Y |  |  | 마지막으로 고친 호출 클라이언트 |
| 18 | `updated_by` | VARCHAR(255) | Y |  |  | 마지막으로 고친 사용자 |
| 19 | `created_at` | TIMESTAMPTZ | N |  | `앱 생성` | 생성 시각 |
| 20 | `updated_at` | TIMESTAMPTZ | N |  | `앱 생성` | 마지막 변경 시각 |
| 21 | `started_at` | TIMESTAMPTZ | Y |  |  | 시작 |
| 22 | `finished_at` | TIMESTAMPTZ | Y |  |  | 종료 |

| No | 제약·인덱스 이름 | 종류 | 컬럼 | 내용 |
|---|---|---|---|---|
| 1 | `ck_execution_operations_non_negative_first_sequence` | CHECK |  | `first_sequence >= 0` |
| 2 | `ck_execution_operations_positive_operation_number` | CHECK |  | `operation_number > 0` |
| 3 | `ck_execution_operations_supported_schema_version` | CHECK |  | `schema_version = '1.0'` |
| 4 | `ck_execution_operations_valid_operation_failure_type` | CHECK |  | `failure_type IS NULL OR failure_type IN ('TOOL_ERROR', 'INFRASTRUCTURE_ERROR', 'WORKER_SHUTDOWN', 'RUNTIME_UNAVAILABLE', 'LEASE_EXPIRED', 'INTERNAL_ERROR', 'OPERATION_WAIT_TIMEOUT', 'OPERATION_TIMEOUT', 'STEP_TIMEOUT', 'EXECUTION_TIMEOUT', 'QUEUE_TIMEOUT', 'OUTPUT_LIMIT_EXCEEDED', 'RUNTIME_SESSION_LOST', 'COMPLETION_FAILED')` |
| 5 | `ck_execution_operations_valid_operation_sequence_range` | CHECK |  | `last_sequence >= first_sequence` |
| 6 | `ck_execution_operations_valid_operation_status` | CHECK |  | `status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')` |
| 7 | `ck_execution_operations_valid_operation_timeout` | CHECK |  | `operation_timeout_seconds IS NULL OR operation_timeout_seconds >= 1` |
| 8 | `fk_execution_operations_execution_attempt_id_execution_attempts` | FK | execution_attempt_id | → execution_attempts (ON DELETE NO ACTION) |
| 9 | `fk_execution_operations_execution_id_executions` | FK | execution_id | → executions (ON DELETE CASCADE) |
| 10 | `uq_execution_operations_execution_idempotency_key` | UNIQUE | execution_id, idempotency_key |  |
| 11 | `uq_operations_execution_number` | UNIQUE | execution_id, operation_number |  |
| 12 | `ix_execution_operations_execution_number` | INDEX | execution_id, operation_number |  |

### 2.3 execution_steps (스텝)

실행 요청 영역. Step(노트북 셀 하나)의 현재 상태. 정리 정책: 없음

| No | 컬럼 ID | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|---|
| 1 | `id` | UUID | N | PK | `앱 생성` | 식별자 |
| 2 | `execution_id` | UUID | N | FK,UK |  | 소속 Execution |
| 3 | `operation_id` | UUID | N | FK |  | 소속 Operation |
| 4 | `sequence` | INTEGER | N | UK |  | Execution 전체에서의 순번(0부터) |
| 5 | `source_type` | VARCHAR(32) | N |  |  | INLINE / PATH |
| 6 | `source_path` | TEXT | Y |  |  | PATH일 때 공유 PV 상대경로 |
| 7 | `source_sha256` | VARCHAR(64) | N |  |  | 원본 코드 sha256 |
| 8 | `source_snapshot_path` | TEXT | N |  |  | Executor 스냅샷 상대경로 |
| 9 | `source_size_bytes` | BIGINT | N |  |  | 스냅샷 크기 |
| 10 | `code_hash` | VARCHAR(64) | Y |  |  | 코드 해시 |
| 11 | `step_timeout_seconds` | INTEGER | Y |  |  | Step 시간 한도 |
| 12 | `skill_name` | VARCHAR(255) | Y |  |  | 호출자 표시용 |
| 13 | `tool_name` | VARCHAR(255) | Y |  |  | 호출자 표시용 |
| 14 | `status` | VARCHAR(32) | N |  | `PENDING` | 현재 Step 상태(최신 Attempt 기준) |
| 15 | `input_parameters` | JSON | N |  | `앱 생성` | 호출자 입력 JSON |
| 16 | `output_summary` | JSON | N |  | `앱 생성` | 출력 요약 JSON |
| 17 | `result_execution_attempt_id` | UUID | Y | FK |  | 현재 결과를 만든 Attempt |
| 18 | `result_manifest_path` | TEXT | Y |  |  | 결과 manifest 상대경로 |
| 19 | `result_manifest_checksum_sha256` | VARCHAR(64) | Y |  |  | manifest sha256 |
| 20 | `result_manifest_size_bytes` | BIGINT | Y |  |  | manifest 크기 |
| 21 | `result_fencing_token` | BIGINT | Y |  |  | 결과를 쓴 fencing token |
| 22 | `result_complete` | BOOLEAN | Y |  |  | 출력이 빠짐없이 저장됐는지 |
| 23 | `result_representation_count` | BIGINT | N |  | `0` | 출력 파일 수 |
| 24 | `result_total_size_bytes` | BIGINT | N |  | `0` | 출력 총 크기 |
| 25 | `error_message` | TEXT | Y |  |  | 오류 요약 |
| 26 | `created_by_client` | VARCHAR(64) | Y |  |  | 행을 만든 호출 클라이언트 |
| 27 | `created_by` | VARCHAR(255) | Y |  |  | 행을 만든 사용자 |
| 28 | `updated_by_client` | VARCHAR(64) | Y |  |  | 마지막으로 고친 호출 클라이언트 |
| 29 | `updated_by` | VARCHAR(255) | Y |  |  | 마지막으로 고친 사용자 |
| 30 | `created_at` | TIMESTAMPTZ | N |  | `앱 생성` | 생성 시각 |
| 31 | `updated_at` | TIMESTAMPTZ | N |  | `앱 생성` | 마지막 변경 시각 |
| 32 | `started_at` | TIMESTAMPTZ | Y |  |  | 시작 |
| 33 | `finished_at` | TIMESTAMPTZ | Y |  |  | 종료 |

| No | 제약·인덱스 이름 | 종류 | 컬럼 | 내용 |
|---|---|---|---|---|
| 1 | `ck_execution_steps_non_negative_sequence` | CHECK |  | `sequence >= 0` |
| 2 | `ck_execution_steps_valid_source` | CHECK |  | `(source_type = 'INLINE' AND source_path IS NULL) OR (source_type = 'PATH' AND source_path IS NOT NULL)` |
| 3 | `ck_execution_steps_valid_source_type` | CHECK |  | `source_type IN ('INLINE', 'PATH')` |
| 4 | `ck_execution_steps_valid_step_status` | CHECK |  | `status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'SKIPPED', 'CANCELLED')` |
| 5 | `ck_execution_steps_valid_step_timeout` | CHECK |  | `step_timeout_seconds IS NULL OR step_timeout_seconds >= 1` |
| 6 | `fk_execution_steps_execution_id_executions` | FK | execution_id | → executions (ON DELETE CASCADE) |
| 7 | `fk_execution_steps_operation_id_execution_operations` | FK | operation_id | → execution_operations (ON DELETE CASCADE) |
| 8 | `fk_execution_steps_result_execution_attempt_id_execution_attempts` | FK | result_execution_attempt_id | → execution_attempts (ON DELETE SET NULL) |
| 9 | `uq_execution_steps_execution_sequence` | UNIQUE | execution_id, sequence |  |
| 10 | `ix_execution_steps_execution_id` | INDEX | execution_id |  |
| 11 | `ix_execution_steps_operation_id` | INDEX | operation_id |  |

### 2.4 execution_attempts (실행 시도)

실행 시도 영역. Worker의 claim 1회. 정리 정책: 없음

| No | 컬럼 ID | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|---|
| 1 | `id` | UUID | N | PK | `앱 생성` | 식별자 |
| 2 | `execution_id` | UUID | N | FK,UK |  | 소속 Execution |
| 3 | `attempt_number` | INTEGER | N | UK |  | Execution 안 순번(1부터) |
| 4 | `runtime_type` | VARCHAR(32) | N |  | `JUPYTER` | Runtime 종류(JUPYTER) |
| 5 | `runtime_profile` | VARCHAR(128) | N |  | `basic` | 커널 프로필 |
| 6 | `runtime_target_id` | UUID | N |  |  | 실행한 Target. FK 없음 |
| 7 | `runtime_session_id` | VARCHAR(255) | Y |  |  | 커널 세션 |
| 8 | `status` | VARCHAR(32) | N |  |  | Attempt 상태 |
| 9 | `lease_owner` | VARCHAR(255) | Y |  |  | 소유 Worker |
| 10 | `lease_expires_at` | TIMESTAMPTZ | Y |  |  | lease 만료 |
| 11 | `heartbeat_at` | TIMESTAMPTZ | N |  |  | 마지막 갱신 |
| 12 | `fencing_token` | BIGINT | N |  | `0` | 이 Attempt의 fencing token |
| 13 | `error_message` | TEXT | Y |  |  | 오류 요약 |
| 14 | `failure_type` | VARCHAR(32) | Y |  |  | 실패 원인 |
| 15 | `retry_strategy` | VARCHAR(32) | N |  | `NOT_RETRYABLE` | 재시도 방식 |
| 16 | `runtime_session_cleanup_status` | VARCHAR(32) | N |  | `NOT_REQUIRED` | 세션 정리 상태 |
| 17 | `runtime_abort_status` | VARCHAR(32) | N |  | `NOT_REQUIRED` | 중단 결과 |
| 18 | `created_by_client` | VARCHAR(64) | Y |  |  | 행을 만든 호출 클라이언트 |
| 19 | `created_by` | VARCHAR(255) | Y |  |  | 행을 만든 사용자 |
| 20 | `updated_by_client` | VARCHAR(64) | Y |  |  | 마지막으로 고친 호출 클라이언트 |
| 21 | `updated_by` | VARCHAR(255) | Y |  |  | 마지막으로 고친 사용자 |
| 22 | `created_at` | TIMESTAMPTZ | N |  | `앱 생성` | 생성 시각 |
| 23 | `updated_at` | TIMESTAMPTZ | N |  | `앱 생성` | 마지막 변경 시각 |
| 24 | `started_at` | TIMESTAMPTZ | N |  |  | 시작 |
| 25 | `finished_at` | TIMESTAMPTZ | Y |  |  | 종료 |

| No | 제약·인덱스 이름 | 종류 | 컬럼 | 내용 |
|---|---|---|---|---|
| 1 | `ck_execution_attempts_non_negative_fencing_token` | CHECK |  | `fencing_token >= 0` |
| 2 | `ck_execution_attempts_positive_attempt_number` | CHECK |  | `attempt_number > 0` |
| 3 | `ck_execution_attempts_valid_attempt_failure_type` | CHECK |  | `failure_type IS NULL OR failure_type IN ('TOOL_ERROR', 'INFRASTRUCTURE_ERROR', 'WORKER_SHUTDOWN', 'RUNTIME_UNAVAILABLE', 'LEASE_EXPIRED', 'INTERNAL_ERROR', 'OPERATION_WAIT_TIMEOUT', 'OPERATION_TIMEOUT', 'STEP_TIMEOUT', 'EXECUTION_TIMEOUT', 'OUTPUT_LIMIT_EXCEEDED', 'RUNTIME_SESSION_LOST', 'COMPLETION_FAILED')` |
| 4 | `ck_execution_attempts_valid_attempt_retry_strategy` | CHECK |  | `retry_strategy IN ('NOT_RETRYABLE', 'FROM_FAILED_STEP', 'FROM_START')` |
| 5 | `ck_execution_attempts_valid_attempt_runtime_type` | CHECK |  | `runtime_type IN ('JUPYTER')` |
| 6 | `ck_execution_attempts_valid_attempt_status` | CHECK |  | `status IN ('RUNNING', 'WAITING', 'SUCCEEDED', 'FAILED', 'CANCELLED')` |
| 7 | `ck_execution_attempts_valid_runtime_abort_status` | CHECK |  | `runtime_abort_status IN ('NOT_REQUIRED', 'PENDING', 'IDLE_CONFIRMED', 'SESSION_DELETED', 'SESSION_MISSING', 'FAILED')` |
| 8 | `ck_execution_attempts_valid_runtime_session_cleanup_status` | CHECK |  | `runtime_session_cleanup_status IN ('NOT_REQUIRED', 'PENDING', 'SUCCEEDED', 'FAILED')` |
| 9 | `fk_execution_attempts_execution_id_executions` | FK | execution_id | → executions (ON DELETE CASCADE) |
| 10 | `uq_execution_attempts_execution_attempt` | UNIQUE | execution_id, attempt_number |  |
| 11 | `ix_execution_attempts_lease` | INDEX | status, lease_expires_at |  |
| 12 | `ix_execution_attempts_target_status` | INDEX | runtime_target_id, status |  |

### 2.5 execution_step_attempts (스텝 시도)

실행 시도 영역. Step 실행 시도 1회와 그 결과 위치. 정리 정책: 없음

| No | 컬럼 ID | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|---|
| 1 | `id` | UUID | N | PK | `앱 생성` | 식별자 |
| 2 | `execution_id` | UUID | N | FK |  | 소속 Execution |
| 3 | `execution_attempt_id` | UUID | N | FK,UK |  | 소속 Attempt |
| 4 | `execution_step_id` | UUID | N | FK |  | 대상 Step |
| 5 | `sequence` | INTEGER | N | UK |  | Step 순번 |
| 6 | `skill_name` | VARCHAR(255) | Y |  |  | 호출자 표시용 |
| 7 | `tool_name` | VARCHAR(255) | Y |  |  | 호출자 표시용 |
| 8 | `input_parameters` | JSON | N |  | `앱 생성` | 호출자 입력 JSON |
| 9 | `status` | VARCHAR(32) | N |  |  | 이 시도의 Step 상태 |
| 10 | `output_summary` | JSON | N |  | `앱 생성` | 출력 요약 |
| 11 | `result_manifest_path` | TEXT | Y |  |  | 이 시도의 manifest 상대경로 |
| 12 | `result_manifest_checksum_sha256` | VARCHAR(64) | Y |  |  | manifest sha256 |
| 13 | `result_manifest_size_bytes` | BIGINT | Y |  |  | manifest 크기 |
| 14 | `result_fencing_token` | BIGINT | Y |  |  | 결과 fencing token |
| 15 | `result_complete` | BOOLEAN | Y |  |  | 출력 완전 여부 |
| 16 | `result_representation_count` | BIGINT | N |  | `0` | 출력 파일 수 |
| 17 | `result_total_size_bytes` | BIGINT | N |  | `0` | 출력 총 크기 |
| 18 | `peak_memory_bytes` | BIGINT | Y |  |  | 이 시도 동안 커널의 최대 메모리(바이트). 측정 못 하면 NULL |
| 19 | `cpu_seconds` | FLOAT | Y |  |  | 이 시도가 쓴 커널 CPU 시간(초). 측정 못 하면 NULL |
| 20 | `error_message` | TEXT | Y |  |  | 오류 요약 |
| 21 | `created_by_client` | VARCHAR(64) | Y |  |  | 행을 만든 호출 클라이언트 |
| 22 | `created_by` | VARCHAR(255) | Y |  |  | 행을 만든 사용자 |
| 23 | `updated_by_client` | VARCHAR(64) | Y |  |  | 마지막으로 고친 호출 클라이언트 |
| 24 | `updated_by` | VARCHAR(255) | Y |  |  | 마지막으로 고친 사용자 |
| 25 | `created_at` | TIMESTAMPTZ | N |  | `앱 생성` | 생성 시각 |
| 26 | `updated_at` | TIMESTAMPTZ | N |  | `앱 생성` | 마지막 변경 시각 |
| 27 | `started_at` | TIMESTAMPTZ | N |  |  | 시작 |
| 28 | `finished_at` | TIMESTAMPTZ | Y |  |  | 종료 |

| No | 제약·인덱스 이름 | 종류 | 컬럼 | 내용 |
|---|---|---|---|---|
| 1 | `ck_execution_step_attempts_non_negative_sequence` | CHECK |  | `sequence >= 0` |
| 2 | `ck_execution_step_attempts_valid_step_attempt_status` | CHECK |  | `status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'SKIPPED', 'CANCELLED')` |
| 3 | `fk_execution_step_attempts_execution_attempt_id_execution_attempts` | FK | execution_attempt_id | → execution_attempts (ON DELETE CASCADE) |
| 4 | `fk_execution_step_attempts_execution_id_executions` | FK | execution_id | → executions (ON DELETE CASCADE) |
| 5 | `fk_execution_step_attempts_execution_step_id_execution_steps` | FK | execution_step_id | → execution_steps (ON DELETE CASCADE) |
| 6 | `uq_execution_step_attempts_attempt_sequence` | UNIQUE | execution_attempt_id, sequence |  |
| 7 | `ix_execution_step_attempts_execution_attempt_id` | INDEX | execution_attempt_id |  |
| 8 | `ix_step_attempts_execution_sequence` | INDEX | execution_id, sequence |  |

### 2.6 execution_retries (재시도 요청)

실행 시도 영역. 호출자의 재시도 요청. 정리 정책: 없음

| No | 컬럼 ID | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|---|
| 1 | `id` | UUID | N | PK | `앱 생성` | 식별자 |
| 2 | `execution_id` | UUID | N | FK,UK |  | 소속 Execution |
| 3 | `idempotency_key` | VARCHAR(255) | N | UK |  | 재시도 멱등 키 |
| 4 | `from_sequence` | INTEGER | N |  |  | 재시작 Step |
| 5 | `created_at` | TIMESTAMPTZ | N |  | `앱 생성` | 생성 시각 |

| No | 제약·인덱스 이름 | 종류 | 컬럼 | 내용 |
|---|---|---|---|---|
| 1 | `ck_execution_retries_non_negative_from_sequence` | CHECK |  | `from_sequence >= 0` |
| 2 | `fk_execution_retries_execution_id_executions` | FK | execution_id | → executions (ON DELETE CASCADE) |
| 3 | `uq_execution_retries_execution_idempotency_key` | UNIQUE | execution_id, idempotency_key |  |
| 4 | `ix_execution_retries_execution_id` | INDEX | execution_id |  |

### 2.7 execution_step_runs (스텝 저널 요청)

인계 영역. Step 시도의 Jupyter 저널 요청. 정리 정책: 없음

| No | 컬럼 ID | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|---|
| 1 | `execution_step_attempt_id` | UUID | N | PK,FK |  | 실행 중인 Step 시도(PK) |
| 2 | `execution_id` | UUID | N | FK |  | 소속 Execution |
| 3 | `request_id` | UUID | N | UK |  | Jupyter 저널 요청 ID |
| 4 | `artifact_baseline` | JSON | N |  |  | Step 시작 시 작업공간 파일 목록(새 산출물 판별용) |
| 5 | `submitted_at` | TIMESTAMPTZ | Y |  |  | 커널에 코드를 보낸 시각 |
| 6 | `created_at` | TIMESTAMPTZ | N |  | `앱 생성` | 생성 시각 |

| No | 제약·인덱스 이름 | 종류 | 컬럼 | 내용 |
|---|---|---|---|---|
| 1 | `fk_execution_step_runs_execution_id_executions` | FK | execution_id | → executions (ON DELETE CASCADE) |
| 2 | `fk_execution_step_runs_execution_step_attempt_id_execution_step_attempts` | FK | execution_step_attempt_id | → execution_step_attempts (ON DELETE CASCADE) |
| 3 | `uq_execution_step_runs_request_id` | UNIQUE | request_id |  |
| 4 | `ix_execution_step_runs_execution_id` | INDEX | execution_id |  |

### 2.8 execution_handovers (인계 대기)

인계 영역. 다른 Worker를 기다리는 인계 표시. 정리 정책: 인수 시 삭제

| No | 컬럼 ID | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|---|
| 1 | `execution_id` | UUID | N | PK,FK |  | 인계 대기 Execution(PK) |
| 2 | `reason` | VARCHAR(32) | N |  |  | EXECUTOR_SHUTDOWN / EXECUTOR_UNRESPONSIVE |
| 3 | `paused_since` | TIMESTAMPTZ | N |  |  | 출력 수신이 멈춘 시각 |
| 4 | `created_at` | TIMESTAMPTZ | N |  | `앱 생성` | 생성 시각 |

| No | 제약·인덱스 이름 | 종류 | 컬럼 | 내용 |
|---|---|---|---|---|
| 1 | `fk_execution_handovers_execution_id_executions` | FK | execution_id | → executions (ON DELETE CASCADE) |

### 2.9 execution_artifacts (산출물)

결과 영역. 작업공간 산출물 파일 등록. 정리 정책: 없음

| No | 컬럼 ID | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|---|
| 1 | `id` | UUID | N | PK | `앱 생성` | 식별자 |
| 2 | `execution_id` | UUID | N | FK |  | 소속 Execution |
| 3 | `execution_attempt_id` | UUID | Y | FK |  | 만든 Attempt |
| 4 | `execution_step_id` | UUID | Y | FK |  | 만든 Step |
| 5 | `execution_step_attempt_id` | UUID | Y | FK |  | 만든 Step 시도 |
| 6 | `parent_artifact_id` | UUID | Y | FK |  | 원본 Artifact(파생 관계) |
| 7 | `external_parent_asset_id` | VARCHAR(255) | Y |  |  | 외부 자산 ID |
| 8 | `artifact_type` | VARCHAR(32) | N |  |  | 종류 |
| 9 | `storage_type` | VARCHAR(32) | N |  |  | PV / S3 |
| 10 | `status` | VARCHAR(32) | N |  |  | AVAILABLE / INCOMPLETE / DELETED |
| 11 | `name` | VARCHAR(255) | N |  |  | 이름 |
| 12 | `description` | TEXT | Y |  |  | 설명 |
| 13 | `uri` | TEXT | N |  |  | 저장 위치 URI |
| 14 | `relative_path` | TEXT | Y |  |  | Jupyter 작업공간 상대경로 |
| 15 | `media_type` | VARCHAR(255) | Y |  |  | MIME |
| 16 | `size_bytes` | BIGINT | Y |  |  | 크기 |
| 17 | `checksum_sha256` | VARCHAR(64) | Y |  |  | sha256 |
| 18 | `metadata` | JSON | N |  | `앱 생성` | 호출자 또는 등록 시 메타데이터 JSON |
| 19 | `identity_hash` | VARCHAR(64) | N | UK |  | 중복 등록 방지 해시 |
| 20 | `created_by_client` | VARCHAR(64) | Y |  |  | 행을 만든 호출 클라이언트 |
| 21 | `created_by` | VARCHAR(255) | Y |  |  | 행을 만든 사용자 |
| 22 | `updated_by_client` | VARCHAR(64) | Y |  |  | 마지막으로 고친 호출 클라이언트 |
| 23 | `updated_by` | VARCHAR(255) | Y |  |  | 마지막으로 고친 사용자 |
| 24 | `created_at` | TIMESTAMPTZ | N |  | `앱 생성` | 생성 시각 |
| 25 | `updated_at` | TIMESTAMPTZ | N |  | `앱 생성` | 마지막 변경 시각 |

| No | 제약·인덱스 이름 | 종류 | 컬럼 | 내용 |
|---|---|---|---|---|
| 1 | `ck_execution_artifacts_non_negative_size` | CHECK |  | `size_bytes IS NULL OR size_bytes >= 0` |
| 2 | `ck_execution_artifacts_valid_artifact_status` | CHECK |  | `status IN ('AVAILABLE', 'INCOMPLETE', 'DELETED')` |
| 3 | `ck_execution_artifacts_valid_artifact_storage_type` | CHECK |  | `storage_type IN ('PV', 'S3')` |
| 4 | `ck_execution_artifacts_valid_artifact_type` | CHECK |  | `artifact_type IN ('DATASET', 'NOTEBOOK', 'REPORT', 'PLOT', 'MODEL', 'METRIC', 'LOG', 'OTHER')` |
| 5 | `fk_execution_artifacts_execution_attempt_id_execution_attempts` | FK | execution_attempt_id | → execution_attempts (ON DELETE CASCADE) |
| 6 | `fk_execution_artifacts_execution_id_executions` | FK | execution_id | → executions (ON DELETE CASCADE) |
| 7 | `fk_execution_artifacts_execution_step_attempt_id_execution_step_attempts` | FK | execution_step_attempt_id | → execution_step_attempts (ON DELETE SET NULL) |
| 8 | `fk_execution_artifacts_execution_step_id_execution_steps` | FK | execution_step_id | → execution_steps (ON DELETE SET NULL) |
| 9 | `fk_execution_artifacts_parent_artifact_id_execution_artifacts` | FK | parent_artifact_id | → execution_artifacts (ON DELETE SET NULL) |
| 10 | `uq_execution_artifacts_identity_hash` | UNIQUE | identity_hash |  |
| 11 | `ix_execution_artifacts_execution_created` | INDEX | execution_id, created_at, id |  |
| 12 | `ix_execution_artifacts_step` | INDEX | execution_step_id, created_at |  |

### 2.10 execution_diagnostics (진단)

결과 영역. 실패·누락 관찰 기록. 정리 정책: 없음

| No | 컬럼 ID | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|---|
| 1 | `id` | UUID | N | PK | `앱 생성` | 식별자 |
| 2 | `execution_id` | UUID | N | FK |  | 소속 Execution |
| 3 | `attempt_id` | UUID | Y |  |  | 관련 Attempt(FK 없음) |
| 4 | `operation_id` | UUID | Y |  |  | 관련 Operation(FK 없음) |
| 5 | `step_id` | UUID | Y |  |  | 관련 Step(FK 없음) |
| 6 | `step_sequence` | INTEGER | Y |  |  | Step 순번 |
| 7 | `fencing_token` | INTEGER | N |  |  | 기록 시 fencing token |
| 8 | `detail` | JSON | N |  |  | 진단 내용 JSON(code, phase, origin, severity 등) |
| 9 | `occurred_at` | TIMESTAMPTZ | N |  |  | 발생 시각 |
| 10 | `created_at` | TIMESTAMPTZ | N |  |  | 생성 시각 |
| 11 | `updated_at` | TIMESTAMPTZ | N |  |  | 마지막 변경 시각 |
| 12 | `created_by_client` | VARCHAR(64) | Y |  |  | 행을 만든 호출 클라이언트 |
| 13 | `created_by` | VARCHAR(255) | Y |  |  | 행을 만든 사용자 |
| 14 | `updated_by_client` | VARCHAR(64) | Y |  |  | 마지막으로 고친 호출 클라이언트 |
| 15 | `updated_by` | VARCHAR(255) | Y |  |  | 마지막으로 고친 사용자 |

| No | 제약·인덱스 이름 | 종류 | 컬럼 | 내용 |
|---|---|---|---|---|
| 1 | `ck_execution_diagnostics_nonnegative_step_sequence` | CHECK |  | `step_sequence IS NULL OR step_sequence >= 0` |
| 2 | `ck_execution_diagnostics_positive_fencing_token` | CHECK |  | `fencing_token >= 1` |
| 3 | `fk_execution_diagnostics_execution_id_executions` | FK | execution_id | → executions (ON DELETE CASCADE) |
| 4 | `ix_diagnostics_attempt_cursor` | INDEX | execution_id, attempt_id, created_at, id |  |
| 5 | `ix_diagnostics_execution_cursor` | INDEX | execution_id, created_at, id |  |

### 2.11 execution_event_sequences (이벤트 순번)

이벤트 영역. 실행별 이벤트 순번 카운터. 정리 정책: 없음

| No | 컬럼 ID | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|---|
| 1 | `execution_id` | UUID | N | PK,FK |  | 소속 Execution |
| 2 | `last_sequence` | BIGINT | N |  |  | 마지막으로 부여한 이벤트 순번 |

| No | 제약·인덱스 이름 | 종류 | 컬럼 | 내용 |
|---|---|---|---|---|
| 1 | `ck_execution_event_sequences_positive_last_sequence` | CHECK |  | `last_sequence >= 1` |
| 2 | `fk_execution_event_sequences_execution_id_executions` | FK | execution_id | → executions (ON DELETE CASCADE) |

### 2.12 execution_events (실행 이벤트)

이벤트 영역. 실행 타임라인의 이벤트 1건. 정리 정책: 종료 후 90일

| No | 컬럼 ID | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|---|
| 1 | `id` | UUID | N | PK | `앱 생성` | 식별자 |
| 2 | `execution_id` | UUID | N | FK,UK |  | 소속 Execution |
| 3 | `event_sequence` | BIGINT | N | UK |  | Execution 안 이벤트 순번(1부터) |
| 4 | `event_type` | VARCHAR(255) | N |  |  | 이벤트 종류 |
| 5 | `schema_version` | VARCHAR(32) | N |  |  | 이벤트 스키마 버전 |
| 6 | `payload` | JSON | N |  |  | 이벤트 본문 JSON |
| 7 | `created_by_client` | VARCHAR(64) | Y |  |  | 행을 만든 호출 클라이언트 |
| 8 | `created_by` | VARCHAR(255) | Y |  |  | 행을 만든 사용자 |
| 9 | `updated_by_client` | VARCHAR(64) | Y |  |  | 마지막으로 고친 호출 클라이언트 |
| 10 | `updated_by` | VARCHAR(255) | Y |  |  | 마지막으로 고친 사용자 |
| 11 | `created_at` | TIMESTAMPTZ | N |  |  | 생성 시각 |
| 12 | `updated_at` | TIMESTAMPTZ | N |  |  | 마지막 변경 시각 |

| No | 제약·인덱스 이름 | 종류 | 컬럼 | 내용 |
|---|---|---|---|---|
| 1 | `ck_execution_events_positive_event_sequence` | CHECK |  | `event_sequence >= 1` |
| 2 | `fk_execution_events_execution_id_executions` | FK | execution_id | → executions (ON DELETE CASCADE) |
| 3 | `uq_execution_events_execution_sequence` | UNIQUE | execution_id, event_sequence |  |
| 4 | `ix_execution_events_created` | INDEX | created_at, id |  |
| 5 | `ix_execution_events_execution_cursor` | INDEX | execution_id, event_sequence |  |

### 2.13 outbox_events (발행 대기열)

이벤트 영역. Redis 발행 대기·완료 1건. 정리 정책: 발행 후 7일

| No | 컬럼 ID | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|---|
| 1 | `id` | UUID | N | PK | `앱 생성` | 식별자 |
| 2 | `aggregate_type` | VARCHAR(128) | N | UK |  | 집계 종류(Execution) |
| 3 | `aggregate_id` | UUID | N | UK |  | Execution ID |
| 4 | `event_type` | VARCHAR(255) | N |  |  | 이벤트 종류 |
| 5 | `event_sequence` | BIGINT | N | UK |  | 발행 순서 |
| 6 | `execution_event_id` | UUID | N | FK,UK |  | 원본 이벤트 |
| 7 | `created_by_client` | VARCHAR(64) | Y |  |  | 행을 만든 호출 클라이언트 |
| 8 | `created_by` | VARCHAR(255) | Y |  |  | 행을 만든 사용자 |
| 9 | `updated_by_client` | VARCHAR(64) | Y |  |  | 마지막으로 고친 호출 클라이언트 |
| 10 | `updated_by` | VARCHAR(255) | Y |  |  | 마지막으로 고친 사용자 |
| 11 | `status` | VARCHAR(32) | N |  |  | PENDING / PUBLISHED |
| 12 | `attempt_count` | INTEGER | N |  | `0` | 발행 시도 횟수 |
| 13 | `available_at` | TIMESTAMPTZ | N |  |  | 다음 발행 가능 시각(백오프) |
| 14 | `created_at` | TIMESTAMPTZ | N |  |  | 생성 시각 |
| 15 | `updated_at` | TIMESTAMPTZ | N |  |  | 마지막 변경 시각 |
| 16 | `published_at` | TIMESTAMPTZ | Y |  |  | 발행 시각 |
| 17 | `last_error` | VARCHAR(500) | Y |  |  | 마지막 발행 오류 |

| No | 제약·인덱스 이름 | 종류 | 컬럼 | 내용 |
|---|---|---|---|---|
| 1 | `ck_outbox_events_valid_outbox_sequence` | CHECK |  | `event_sequence >= 1` |
| 2 | `ck_outbox_events_valid_outbox_status` | CHECK |  | `status IN ('PENDING', 'PUBLISHED')` |
| 3 | `fk_outbox_events_execution_event_id_execution_events` | FK | execution_event_id | → execution_events (ON DELETE RESTRICT) |
| 4 | `uq_outbox_aggregate_event_sequence` | UNIQUE | aggregate_type, aggregate_id, event_sequence |  |
| 5 | `uq_outbox_execution_event_id` | UNIQUE | execution_event_id |  |
| 6 | `ix_outbox_events_aggregate_id` | INDEX | aggregate_id |  |
| 7 | `ix_outbox_pending` | INDEX | status, available_at, created_at |  |
| 8 | `ix_outbox_pending_event_order` | INDEX | aggregate_type, aggregate_id, event_sequence | WHERE status = 'PENDING' |

### 2.14 runtime_targets (Runtime Target)

Runtime 영역. 등록된 Jupyter 서버. 정리 정책: 수동

| No | 컬럼 ID | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|---|
| 1 | `id` | UUID | N | PK | `앱 생성` | 식별자 |
| 2 | `name` | VARCHAR(255) | N | UK |  | Target 이름(유일) |
| 3 | `runtime_type` | VARCHAR(32) | N |  | `JUPYTER` | Runtime 종류(JUPYTER) |
| 4 | `connection_config` | JSON | N |  |  | 접속 설정 JSON(URL 등) |
| 5 | `connection_key` | VARCHAR(2048) | N | UK |  | 물리 서버 식별 키. 같은 서버 중복 등록 방지 |
| 6 | `credential_ref` | VARCHAR(255) | N |  |  | 자격증명 참조 |
| 7 | `credential_ciphertext` | TEXT | Y |  |  | Fernet 암호화 토큰 |
| 8 | `pool` | VARCHAR(32) | N |  |  | INTERACTIVE / BATCH |
| 9 | `status` | VARCHAR(32) | N |  |  | ACTIVE / DRAINING / OFFLINE |
| 10 | `max_concurrent_executions` | INTEGER | N |  |  | 동시 실행 슬롯 |
| 11 | `supported_profiles` | JSON | N |  | `앱 생성` | 지원 커널 프로필 |
| 12 | `enabled` | BOOLEAN | N |  | `True` | false면 배정·probe 제외 |
| 13 | `last_health_check_at` | TIMESTAMPTZ | Y |  |  | 마지막 health 확인 |
| 14 | `last_health_error` | VARCHAR(500) | Y |  |  | health 오류 |
| 15 | `active_session_count` | INTEGER | Y |  |  | Jupyter가 보고한 세션 수 |
| 16 | `session_count_observed_at` | TIMESTAMPTZ | Y |  |  | 세션 수 관측 시각 |
| 17 | `resource_observed_at` | TIMESTAMPTZ | Y |  |  | 자원 관측 시각 |
| 18 | `resource_last_check_at` | TIMESTAMPTZ | Y |  |  | 자원 확인 시도 시각 |
| 19 | `resource_last_error` | VARCHAR(500) | Y |  |  | 자원 관측 오류 |
| 20 | `resource_source` | VARCHAR(64) | Y |  |  | 관측 출처(cgroup 등) |
| 21 | `resource_estimated` | BOOLEAN | Y |  |  | 추정치 여부 |
| 22 | `resource_process_count` | INTEGER | Y |  |  | 커널 프로세스 수 |
| 23 | `cpu_used_cores` | FLOAT | Y |  |  | CPU 사용 코어 |
| 24 | `cpu_capacity_cores` | FLOAT | Y |  |  | CPU 용량 |
| 25 | `cpu_utilization` | FLOAT | Y |  |  | CPU 사용률(0~1) |
| 26 | `memory_used_bytes` | BIGINT | Y |  |  | 메모리 사용 |
| 27 | `memory_capacity_bytes` | BIGINT | Y |  |  | 메모리 용량 |
| 28 | `memory_utilization` | FLOAT | Y |  |  | 메모리 사용률 |
| 29 | `resource_errors` | JSON | N |  | `앱 생성` | 항목별 관측 오류 |
| 30 | `created_by_client` | VARCHAR(64) | Y |  |  | 행을 만든 호출 클라이언트 |
| 31 | `created_by` | VARCHAR(255) | Y |  |  | 행을 만든 사용자 |
| 32 | `updated_by_client` | VARCHAR(64) | Y |  |  | 마지막으로 고친 호출 클라이언트 |
| 33 | `updated_by` | VARCHAR(255) | Y |  |  | 마지막으로 고친 사용자 |
| 34 | `created_at` | TIMESTAMPTZ | N |  | `앱 생성` | 생성 시각 |
| 35 | `updated_at` | TIMESTAMPTZ | N |  | `앱 생성` | 마지막 변경 시각 |

| No | 제약·인덱스 이름 | 종류 | 컬럼 | 내용 |
|---|---|---|---|---|
| 1 | `ck_runtime_targets_non_negative_active_session_count` | CHECK |  | `active_session_count IS NULL OR active_session_count >= 0` |
| 2 | `ck_runtime_targets_positive_max_concurrency` | CHECK |  | `max_concurrent_executions > 0` |
| 3 | `ck_runtime_targets_valid_runtime_target_status` | CHECK |  | `status IN ('ACTIVE', 'DRAINING', 'OFFLINE')` |
| 4 | `ck_runtime_targets_valid_runtime_type` | CHECK |  | `runtime_type IN ('JUPYTER')` |
| 5 | `uq_runtime_targets_connection_key` | UNIQUE | connection_key |  |
| 6 | `uq_runtime_targets_name` | UNIQUE | name |  |
| 7 | `ix_runtime_targets_created_cursor` | INDEX | created_at, id |  |
| 8 | `ix_runtime_targets_pool_status` | INDEX | pool, enabled, status |  |

### 2.15 runtime_target_purges (삭제된 Target)

Runtime 영역. 삭제된 Target의 흔적. 정리 정책: 없음

| No | 컬럼 ID | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|---|
| 1 | `id` | UUID | N | PK | `앱 생성` | 식별자 |
| 2 | `target_id` | UUID | N | UK |  | 삭제된 Target의 원래 ID |
| 3 | `target_name` | VARCHAR(255) | N |  |  | 삭제 당시 이름 |
| 4 | `runtime_type` | VARCHAR(32) | N |  |  | Runtime 종류(JUPYTER) |
| 5 | `connection_config` | JSON | N |  |  | 삭제 당시 접속 설정(자격증명 제외) |
| 6 | `pool` | VARCHAR(32) | N |  |  | INTERACTIVE / BATCH |
| 7 | `created_by_client` | VARCHAR(64) | Y |  |  | 행을 만든 호출 클라이언트 |
| 8 | `created_by` | VARCHAR(255) | Y |  |  | 행을 만든 사용자 |
| 9 | `updated_by_client` | VARCHAR(64) | Y |  |  | 마지막으로 고친 호출 클라이언트 |
| 10 | `updated_by` | VARCHAR(255) | Y |  |  | 마지막으로 고친 사용자 |
| 11 | `created_at` | TIMESTAMPTZ | N |  | `앱 생성` | 생성 시각 |
| 12 | `updated_at` | TIMESTAMPTZ | N |  | `앱 생성` | 마지막 변경 시각 |

| No | 제약·인덱스 이름 | 종류 | 컬럼 | 내용 |
|---|---|---|---|---|
| 1 | `ck_runtime_target_purges_valid_pool` | CHECK |  | `pool IN ('INTERACTIVE', 'BATCH')` |
| 2 | `ck_runtime_target_purges_valid_runtime_type` | CHECK |  | `runtime_type IN ('JUPYTER')` |
| 3 | `uq_runtime_target_purges_target_id` | UNIQUE | target_id |  |

### 2.16 executor_maintenance (접수 상태)

운영 영역. 전체 접수 상태(항상 1행). 정리 정책: -

| No | 컬럼 ID | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|---|
| 1 | `singleton_key` | VARCHAR(32) | N | PK | `executor` | 항상 'executor' |
| 2 | `admission_state` | VARCHAR(32) | N |  | `ACTIVE` | ACTIVE / DRAINING |
| 3 | `version` | BIGINT | N |  | `0` | 변경 버전 |
| 4 | `created_by_client` | VARCHAR(64) | Y |  |  | 행을 만든 호출 클라이언트 |
| 5 | `created_by` | VARCHAR(255) | Y |  |  | 행을 만든 사용자 |
| 6 | `updated_by_client` | VARCHAR(64) | Y |  |  | 마지막으로 고친 호출 클라이언트 |
| 7 | `updated_by` | VARCHAR(255) | Y |  |  | 마지막으로 고친 사용자 |
| 8 | `created_at` | TIMESTAMPTZ | N |  | `앱 생성` | 생성 시각 |
| 9 | `updated_at` | TIMESTAMPTZ | N |  | `앱 생성` | 마지막 변경 시각 |

| No | 제약·인덱스 이름 | 종류 | 컬럼 | 내용 |
|---|---|---|---|---|
| 1 | `ck_executor_maintenance_non_negative_version` | CHECK |  | `version >= 0` |
| 2 | `ck_executor_maintenance_singleton_key` | CHECK |  | `singleton_key = 'executor'` |
| 3 | `ck_executor_maintenance_valid_admission_state` | CHECK |  | `admission_state IN ('ACTIVE', 'DRAINING')` |

### 2.17 maintenance_runs (일괄 중지)

운영 영역. 실행 일괄 중지 요청. 정리 정책: 없음

| No | 컬럼 ID | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|---|
| 1 | `id` | UUID | N | PK | `앱 생성` | 식별자 |
| 2 | `idempotency_key` | VARCHAR(255) | N | UK |  | 멱등 키 |
| 3 | `request_fingerprint` | VARCHAR(64) | N |  |  | 요청 지문(sha256) |
| 4 | `action` | VARCHAR(32) | N |  |  | STOP_ACTIVE_EXECUTIONS |
| 5 | `status` | VARCHAR(32) | N |  |  | Run 상태 |
| 6 | `lease_owner` | VARCHAR(255) | Y |  |  | 처리 중인 Worker |
| 7 | `lease_expires_at` | TIMESTAMPTZ | Y |  |  | lease 만료 |
| 8 | `heartbeat_at` | TIMESTAMPTZ | Y |  |  | 마지막 lease 갱신 |
| 9 | `fencing_token` | BIGINT | N |  | `0` | Run 소유권 세대 |
| 10 | `error_message` | TEXT | Y |  |  | 오류 요약 |
| 11 | `created_by_client` | VARCHAR(64) | Y |  |  | 행을 만든 호출 클라이언트 |
| 12 | `created_by` | VARCHAR(255) | Y |  |  | 행을 만든 사용자 |
| 13 | `updated_by_client` | VARCHAR(64) | Y |  |  | 마지막으로 고친 호출 클라이언트 |
| 14 | `updated_by` | VARCHAR(255) | Y |  |  | 마지막으로 고친 사용자 |
| 15 | `created_at` | TIMESTAMPTZ | N |  | `앱 생성` | 생성 시각 |
| 16 | `updated_at` | TIMESTAMPTZ | N |  | `앱 생성` | 마지막 변경 시각 |
| 17 | `started_at` | TIMESTAMPTZ | Y |  |  | 시작 |
| 18 | `finished_at` | TIMESTAMPTZ | Y |  |  | 종료 |

| No | 제약·인덱스 이름 | 종류 | 컬럼 | 내용 |
|---|---|---|---|---|
| 1 | `ck_maintenance_runs_non_negative_fencing_token` | CHECK |  | `fencing_token >= 0` |
| 2 | `ck_maintenance_runs_valid_action` | CHECK |  | `action IN ('STOP_ACTIVE_EXECUTIONS')` |
| 3 | `ck_maintenance_runs_valid_status` | CHECK |  | `status IN ('REQUESTED', 'RUNNING', 'SUCCEEDED', 'FAILED')` |
| 4 | `uq_maintenance_runs_idempotency_key` | UNIQUE | idempotency_key |  |
| 5 | `ix_maintenance_runs_created` | INDEX | created_at, id |  |
| 6 | `ix_maintenance_runs_recovery` | INDEX | status, lease_expires_at |  |

### 2.18 maintenance_run_targets (일괄 중지 대상)

운영 영역. 일괄 중지 대상 실행. 정리 정책: 없음

| No | 컬럼 ID | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|---|
| 1 | `id` | UUID | N | PK | `앱 생성` | 식별자 |
| 2 | `maintenance_run_id` | UUID | N | FK,UK |  | 소속 Run |
| 3 | `execution_id` | UUID | N | FK,UK |  | 중지 대상 Execution |
| 4 | `selected_execution_status` | VARCHAR(32) | N |  |  | 선정 당시 상태 |
| 5 | `status` | VARCHAR(32) | N |  |  | 대상 처리 상태 |
| 6 | `error_message` | TEXT | Y |  |  | 오류 요약 |
| 7 | `stop_requested_at` | TIMESTAMPTZ | Y |  |  | 중지 요청 시각 |
| 8 | `completed_at` | TIMESTAMPTZ | Y |  |  | 처리 완료 |
| 9 | `created_by_client` | VARCHAR(64) | Y |  |  | 행을 만든 호출 클라이언트 |
| 10 | `created_by` | VARCHAR(255) | Y |  |  | 행을 만든 사용자 |
| 11 | `updated_by_client` | VARCHAR(64) | Y |  |  | 마지막으로 고친 호출 클라이언트 |
| 12 | `updated_by` | VARCHAR(255) | Y |  |  | 마지막으로 고친 사용자 |
| 13 | `created_at` | TIMESTAMPTZ | N |  | `앱 생성` | 생성 시각 |
| 14 | `updated_at` | TIMESTAMPTZ | N |  | `앱 생성` | 마지막 변경 시각 |

| No | 제약·인덱스 이름 | 종류 | 컬럼 | 내용 |
|---|---|---|---|---|
| 1 | `ck_maintenance_run_targets_valid_status` | CHECK |  | `status IN ('PENDING', 'STOP_REQUESTED', 'STOPPED', 'FAILED')` |
| 2 | `fk_maintenance_run_targets_execution_id_executions` | FK | execution_id | → executions (ON DELETE NO ACTION) |
| 3 | `fk_maintenance_run_targets_maintenance_run_id_maintenance_runs` | FK | maintenance_run_id | → maintenance_runs (ON DELETE CASCADE) |
| 4 | `uq_maintenance_run_targets_run_execution` | UNIQUE | maintenance_run_id, execution_id |  |
| 5 | `ix_maintenance_run_targets_cursor` | INDEX | maintenance_run_id, created_at, id |  |
| 6 | `ix_maintenance_run_targets_run_status` | INDEX | maintenance_run_id, status |  |

### 2.19 event_retention_lease (주기 작업 lease)

운영 영역. 주기 작업 단일 실행 표시(작업 종류당 1행). 정리 정책: -

| No | 컬럼 ID | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|---|
| 1 | `singleton_key` | VARCHAR(32) | N | PK |  | 작업 키(events, lease-recovery, runtime-health 등) |
| 2 | `lease_owner` | VARCHAR(255) | Y |  |  | 이번 주기를 잡은 Pod |
| 3 | `lease_expires_at` | TIMESTAMPTZ | Y |  |  | 다음 주기 가능 시각 |
| 4 | `last_started_at` | TIMESTAMPTZ | Y |  |  | 마지막 시작 |
| 5 | `last_completed_at` | TIMESTAMPTZ | Y |  |  | 마지막 완료 |
| 6 | `last_error` | VARCHAR(1000) | Y |  |  | 마지막 오류 |
| 7 | `created_at` | TIMESTAMPTZ | N |  | `앱 생성` | 생성 시각 |
| 8 | `updated_at` | TIMESTAMPTZ | N |  | `앱 생성` | 마지막 변경 시각 |

| No | 제약·인덱스 이름 | 종류 | 컬럼 | 내용 |
|---|---|---|---|---|
| 1 | `ck_event_retention_lease_complete_lease` | CHECK |  | `(lease_owner IS NULL) = (lease_expires_at IS NULL)` |

### 2.20 command_receipts (명령 영수증)

멱등 영역. 관리 명령·Artifact 생성의 멱등 응답. 정리 정책: 없음

| No | 컬럼 ID | 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|---|
| 1 | `id` | UUID | N | PK | `앱 생성` | 식별자 |
| 2 | `scope` | VARCHAR(64) | N | UK |  | 명령 범위(Runtime Target, 정비, Artifact 생성 등) |
| 3 | `idempotency_key` | VARCHAR(255) | N | UK |  | 멱등 키 |
| 4 | `command_type` | VARCHAR(128) | N |  |  | 명령 종류 |
| 5 | `request_fingerprint` | VARCHAR(64) | N |  |  | 요청 지문(sha256) |
| 6 | `result` | JSON | N |  |  | 처음 실행한 결과 JSON. 같은 키 재요청에 그대로 돌려준다 |
| 7 | `created_at` | TIMESTAMPTZ | N |  | `앱 생성` | 생성 시각 |

| No | 제약·인덱스 이름 | 종류 | 컬럼 | 내용 |
|---|---|---|---|---|
| 1 | `uq_command_receipts_scope_idempotency_key` | UNIQUE | scope, idempotency_key |  |

## 3. RDB 외 저장소

RDB 밖에 있지만 DB 행이 가리키거나 서비스가 주고받는 데이터다. Vector DB는 사용하지 않는다.

| No | 저장소 | 위치 · 필드 | 타입 | 설명 |
|---|---|---|---|---|
| 1 | Redis Stream executor.events.<tenant> | `event_id` | string(uuid) | 이벤트 ID. 중복 제거 키 |
| 2 | Redis Stream executor.events.<tenant> | `event_type` | string | 이벤트 종류 8개 중 하나 |
| 3 | Redis Stream executor.events.<tenant> | `schema_version` | string | 1.0 |
| 4 | Redis Stream executor.events.<tenant> | `execution_id` | string(uuid) | Execution ID |
| 5 | Redis Stream executor.events.<tenant> | `event_sequence` | 숫자 문자열 | Execution별 순번(1부터) |
| 6 | Redis Stream executor.events.<tenant> | `payload` | JSON 문자열 | 종류별 본문 |
| 7 | Redis Stream executor.events.<tenant> | `occurred_at` | ISO-8601 | 발생 시각 |
| 8 | Redis Stream executor.events.<tenant> | `tenant_id` | string | Stream의 테넌트 |
| 9 | 공유 PV <STORAGE_ROOT>/executor/executions/<execution_id>/ | `operations/<operation_id>/sources/<step_id>.py` | 파일 | 소스 스냅샷. execution_steps.source_snapshot_path가 가리킨다 |
| 10 | 공유 PV <STORAGE_ROOT>/executor/executions/<execution_id>/ | `operations/<operation_id>/steps/<step_id>/attempts/<attempt_id>/<fencing_token>.partial/` | 디렉터리 | 쓰는 중인 결과. Worker 하나만 쓴다 |
| 11 | 공유 PV <STORAGE_ROOT>/executor/executions/<execution_id>/ | `.../attempts/<attempt_id>/<fencing_token>/manifest.json` | 파일 | 확정된 Step 결과 목록. execution_step_attempts.result_manifest_path가 가리킨다. 불변 |
| 12 | 공유 PV <STORAGE_ROOT>/executor/executions/<execution_id>/ | `.../attempts/<attempt_id>/<fencing_token>/outputs/` | 디렉터리 | 출력 파일. manifest가 경로, 크기, sha256을 가진다 |
| 13 | Jupyter PV tenants/<tenant>/users/<user>/projects/<project>/sessions/<session>/executions/<execution_id>/ | `notebooks/execution.ipynb` | 파일 | 실행 노트북. executions.notebook_path |
| 14 | Jupyter PV tenants/<tenant>/users/<user>/projects/<project>/sessions/<session>/executions/<execution_id>/ | `artifacts/` | 디렉터리 | 산출물. execution_artifacts.relative_path |
| 15 | Jupyter PV tenants/<tenant>/users/<user>/projects/<project>/sessions/<session>/executions/<execution_id>/ | `artifacts/manifest.jsonl` | 파일 | 코드가 선언한 산출물 목록 |
