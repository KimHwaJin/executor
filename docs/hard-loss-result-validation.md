# 강제 종료·오래된 Worker 복귀 시 결과 정합성 검증

## 기준과 복귀 지점

- 시작 기준: `main`의 `8c50baa7d69b1ecc42071f9e56e3c2fe960396b5`.
- 작업 브랜치: `feature/hard-loss-result-validation`.
- Jupyter 커스텀 저장 방식과 노트북 `0644` 정책을 유지한다.
- Contents API 비교는 별도 `feature/jupyter-contents-api-evaluation`에 보존한다.
- 복귀 시 작업 브랜치를 보존하고 위 기준 커밋의 main으로 전환한다.
  DB/Redis 초기화, 원격 이력 재작성, 사용자 파일 삭제는 하지 않는다.

## 계획 — 실행 전

1. 현재 로컬 Jupyter 이미지의 권한 구현을 읽기 전용으로 확인한다.
2. 최신 Executor 코드와 Jupyter 확장을 적용한 테스트 전용 이미지를 만든다.
   일반 서비스와 이미지 태그를 변경하지 않는다.
3. UUID Compose 프로젝트의 별도 DB, Redis, 공유 결과 볼륨, Jupyter PV에서
   SINGLE/MULTI × basic/ml의 텍스트·PNG 출력 중 SIGKILL을 검증한다.
4. Worker를 일시 정지해 lease를 만료시키고, 다른 Worker의 정리 후 재개하여
   이전 소유자의 뒤늦은 결과 반영을 검증한다. 자동 코드 재실행을 가정하지 않는다.
5. REST/DB/Redis 이벤트, 완료 Step의 manifest/checksum, 미완료 Step의
   참조 유무, 노트북 최신성 표시, 커널 정리를 비교한다.
6. 발견한 결함은 최소 범위로 수정하고 회귀 테스트·실제 Docker 재검증 후 기록한다.

강제 종료에서 아직 봉인·DB commit되지 않은 출력의 자동 복구를 약속하지 않는다.
중간 파일은 공개 결과가 아니며, 결과가 없을 때 완료로 표시해서는 안 된다.
파일 봉인과 DB commit 사이의 장애, DB·Redis·스토리지 장애 주입은 이번 결과에
실제 수행 범위를 구분하여 기록한다. 테스트가 없는 경우 통과로 간주하지 않는다.

## 환경 확인

현재 일반 로컬 `executor-jupyter-1`에는 `os.fchmod(..., 0o644)`가 없는
이전 코드가 로드되어 있다. 소스의 수정이 실행 중인 이미지에 자동 반영된 것이 아니다.
일반 서비스의 이미지 갱신·재시작은 이번 격리 검증과 별개의 배포 작업이다.

## 결과

진행 중. 실제 검증 후 케이스, 관측된 결함, 수정 커밋, 제한 사항을 추가한다.
