# Jupyter Dockerfile 상세 가이드

이 디렉토리만 별도로 전달하여 Jenkins에서 빌드하고 Harbor에 올리는 패키지다.
아래 설명은 Dockerfile 순서대로 구성한다. 빌드 중 별도 검증 스크립트는 실행하지 않는다.

## 1. 빌드 인자

| ARG | 목적 |
|---|---|
| `PYTHON312_IMAGE` | Python 3.12와 uv가 설치된 Debian Bookworm Slim 이미지. 빌드·최종 스테이지 공통 |
| `UV_DEFAULT_INDEX` | Python 패키지 인덱스. 폐쇄망에서는 lock 생성 시 쓴 Nexus 주소 |
| `PYTHON310_VERSION=3.10.11` | 반입한 3.10 공식 소스 파일명 |
| `PYTHON311_VERSION=3.11.16` | 반입한 3.11 공식 소스 파일명 |

Docker가 최신 Python을 검색하거나 소스를 다운로드하지 않는다. 3.11.16은 2026-09-08
기준 확인한 최신 3.11이다. 3.10.11은 요구사항에 따른 고정 버전이며 이후 보안 수정이
빠져 있으므로 운영 반입 승인이 필요하다.

`FROM` 앞의 전역 ARG를 RUN에서 쓰려면 해당 스테이지에서 다시 선언해야 한다.
계정·비밀번호·토큰을 ARG나 URL에 넣지 않는다.

## 2. Python 소스 빌드 스테이지

`FROM ${PYTHON312_IMAGE} AS python-build`는 컴파일용 스테이지다.
`PYTHON_BUILD_JOBS=2`는 `make -j`의 병렬도이며 Jenkins CPU·메모리에 맞춰 조절한다.
Jupyter의 실행 동시성과는 무관하다.

### apt 개발 패키지

`apt-get update`로 목록을 갱신하고 `--no-install-recommends`로 추가 권장 패키지는
설치하지 않는다. 같은 RUN 끝에서 apt 목록을 삭제한다.

| 패키지 | 목적 |
|---|---|
| `build-essential`, `pkg-config` | C 컴파일러·make 및 라이브러리 탐색 |
| `xz-utils` | 공식 tar.xz 압축 해제 |
| `libssl-dev` | ssl·hashlib 등 OpenSSL 모듈 |
| `zlib1g-dev`, `libbz2-dev`, `liblzma-dev` | 압축 모듈 |
| `libffi-dev`, `libsqlite3-dev` | ctypes, sqlite3 |
| `libreadline-dev`, `libncurses-dev` | readline 및 터미널 모듈 |
| `libgdbm-dev`, `libgdbm-compat-dev` | dbm |
| `libexpat1-dev`, `uuid-dev` | XML 파서와 UUID 지원 |

폐쇄망 apt 미러에는 이 개발 패키지도 있어야 한다. GUI용 Tk 개발 패키지는 포함하지
않으므로 tkinter 지원을 전제하지 않는다.

### Python별 COPY 및 컴파일 RUN

| 구문 | 의도 |
|---|---|
| `COPY python-sources/Python-${VERSION}.tar.xz ...` | 빌드 컨텍스트에 사전 반입한 파일 복사. 없으면 즉시 실패 |
| `mkdir -p /tmp/python...-src` | 소스 작업 디렉토리 준비 |
| `tar -xJf ... --strip-components=1` | xz 압축을 풀고 최상위 Python 버전 폴더 한 겹 제거 |
| `cd /tmp/python...-src` | 이후 configure·make의 작업 위치 |
| `./configure --prefix=/opt/python/3.10` | 시스템 Python과 분리된 설치 경로. 3.11은 별도 prefix |
| `--with-ensurepip=no` | pip 부트스트랩 생략. 패키지는 uv로 설치 |
| `--with-system-expat` | apt에서 관리하는 XML 파서 라이브러리 사용 |
| `make -j "${PYTHON_BUILD_JOBS}"` | 대상 CPU에 맞는 Python과 모듈 컴파일 |
| `make altinstall` | 버전 명령 설치. 시스템 python3를 덮어쓰지 않음 |

3.10과 3.11을 독립 RUN으로 나누어 뒤쪽 소스만 바뀌면 앞쪽 빌드 캐시를 재사용한다.
PGO/LTO 최적화나 별도 테스트 실행은 추가하지 않는다. CPython 자체를 shared libpython으로
빌드하지 않으므로 libpython을 위한 LD_LIBRARY_PATH·ldconfig 설정은 추가하지 않는다.

## 3. 최종 스테이지

`FROM ${PYTHON312_IMAGE}`로 동일 베이스의 새 스테이지를 시작한다. 빌드 도구와 소스는
자동 승계되지 않으며, 뒤에서 설치 결과 `/opt/python`만 복사한다.

### 환경변수

| ENV | 목적 |
|---|---|
| `JUPYTER_ROOT_DIR=/workspace/jupyter` | 노트북·아티팩트 작업공간 기본값. 배포 환경변수로 변경 가능 |
| `JUPYTER_TOKEN=default` | 테스트 기본값. 운영에서는 반드시 별도 토큰 주입 |
| `DEBIAN_FRONTEND=noninteractive` | apt 설치 시 대화형 입력 방지 |
| `PATH=/opt/venvs/jupyter/bin:...` | 셸의 기본 Python은 서버 3.12 환경 |
| `HOME=/home/jovyan` | 사용자 설정·캐시 위치. 작업공간 루트와 별개 |
| `JUPYTER_CONFIG_DIR` | 서버 설정 파일 위치 |
| `JUPYTER_PATH` | 서버가 kernelspec을 찾는 위치 |
| `UV_COMPILE_BYTECODE=1` | 패키지 bytecode 사전 생성 |
| `UV_LINK_MODE=copy` | 캐시와 가상환경 간 hardlink 대신 복사 |
| `UV_NO_CACHE=1` | uv 다운로드 캐시를 이미지에 쌓지 않음 |
| `UV_PYTHON_DOWNLOADS=never` | uv의 추가 Python 다운로드 차단 |

### 런타임 라이브러리와 Python 복사

컴파일된 확장 모듈이 참조하는 OpenSSL, zlib, bz2, lzma, ffi, SQLite, readline/ncurses,
gdbm, Expat, uuid의 **런타임 라이브러리**를 설치한다. 컴파일러·개발용 헤더는 최종
스테이지에 설치하지 않는다.

`ca-certificates`는 HTTPS, `curl`은 헬스체크·진단, `fonts-dejavu-core`는 그림 폰트,
`libgomp1`은 분석 패키지 OpenMP, `tini`는 종료 신호 전달·자식 프로세스 회수에 필요하다.

`COPY --from=python-build /opt/python /opt/python`은 두 Python의 실행파일뿐 아니라
표준 라이브러리·확장 모듈·헤더를 포함한 설치 결과를 동일 경로로 옮긴다. 소스 tar,
컴파일러, `/tmp` 소스 작업 폴더는 최종 이미지로 복사하지 않는다.

## 4. uv 프로젝트 동기화

`COPY environments/...`는 환경마다 독립적인 pyproject.toml과 uv.lock을 가져온다.

| 환경 | `--python` 인터프리터 | `UV_PROJECT_ENVIRONMENT` 설치 경로 |
|---|---|---|
| 서버 | `/usr/local/bin/python3.12` | `/opt/venvs/jupyter` |
| default | `/opt/python/3.11/bin/python3.11` | `/opt/venvs/default` |
| 3102311 | `/opt/python/3.10/bin/python3.10` | `/opt/venvs/3102311` |

`--project`는 환경 정의 위치다. `--locked`는 lock이 없거나 갱신이 필요하면 실패시킨다.
`--no-dev`는 개발 의존성 제외, `--no-install-project`는 환경 정의 자체를 패키지로
설치하지 않는 옵션이다. 서버와 커널의 site-packages는 공유하지 않는다.

## 5. 커스텀 확장과 kernelspec

확장 코드와 활성화 JSON을 서버 환경에 복사한다. `uv pip install --strict --no-deps`로
확장을 설치하며 서버 lock 밖에서 의존성 버전을 바꾸지 않는다. 확장 빌드용 setuptools
등 build-system 의존성도 Nexus에서 제공해야 한다.

각 커널의 Python으로 `-m ipykernel install`을 실행한다. `--prefix`는 등록 위치,
`--name`은 API에서 쓰는 커널 ID, `--display-name`은 UI 표시 이름이다.
kernelspec의 argv가 각 가상환경 Python을 가리키므로 서버가 3.12여도 셀은 선택한
커널의 3.11.16 또는 3.10.11로 실행된다. 자동 생성된 python3 kernelspec만 삭제한다.
기본 커널 및 허용 목록은 jupyter_server_config.py의 default, 3102311을 유지한다.
커스텀 자원·스토리지 API는 커널이 아닌 서버 3.12 프로세스에서 실행한다.

## 6. 비-root 사용자와 권한

- `rm -rf /home/jovyan`: 이미지 빌드 내부의 기존 홈 정리. 배포 PVC가 아니다.
- `groupadd --gid 1000`, `useradd --uid 1000`: 고정 UID/GID의 실행 사용자 생성.
- `--create-home`, `--shell /bin/bash`: 홈과 터미널 셸 준비.
- `mkdir -p`: 작업공간과 서버 설정 디렉토리 생성.
- `chown -R`: 이미지 안의 작업공간·홈 소유권을 실행 사용자에게 부여.
- `COPY --chown=1000:1000`: 설정 파일을 같은 사용자 소유로 복사.
- `COPY --chmod=755`: 시작 스크립트에 읽기·실행 권한 부여.
- `USER 1000:1000`: 이후 서버와 커널은 root가 아닌 사용자로 실행.
- `WORKDIR`: 빌드 시 작업공간을 기본 작업 디렉토리로 지정.

이미지 chown은 실제 PVC 권한을 보장하지 않는다. PVC가 마운트되면 기존 디렉토리가
가려지므로 운영에서 UID/GID 1000의 읽기·쓰기·탐색 권한을 별도로 맞춘다.
`/opt/python`과 `/opt/venvs`는 root 소유이며 일반 사용자가 표준 환경을 수정하지 않는다.

## 7. 컨테이너 실행 및 변경 체크

`EXPOSE 8888`은 메타데이터이며 Service를 만들지 않는다. `ENTRYPOINT`의 tini가
start-jupyter를 실행하고 스크립트는 루트·토큰 확인 후 JupyterLab을 실행한다.
컨테이너 시작 시 uv sync나 Python 컴파일은 하지 않는다.

| 변경 | 같이 확인할 것 |
|---|---|
| 3.12 베이스 | Debian Bookworm, uv 버전, 승인 image digest |
| 소스 tar | Docker ARG, 실제 파일명, Python 범위와 해당 uv.lock |
| 패키지 | pyproject와 uv.lock을 빌드 전에 함께 갱신·커밋 |
| Nexus | 세 lock 인덱스와 Docker 빌드 인자 일치 |
| apt 미러 | 개발 패키지와 런타임 라이브러리 모두 제공 |
| UID/GID·루트 | PVC 권한, mountPath와 컨테이너 환경변수 |

소스 반입 위치와 실제 Jenkins/Harbor 빌드 명령은 README 4절을 따른다.
