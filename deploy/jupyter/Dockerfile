# Docker Hub's official Python image; the digest pins the exact build.
ARG PYTHON312_IMAGE=python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
ARG UV_VERSION=0.11.32
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
ARG UV_VERSION

# The base image has no uv: install the pinned version from the same index.
RUN pip install --no-cache-dir --index-url "${UV_DEFAULT_INDEX}" "uv==${UV_VERSION}"

# Deployment settings: override these with container environment variables.
# ROOT_DIR must match the shared PVC mount path and be writable by UID 1000.
# JUPYTER_TOKEN has no image default: inject a distinct token per server at
# runtime, or start-jupyter.sh refuses to start. See README.md section 5.
ENV JUPYTER_ROOT_DIR=/workspace/jupyter

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

COPY environments /opt/jupyter-env
COPY install-environments.sh /tmp/install-environments.sh
RUN sh /tmp/install-environments.sh && rm /tmp/install-environments.sh

COPY extension /opt/jupyter-resource-extension
COPY executor_resource_extension.json \
    /opt/venvs/jupyter/etc/jupyter/jupyter_server_config.d/executor_resource_extension.json

RUN uv pip install --strict --python /opt/venvs/jupyter/bin/python \
        --no-deps /opt/jupyter-resource-extension

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
