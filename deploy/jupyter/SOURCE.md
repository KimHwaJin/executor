# 구성 출처 / 유지보수

이 디렉토리는 Executor 저장소의 Jupyter 구성을 독립 빌드 컨텍스트로 복사한 전달 패키지다.

- 기준 커밋: `d1be8accb335ed4a0c60434469b57b2b26bc8ee5`
- 원본: `test_harness/jupyter/`
- 원본 디렉토리는 이동하거나 수정하지 않았다.
- Dockerfile의 COPY 소스만 이 디렉토리를 기준으로 변경했다.
- 커널 requirements, 설정, 시작 스크립트와 확장 소스는 기준 버전과 동일하다.
- 기존 Executor의 current-file 다운로드(Range 생략 시 전체 반환)와 호환된다.

빌드/실행 시 원본 저장소, test_harness, Executor 소스, uv 또는 Git은 필요 없다.
이 문서의 경로와 커밋은 출처 설명일 뿐 빌드 의존성이 아니다.

향후 확장 기능을 변경할 때는 원본 하네스와 이 패키지의 확장 소스를 함께 반영해야 한다.
저장소 테스트는 두 확장 소스의 차이를 검사한다. 런타임용 패키지 목록은 배포 정책에 맞게
`environments/*/requirements.txt`에서 조정할 수 있다.
