# 빌드에 필요한 Python 공식 소스

이 디렉토리에 다음 두 파일을 넣고 상위 디렉토리에서 Docker 이미지를 빌드한다.

- `Python-3.10.11.tar.xz`: https://www.python.org/downloads/release/python-31011/
- `Python-3.11.16.tar.xz`: https://www.python.org/downloads/release/python-31116/

공식 페이지의 **XZ compressed source tarball**을 받는다. Windows 설치 파일이나
임의의 가상환경 압축본을 넣는 곳이 아니다. 공식 서명/체크섬 및 사내 반입 절차로
검증한 파일을 사용한다. Docker 빌드 중 외부에서 Python을 다운로드하지 않는다.

tar 파일은 이 저장소에서 Git에 커밋하지 않는다. 디렉토리를 별도로 전달할 때는
반드시 두 tar를 함께 포함하거나, Jenkins가 사내 저장소에서 이 위치에 준비해야 한다.
Git clone만으로는 tar가 없으므로 빌드할 수 없다.

Python 3.11.16은 2026-09-08 기준 확인한 3.11 최신 릴리스다. 이후 변경하려면
Dockerfile의 `PYTHON311_VERSION`과 실제 파일명을 함께 맞춘다.
3.10.11은 요청에 따른 고정 버전이며 이후 보안 수정을 포함하지 않으므로 운영 반입
승인이 필요하다. 3.10 버전을 변경할 경우 해당 pyproject 및 uv.lock도 함께 갱신한다.
