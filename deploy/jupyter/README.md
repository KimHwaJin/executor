# Executor용 Jupyter 이미지

Dockerfile의 각 구문, 권한 설정, PVC 적용 시 주의사항은
[`DOCKERFILE_GUIDE.md`](DOCKERFILE_GUIDE.md)에 상세히 정리되어 있다.

**배포 전 Dockerfile 상단의 `JUPYTER_ROOT_DIR`, `JUPYTER_TOKEN`과
아래 5번 배포 설정을 확인한다. 별도 주입이 없으면 토큰은 `default`로 실행된다.**

**저장소에 포함된 최초 `uv.lock`은 공개 PyPI 기준이다. 폐쇄망에 반입한 뒤에는 이미지
빌드보다 먼저 아래 3번 절차로 실제 Nexus 기준 lock을 생성하고 Git에 커밋한다.**

이 문서에서 작업공간 루트는 `${JUPYTER_ROOT_DIR}`로 표시한다.
배포 환경에 따라 달라질 수 있는 경로이며, 이미지 기본값은 `/workspace/jupyter`다.

## 1. 파일 구성과 수정할 곳

| 파일 | 역할 | 수정이 필요한 경우 |
|---|---|---|
| `Dockerfile` | Bookworm 기반 Python·uv 이미지 조립, lock 기반 환경 동기화, 커널 등록, 실행 사용자 설정 | 베이스 이미지, 패키지 인덱스, apt 미러, OS 패키지, Python 버전, UID/GID 변경 |
| `environments/server/pyproject.toml`, `uv.lock` | Jupyter 서버 의존성과 확정 버전 | JupyterLab·Jupyter Server 버전 변경 후 lock 갱신 |
| `environments/default/pyproject.toml`, `uv.lock` | `default` 커널 의존성과 확정 버전 | 분석 라이브러리 변경 후 lock 갱신 |
| `environments/3102311/pyproject.toml`, `uv.lock` | `3102311` 커널 의존성과 확정 버전 | 승인된 Python 3.10 패키지 추가 후 lock 갱신 |
| `jupyter_server_config.py` | 루트·토큰 적용, 포트, 허용 커널, 기본 커널 설정 | 포트나 커널 정책 변경. 루트·토큰은 파일 수정 없이 환경변수로 지정 |
| `start-jupyter.sh` | 필수 환경변수 확인 후 JupyterLab 실행 | 일반적으로 수정하지 않음 |
| `executor_resource_extension.json` | Executor 연동 확장 활성화 | 그대로 유지 |
| `extension/pyproject.toml`, `extension/src/` | 자원 조회, 작업공간 준비, 노트북 작성, 파일 다운로드 기능 | Executor 연동 코드이므로 일반 배포 시 수정하지 않음 |
| `.dockerignore` | 비밀 설정·캐시·작업 데이터를 빌드 컨텍스트에서 제외 | 일반적으로 수정하지 않음 |

**`server`, `default`, `3102311`은 서로 독립된 uv 프로젝트다.** 한 디렉토리를 uv
workspace로 묶지 않으며 각각 자신의 `pyproject.toml`과 `uv.lock`을 소유한다.
`default`와 `3102311`은 패키지 설치 경로도 공유하지 않는다. `3102311`에 향후 제공받을
목록을 적용할 때는 기존 `ipykernel`을 유지하고 같은 `dependencies` 배열에 추가한다.
`3102311`은 외부 계약에 사용되는 kernelspec ID이므로 유지하지만, 실제 Python patch
버전은 `PYTHON310_IMAGE`에 포함된 3.10.x 버전을 따른다.

`pyproject.toml`은 사람이 관리하는 직접 의존성과 Python 범위를 정의한다. `uv.lock`은 uv가
결정한 간접 의존성, 정확한 버전과 배포 파일 해시를 기록하며 반드시 Git에 함께 커밋한다.
`uv.lock`은 직접 수정하지 않는다.

`extension/pyproject.toml`은 커스텀 확장을 uv로 빌드·설치하기 위한 필수 파일이다.
별도의 패키징 명령은 필요하지 않으며 Dockerfile의 `uv pip install`이 처리한다.

## 2. Dockerfile 동작

빌드 시 다음 순서로 구성한다.

1. uv와 Python 3.10이 포함된 Bookworm Slim 이미지에서 `3102311` 커널 환경을 만든다.
2. uv와 Python 3.11이 포함된 같은 Bookworm Slim 계열의 최종 이미지에 Python 3.10
   런타임과 `3102311` 환경을 포함한다. 두 이미지는 사내 Harbor에 반입하고
   `PYTHON310_IMAGE`, `PYTHON311_IMAGE` 빌드 인자로 주소를 지정한다. 최종 이미지에는
   폰트, OpenMP 런타임, 헬스체크용 `curl`, 프로세스 종료 신호 처리를 위한 `tini`도
   설치한다.
3. 아래 세 환경을 각 디렉토리의 `pyproject.toml`과 `uv.lock`으로부터
   `uv sync --locked --no-install-project`로 설치한다. lock이 없거나 의존성 정의와
   일치하지 않으면 이미지 빌드가 실패한다.
   `default`는 Python 3.11, `3102311`은 Python 3.10.x이며 서로의 패키지 설치
   경로를 공유하지 않는다.

   | 용도 | Python | 이미지 내부 경로 |
   |---|---|---|
   | JupyterLab 서버 | 3.11 | `/opt/venvs/jupyter` |
   | `default` 커널 | 3.11 | `/opt/venvs/default` |
   | `3102311` 커널 | 3.10.x | `/opt/venvs/3102311` |

4. 서버 가상환경에 `extension/`을 uv로 설치하고 확장을 활성화한다.
5. `default`, `3102311` kernelspec을 서버에 등록하고 불필요한 기본 `python3` kernelspec을
   제거한다. 허용 커널은 두 개뿐이며 기본 선택은 `default`다.
6. UID/GID `1000:1000` 사용자를 만들고 설정 파일과 시작 스크립트를 복사한다.
7. `${JUPYTER_ROOT_DIR}` 디렉토리를 생성하고 해당 사용자에게 권한을 부여한다.
   이미지의 기본 작업 디렉토리(`WORKDIR`)도 빌드 시 이 값을 사용한다.

컨테이너 시작 시 `tini`가 `start-jupyter.sh`를 실행한다. 스크립트는 토큰과 루트 경로가
비어 있지 않은지 확인한 뒤 JupyterLab을 실행한다. 서버 설정 파일은 환경변수를 읽고
`0.0.0.0:8888`에서 요청을 받도록 설정한다.

자원 조회 확장은 서버 프로세스에서 cgroup을 읽는다.

두 Python 이미지는 Debian 12 Bookworm Slim으로 통일한다. 서로 다른 Debian 세대의
이미지를 조합하지 않으며, 내부 반입 시 동일한 uv 버전과 승인된 image digest를 사용한다.

## 3. 패키지와 lock 관리

아래 명령은 모두 이 폴더를 현재 디렉토리로 두고 실행한다. uv 0.12.8을 사용한다.

패키지를 추가하거나 삭제하면 `pyproject.toml`과 `uv.lock`이 함께 변경된다.

```shell
UV_DEFAULT_INDEX=https://nexus.example.com/repository/pypi-group/simple/ \
uv add --project environments/default "xgboost==3.0.5"

UV_DEFAULT_INDEX=https://nexus.example.com/repository/pypi-group/simple/ \
uv remove --project environments/default xgboost
```

`pyproject.toml`을 직접 수정했거나 최초 Nexus lock을 생성할 때는 다음 명령을 실행한다.

```shell
UV_DEFAULT_INDEX=https://nexus.example.com/repository/pypi-group/simple/ \
uv lock --project environments/server
UV_DEFAULT_INDEX=https://nexus.example.com/repository/pypi-group/simple/ \
uv lock --project environments/default
UV_DEFAULT_INDEX=https://nexus.example.com/repository/pypi-group/simple/ \
uv lock --project environments/3102311
```

커밋 또는 이미지 빌드 전에 lock 정합성을 확인한다.

```shell
UV_DEFAULT_INDEX=https://nexus.example.com/repository/pypi-group/simple/ \
uv lock --project environments/server --check
UV_DEFAULT_INDEX=https://nexus.example.com/repository/pypi-group/simple/ \
uv lock --project environments/default --check
UV_DEFAULT_INDEX=https://nexus.example.com/repository/pypi-group/simple/ \
uv lock --project environments/3102311 --check
```

패키지 변경 커밋에는 대상 환경의 `pyproject.toml`과 `uv.lock`을 항상 함께 포함한다.
빌드 중에는 lock을 생성하거나 갱신하지 않는다.

## 4. 빌드와 이미지 업로드

**이 README와 Dockerfile이 있는 폴더에서** 실행한다. 주소·태그는 실제 값으로 바꾼다.

```shell
docker build \
  --build-arg PYTHON310_IMAGE=harbor.example.com/library/uv:python3.10-bookworm-slim \
  --build-arg PYTHON311_IMAGE=harbor.example.com/library/uv:python3.11-bookworm-slim \
  --build-arg UV_DEFAULT_INDEX=https://nexus.example.com/repository/pypi-group/simple/ \
  -t ${image.tag} .

docker push ${image.tag}
```

빌드에는 베이스 이미지, apt 저장소, Python 패키지 인덱스 접근이 필요하다.
폐쇄망에서는 Python과 uv가 함께 든 두 이미지를 사내 Harbor에 반입하고, apt 미러와
Nexus PyPI 저장소를 준비해야 한다.

- `PYTHON310_IMAGE`는 `3102311` 커널을 만드는 Python 3.10 Bookworm Slim 이미지다.
- `PYTHON311_IMAGE`는 Jupyter 서버와 `default` 커널을 실행하는 최종 Python 3.11
  Bookworm Slim 이미지다.
- `UV_DEFAULT_INDEX`는 lock 검증과 빌드 중 모든 uv 패키지 설치가 사용하는 유일한 기본
  패키지 인덱스다. 기본값은 최초 lock과 일치하는 공개 PyPI이며, 폐쇄망에서는 실제
  Nexus 주소로 반드시 덮어쓴다.
- uv는 `pip.conf`를 읽지 않는다. 인덱스 설정은 반드시 `UV_DEFAULT_INDEX`로 전달한다.
- 세 값은 이미지 실행 설정이 아니라 빌드 인자다. 계정·비밀번호·토큰을 URL이나
  Dockerfile에 넣지 않는다.
- lock을 만들 때 사용한 패키지 인덱스와 이미지 빌드의 `UV_DEFAULT_INDEX`가 같아야 한다.
  사내 Nexus로 전환하는 최초 1회에는 세 lock을 Nexus 기준으로 다시 생성하고 커밋한다.

## 5. 배포할 때 지정할 값

Dockerfile 상단에 두 설정의 이미지 기본값을 모아 두었다.

```dockerfile
ENV JUPYTER_ROOT_DIR=/workspace/jupyter \
    JUPYTER_TOKEN=default
```

별도 환경변수 주입이 없으면 `${JUPYTER_ROOT_DIR}`은 위 기본값을 사용하고,
토큰은 `default`로 실행된다.
운영 배포 시에는 Secret으로 별도 토큰을 주입한다.


| 항목 | 설정 |
|---|---|
| 이미지 | 위에서 빌드·업로드한 태그 |
| `JUPYTER_TOKEN` | 기본 `default`. 운영에서는 플랫폼 Secret으로 덮어씀. 빈 값이면 시작 실패 |
| `JUPYTER_ROOT_DIR` | 작업공간 루트 `${JUPYTER_ROOT_DIR}`. 실제 공유 PVC 마운트 경로와 일치시킴 |
| `UV_DEFAULT_INDEX` | 선택. Jupyter 터미널에서 PV 아래 사용자 환경을 만들 때 사용할 Nexus 주소. 표준 커널 빌드 인자와 별개의 런타임 주입값 |
| 포트 | 기본 `8888`. Service 대상 포트도 일치시킴 |
| 실행 사용자 | UID/GID `1000:1000`. PVC에 디렉토리·파일 생성 및 수정 권한 필요 |
| 시작 명령 | 이미지 ENTRYPOINT 그대로 사용 |
| 서버 구성 | 서버별 replicas `1`, Executor가 접근할 수 있는 고유 Service 주소 |

기본값을 변경하려면 Kubernetes의 같은 namespace에 `jupyter-config` ConfigMap과
`jupyter-secret` Secret을 만들고 Deployment의 컨테이너에 아래처럼 연결한다.
각 리소스에는 아래 `key`와 동일한 이름으로 값을 등록한다.
리소스를 만들기만 하거나 파일로 마운트하는 것으로는 환경변수가 주입되지 않는다.

```yaml
# Deployment의 spec.template.spec.containers[] 항목 아래
env:
  - name: JUPYTER_ROOT_DIR
    valueFrom:
      configMapKeyRef:
        name: jupyter-config
        key: JUPYTER_ROOT_DIR
  - name: JUPYTER_TOKEN
    valueFrom:
      secretKeyRef:
        name: jupyter-secret
        key: JUPYTER_TOKEN
  - name: UV_DEFAULT_INDEX
    valueFrom:
      configMapKeyRef:
        name: jupyter-config
        key: UV_DEFAULT_INDEX
        optional: true
```

여러 Jupyter 서버는 동일한 공유 스토리지의 동일한 작업공간을 바라보도록 배포한다.
서로 다른 서버의 커널 세션이 섞이지 않도록 여러 Jupyter Pod를 하나의 Service로 무작위 분산하지 않는다.

`HOME=/home/jovyan`은 Jupyter 설정·사용자 데이터 경로이며 작업공간 루트와 다르다.
홈 전체에 빈 볼륨을 마운트하면 내장 `.jupyter/jupyter_server_config.py`가 가려지므로
주의한다. read-only root filesystem을 사용하면 설정을 보존하면서 Jupyter가 쓰는
홈 하위 경로 및 `/tmp`의 쓰기 공간도 별도로 제공해야 한다.

배포 후 Executor에 해당 서버의 endpoint와 토큰을 등록한다.
Executor에서 REST 및 WebSocket으로 해당 서버에 접근할 수 있어야 한다.

이미지에 uv 명령은 포함되지만 `/opt/venvs`의 표준 환경은 일반 사용자가 수정할 수 없다.
추가 환경이 필요한 사용자는 정책이 허용하는 경우에만 쓰기 가능한 PVC 경로 아래에 별도
환경을 만든다. 표준 커널 패키지 변경은 항상 `pyproject.toml`, `uv.lock`, 이미지 재빌드로
처리한다.
