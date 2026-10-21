ARG PYTHON312_IMAGE=astral/uv:python3.12-bookworm-slim
ARG UV_DEFAULT_INDEX=https://pypi.org/simple
ARG PYTHON310_VERSION=3.10.11
ARG PYTHON311_VERSION=3.11.16

FROM ${PYTHON312_IMAGE} AS python-build

ARG PYTHON310_VERSION
ARG PYTHON311_VERSION
ARG PYTHON_BUILD_JOBS=2

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential \
        pkg-config \
        xz-utils \
        libssl-dev \
        zlib1g-dev \
        libbz2-dev \
        liblzma-dev \
        libffi-dev \
        libsqlite3-dev \
        libreadline-dev \
        libncurses-dev \
        libgdbm-dev \
        libgdbm-compat-dev \
        libexpat1-dev \
        uuid-dev \
    && rm -rf /var/lib/apt/lists/*

# Official source tarballs must be supplied in the build context beforehand.
# No Python download occurs in this Dockerfile.
COPY python-sources/Python-${PYTHON310_VERSION}.tar.xz /tmp/python310.tar.xz
RUN mkdir -p /tmp/python310-src \
    && tar -xJf /tmp/python310.tar.xz -C /tmp/python310-src --strip-components=1 \
    && cd /tmp/python310-src \
    && ./configure --prefix=/opt/python/3.10 --with-ensurepip=no --with-system-expat \
    && make -j "${PYTHON_BUILD_JOBS}" \
    && make altinstall

COPY python-sources/Python-${PYTHON311_VERSION}.tar.xz /tmp/python311.tar.xz
RUN mkdir -p /tmp/python311-src \
    && tar -xJf /tmp/python311.tar.xz -C /tmp/python311-src --strip-components=1 \
    && cd /tmp/python311-src \
    && ./configure --prefix=/opt/python/3.11 --with-ensurepip=no --with-system-expat \
    && make -j "${PYTHON_BUILD_JOBS}" \
    && make altinstall

FROM ${PYTHON312_IMAGE}

ARG UV_DEFAULT_INDEX

# Deployment settings: override these with container environment variables.
# ROOT_DIR must match the shared PVC mount path and be writable by UID 1000.
# TOKEN defaults to "default" for testing; override via Secret in production.
# See README.md section 5 before deployment.
ENV JUPYTER_ROOT_DIR=/workspace/jupyter \
    JUPYTER_TOKEN=default

ENV DEBIAN_FRONTEND=noninteractive \
    PATH="/opt/venvs/jupyter/bin:${PATH}" \
    HOME=/home/jovyan \
    JUPYTER_CONFIG_DIR=/home/jovyan/.jupyter \
    JUPYTER_PATH=/opt/venvs/jupyter/share/jupyter \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        curl \
        fonts-dejavu-core \
        libgomp1 \
        libssl3 \
        zlib1g \
        libbz2-1.0 \
        liblzma5 \
        libffi8 \
        libsqlite3-0 \
        libreadline8 \
        libncursesw6 \
        libgdbm6 \
        libgdbm-compat4 \
        libexpat1 \
        libuuid1 \
        tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=python-build /opt/python /opt/python

COPY environments/server /opt/jupyter-env/server
COPY environments/default /opt/jupyter-env/default
COPY environments/3102311 /opt/jupyter-env/3102311

RUN UV_PROJECT_ENVIRONMENT=/opt/venvs/jupyter \
        uv sync --project /opt/jupyter-env/server \
            --locked --no-dev --no-install-project \
            --python /usr/local/bin/python3.12 \
    && UV_PROJECT_ENVIRONMENT=/opt/venvs/default \
        uv sync --project /opt/jupyter-env/default \
            --locked --no-dev --no-install-project \
            --python /opt/python/3.11/bin/python3.11 \
    && UV_PROJECT_ENVIRONMENT=/opt/venvs/3102311 \
        uv sync --project /opt/jupyter-env/3102311 \
            --locked --no-dev --no-install-project \
            --python /opt/python/3.10/bin/python3.10

COPY extension /opt/jupyter-resource-extension
COPY executor_resource_extension.json \
    /opt/venvs/jupyter/etc/jupyter/jupyter_server_config.d/executor_resource_extension.json

RUN uv pip install --strict --python /opt/venvs/jupyter/bin/python \
        --no-deps /opt/jupyter-resource-extension \
    && /opt/venvs/default/bin/python -m ipykernel install \
        --prefix=/opt/venvs/jupyter \
        --name=default \
        --display-name="Default (Python 3.11)" \
    && /opt/venvs/3102311/bin/python -m ipykernel install \
        --prefix=/opt/venvs/jupyter \
        --name=3102311 \
        --display-name="3102311 (Python 3.10.11)" \
    && rm -rf /opt/venvs/jupyter/share/jupyter/kernels/python3

RUN rm -rf /home/jovyan \
    && groupadd --gid 1000 jovyan \
    && useradd \
        --uid 1000 \
        --gid 1000 \
        --create-home \
        --shell /bin/bash \
        jovyan \
    && mkdir -p "${JUPYTER_ROOT_DIR}" /home/jovyan/.jupyter \
    && chown -R 1000:1000 "${JUPYTER_ROOT_DIR}" /home/jovyan

COPY --chown=1000:1000 jupyter_server_config.py \
    /home/jovyan/.jupyter/jupyter_server_config.py
COPY --chmod=755 start-jupyter.sh /usr/local/bin/start-jupyter

USER 1000:1000
WORKDIR "${JUPYTER_ROOT_DIR}"

EXPOSE 8888

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/start-jupyter"]
