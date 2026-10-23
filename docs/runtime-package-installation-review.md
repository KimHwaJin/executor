# 실행 중 Python 패키지 추가 설치 검토

- 상태: 검토 완료, 요구사항 미확정 ([DD-010](deferred-decisions.md#dd-010-execution-scoped-python-package-installation))
- 검토일: 2026-10-19
- 근거: 코드·이미지 정의 정독. 실제 설치 동작은 아직 실측하지 않았다.

## 배경

Agent가 MULTI 모드로 실행하다가 특정 패키지가 없어 `ModuleNotFoundError`가 나면, 그 패키지를
설치한 뒤 코드를 계속 실행하는 시나리오가 가능한지, 현실적인지, 트레이드오프가 무엇인지 검토했다.

## 현재 동작

실행 코드에서 패키지를 설치해도 표준 커널은 바뀌지 않는다.

| 시도 | 결과 | 근거 |
| --- | --- | --- |
| `%pip install`, `!pip install` | `No module named pip` | 커널 venv의 `uv.lock`에 pip 없음, Python을 `--with-ensurepip=no`로 빌드 |
| `!uv pip install x` | 해석·다운로드 후 쓰기 단계에서 `Permission denied` | 대상 자동 선택 시 PATH 첫 항목인 `/opt/venvs/jupyter`(서버 venv)를 고름. `/opt/venvs/*`는 빌드 시 root 소유, 커널은 UID 1000 |
| `!uv pip install --python sys.executable x`, `--system` | `Permission denied` | 위와 같음 |
| `!uv add x` | 작업 디렉터리에 `pyproject.toml`이 없어 실패 | `uv add`는 프로젝트 의존성 추가 명령. `/opt/jupyter-env/*`도 root 소유이고 런타임에는 `UV_PROJECT_ENVIRONMENT`가 없음 |
| `!uv pip install --target <쓰기 가능 경로> x` | 설치됨 | `sys.path`에 경로를 추가해야 import 가능 |

커널 수명은 실행 단위다. Executor는 실행마다 `POST /api/kernels`로 커널을 만들고 종료·취소 시
interrupt 후 `DELETE`한다(`infrastructure/jupyter/sessions.py`, `execution_worker/runtime_cleanup.py`).
커널스펙 `default`, `3102311`은 이미지에 고정된 venv를 가리키므로 다음 실행은 항상 이미지 상태로 시작한다.
표준 커널 패키지 변경 경로는 `deploy/jupyter/environments/<프로필>`의 `pyproject.toml`·`uv.lock`
수정과 이미지 재빌드다(`deploy/jupyter/README.md`).

## 선택지 비교

### 1. 표준 커널 venv 갱신 (`/opt/venvs`에 쓰기 권한 부여)

채택하지 않는다.

- 설치 결과가 해당 컨테이너의 쓰기 레이어에만 남는다. 같은 Pod의 이후 커널과 다른 사용자 실행이 모두 영향을 받는다.
- 다른 Pod·복제본에는 없고 재시작·재스케줄·HPA 스케일아웃 시 사라진다. 같은 코드가 배정 Pod에 따라 성공하거나 실패한다.
- 동시 설치로 venv가 손상되거나 실행 중인 다른 커널의 의존성이 바뀔 수 있다.
- 런타임 Pod에 `readOnlyRootFilesystem`을 적용하면 권한과 무관하게 불가능하다.

### 2. 커널(venv) 복사

채택하지 않는다.

- 기본 venv는 pandas, pyarrow, scipy, polars 등을 포함해 실행마다 수백 MB 복사가 필요하다.
- `KernelSpecManager.allowed_kernelspecs`가 `default`, `3102311`로 제한되고 커널스펙 디렉터리가
  root 소유라, 복사본을 커널로 띄우려면 Jupyter 설정과 Executor 프로필 선택까지 바꿔야 한다.

### 3. 실행별 오버레이 (권장)

표준 venv는 읽기 전용으로 두고 실행 전용 패키지 디렉터리를 `sys.path` **뒤에** 추가한다.

- 위치: 실행 디렉터리 바로 아래 `.pkgs/`
  (`users/{user}/projects/{project}/sessions/{session}/executions/{execution_id}/.pkgs`).
  - 커널 작업 디렉터리가 실행 디렉터리이므로 다른 실행과 자연스럽게 격리된다(`workspace.py`, `sessions.py`).
  - Artifact 스냅샷은 `artifacts/` 아래만 훑으므로 `.pkgs/` 파일은 Artifact로 수집되지 않는다
    (`extension/.../storage.py`의 `snapshot`). `artifacts/` 아래에 두면 안 된다.
  - PVC에 있으므로 커널 재생성·HPA 스케일링과 무관하다.
- MULTI 모드는 실행 하나에 커널 하나를 유지하므로 한 번 적용하면 이후 Step에서 그대로 import된다.
- `append`를 쓰는 이유: 기존 패키지(numpy 등)는 항상 표준 venv 것이 먼저 로드되어 다른 버전으로 가려지지 않는다.

## 권장 설계: Executor 주도 설치

경로·허용 목록·버전 고정을 Agent가 직접 다루면 격리가 깨질 위험이 있으므로 Executor가 전담한다.
Agent는 필요한 패키지만 선언하고 실행 코드는 `import`만 한다.

1. **제출 스키마**: `POST /executions`에 선택 필드
   `packages: [{"name": "scikit-learn", "version": "1.5.*"}]` 추가.
   Executor는 설정된 허용 목록을 검사하고 위반 시 요청을 400으로 거부한다.
2. **설치 주체**: Jupyter 확장에 `POST /executor/workspace/packages` 추가.
   서버 프로세스(UID 1000)가 다음을 실행한다.

   ```bash
   uv pip install --target <실행 디렉터리>/.pkgs \
       -c <프로필별 constraints.txt> \
       --python /opt/venvs/<프로필>/bin/python \
       <패키지...>
   ```

   커널 밖에서 설치하므로 노트북 출력·사용자 코드와 분리된다.
3. **자동 import**: 이미지 빌드 시 각 커널 venv site-packages에 `.pth` 파일을 넣어
   "작업 디렉터리의 `.pkgs`를 `sys.path` 뒤에 추가"한다. Python은 시작 시 `.pth`를 실행하므로
   사용자 코드에 `sys.path` 조작이 필요 없다.
4. **실행 흐름**: `prepare_workspace`(`.pkgs` 생성 포함) → 패키지 설치 → 커널 시작 → Step 실행.
   삽입 위치는 `multi_runner.py`·`single_runner.py`의 workspace 준비와 `start_session` 사이다.
   설치 실패 시 코드를 실행하지 않고 `PACKAGE_INSTALL_FAILED` 등 구조화된 진단과 함께 실패 처리한다.
   재시도·복구 시 재호출해도 설치된 패키지는 건너뛰므로 멱등이다.
5. **정리**: finalize 시 `.pkgs/` 삭제 또는 기존 워크스페이스 정리 정책에 포함.
6. **2단계(선택)**: MULTI 실행 중 operations API로 패키지 추가.
   `.pkgs`가 커널 시작 전부터 `sys.path`에 있고 Python import 시스템은 디렉터리 변경을 감지하므로
   커널 재시작 없이 다음 Step에서 import된다.

## 트레이드오프와 선행 조건

- **의존성 충돌**: `--target`은 표준 venv에 설치된 패키지를 고려하지 않아 numpy 등을 다시 받을 수 있다.
  표준 환경 `uv.lock`에서 만든 constraints로 버전을 고정해야 한다. constraints 파일은 현재 이미지에 없으며
  Dockerfile에서 `uv export --project /opt/jupyter-env/<프로필> --no-hashes`로 생성해야 한다.
- **디스크 사용량**: 중복 의존성이 설치되면 실행마다 수십~수백 MB가 PVC에 쌓일 수 있다.
  `--no-deps` 적용 여부와 정리 정책이 필요하다. 실제 중복 여부는 미검증이다.
- **보안**: Agent가 고른 이름을 그대로 설치하면 typosquatting 등 공급망 위험이 있다. 허용 목록이 필수다.
  폐쇄망 내부 미러(`UV_DEFAULT_INDEX`)에 있는 패키지만 설치 가능하다는 점은 추가 안전장치다.
- **모듈명·패키지명 불일치**: `sklearn` → `scikit-learn`, `cv2` → `opencv-python`처럼 Agent가 추측해야 한다.
- **시간**: 설치 시간이 실행 타임아웃에 포함되고 재시도마다 반복된다. 이미지에 `UV_NO_CACHE=1`이 설정되어 매번 다운로드한다.
- **네트워크**: 런타임 Pod에서 내부 미러에 접근할 수 있어야 한다.

## 결론

- 자주 쓰는 패키지는 표준 환경 `uv.lock`에 추가하고 이미지를 재빌드한다.
- 실행 중 추가 설치가 요구사항이 되면 선택지 3을 Executor 주도 설계로 구현한다.
- 허용 목록과 버전 고정 없이 도입하면 운영 리스크가 크다.

## 구현 전 검증 항목

- `uv pip install --target` + constraints 조합에서 표준 venv 의존성 중복 설치 여부와 디스크 사용량.
- `.pth` 기반 경로 추가가 `default`(3.11), `3102311`(3.10) 커널 모두에서 동작하는지.
- 실행 중 `.pkgs`에 추가한 패키지가 커널 재시작 없이 import되는지.
