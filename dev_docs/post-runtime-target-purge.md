# Runtime 서버 등록 삭제

## 용도와 요청

운영자가 사용을 마친 Runtime Target의 **등록정보**를 제거한다.
REST 전용 관리 API이며 MCP Tool은 추가하지 않는다.

```http
POST /api/v1/runtime-targets/{target_id}/purge
Content-Type: application/json
```

```json
{
  "actor": {"type": "USER", "id": "user-100"}
}
```

- `target_id`: 필수 UUID. 이름이나 endpoint로 삭제하지 않는다.
- `actor`: 필수 요청자. `type`, `id` 모두 필수이며 기존 공통 ActorInput을 사용한다.
  운영자 호출은 `USER`를 사용한다. 요청자 정보는 인증·인가의 대체 수단이 아니다.
- `idempotency_key`, `confirmation_name`: 받지 않는다. 전달하면 `422`이다.
- 강제 종료, 파일 삭제, 이력 삭제 옵션은 없다.

## 응답

```json
{
  "target_id": "2e825b19-0745-47fc-802a-d5b0a058d790",
  "name": "jupyter-batch-01",
  "created_by_type": "USER",
  "created_by": "user-100",
  "updated_by_type": "USER",
  "updated_by": "user-100",
  "created_at": "2026-09-06T06:00:00Z",
  "updated_at": "2026-09-06T06:00:00Z"
}
```

`target_id`, `name`은 삭제된 등록정보의 식별값이다. 감사 필드는 원래 등록정보가
아닌 **삭제 기록**의 생성·수정 정보다. 기록은 불변이며 반복 요청자의 정보로
덮어쓰지 않는다. 응답에 token, 암호문, 접속 설정은 포함하지 않는다.

| 상황 | HTTP | 동작 |
| --- | --- | --- |
| 삭제 조건 충족 | 200 | 등록 제거 및 삭제 기록 생성 |
| 해당 UUID가 이미 삭제됨 | 200 | 최초 삭제 기록 반환 |
| 등록도 삭제 기록도 없음 | 404 | `RUNTIME_TARGET_NOT_FOUND` |
| 미비활성화·진행 중 작업·미확인 커널 정리 | 409 | `RUNTIME_TARGET_PURGE_CONFLICT` |
| 잘못된 UUID·body 또는 폐기한 필드 | 422 | 입력 검증 실패 |

## 삭제 조건과 영향

1. 먼저 `disable`해서 `enabled=false`, `status=OFFLINE`이어야 한다.
2. 실행 중, MULTI 대기, finalize/cancel 처리 등 미완료 작업과 활성 Attempt가 없어야 한다.
3. 재시도 보존 커널, PENDING/FAILED 커널 정리 등 실제 해제가 확인되지 않은
   세션이 없어야 한다. 보존 기한 만료 자체는 커널 해제 증거가 아니다.
4. 과거 Attempt가 같은 커널을 재사용했다면 후속 Attempt의 동일 세션 정리 성공도
   인정한다. 다른 커널의 정리 성공으로 대체하지 않는다.
5. 과거 성공/실패 이력만 있는 경우는 삭제 가능하다. 삭제 트랜잭션은 작업 배정·
   활성화와 같은 Target 행을 잠그며, Runtime 접속이나 커널 삭제를 수행하지 않는다.

삭제되는 것은 활성 등록과 거기에 저장된 인증정보다. Execution, Operation, Step,
Attempt, 이벤트, Artifact 및 실제 파일은 보존한다. Execution/Attempt의 과거
`runtime_target_id`도 유지한다. 이 ID는 더 이상 활성 등록만을 가리키는 외래키가
아니며, 삭제 후에는 `runtime_target_purges.target_id`로 삭제 시점의 이름·종류·
풀·비밀값 없는 접속 설정을 확인할 수 있다. 삭제 기록은 정상 서버 목록에 나오지
않으며 기존 Target 상세 GET은 `404`를 반환한다. 별도 삭제 이력 조회 API는 없다.

다운로드, 노트북 조회/쓰기, 리포트 쓰기는 Execution에 저장된 종류와 풀의
다른 사용 가능한 서버를 이용한다. 풀 내 서버가 모두 없어도 파일은 지우지 않지만
접근은 불가능하다. 풀 간에는 대체하지 않는다. 같은 풀은 기존 PV를 같은 상대경로로
접근할 수 있게 배포해야 한다. Pod/PV 삭제나 수동 생성 커널 종료는 별도 운영 작업이다.
이미 진행 중인 파일 전송을 이 API가 강제로 끊지는 않는다. Pod를 내리면 그 전송은
실패할 수 있으므로 운영자는 별도로 고려해야 한다.

## 재등록과 반복 요청

같은 이름·endpoint·token으로 다시 등록 가능하지만 **새 등록 멱등성 키**를 사용한다.
새 UUID와 새 probe 관측값을 갖는 별도 등록이며, 과거 실행 ID나 커널을 인계하지 않는다.
삭제된 등록에 쓰였던 upsert 키를 재사용하면 `409 IDEMPOTENCY_CONFLICT`이다.
이전 UUID로 purge를 반복해도 새 등록에는 영향이 없고 최초 삭제 결과만 반환한다.

## 배포와 롤백

- Alembic `0005`로 업그레이드한다. 실제 DB를 초기화하지 않는다.
- 기존 Executor 프로세스를 안전하게 종료한 뒤 마이그레이션/새 코드 배포를 진행한다.
  기존 코드는 삭제된 purge의 멱등성 컬럼을 사용하므로 혼용하지 않는다.
- Execution/Attempt의 Target 외래키 2개와 purge 기록의 멱등성 키·fingerprint 컬럼만
  제거한다. UUID 및 업무 이력은 유지한다. 다른 명령의 멱등성 처리와 이벤트 스키마는
  바뀌지 않는다.
- 과거 실행이 참조하는 서버를 이미 삭제했다면 `0004` downgrade를 차단한다.
  이력 ID를 지우거나 가짜 서버를 만들어 강행하지 않는다. `0005` 유지 또는 삭제 전
  백업 복원이 필요하다. 허용되는 downgrade도 폐기한 요청 키·해시는 복원하지 못하며
  기존 삭제 기록에 충돌 없는 대체 값을 채워 이전 스키마만 복원한다.
