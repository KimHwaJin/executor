#!/bin/sh
# Installs the Jupyter server and every kernel environment from its uv.lock.
# environments/server is the server. Every other directory is a kernel: its
# name is the kernelspec ID and its kernel.env sets KERNEL_PYTHON and
# KERNEL_DISPLAY_NAME.
set -eu

ENV_ROOT=/opt/jupyter-env
SERVER_VENV=/opt/venvs/jupyter

# uv sync downloads straight from the lock's file URLs and never requests
# /simple, so a Nexus PyPI proxy that has not seen a package's index page
# answers 404. Requesting each index page first lets Nexus learn the remote
# file locations.
warm_index() {
    uv export --project "$1" --frozen --no-dev --no-hashes --no-emit-project \
        | grep -oE '^[A-Za-z0-9._-]+==' | sed 's/==$//' | tr 'A-Z_.' 'a-z--' \
        | while read -r pkg; do
            curl -fsS -o /dev/null "${UV_DEFAULT_INDEX%/}/${pkg}/" \
                || echo "index warm-up failed: ${pkg}" >&2
        done
}

sync_env() {
    warm_index "$1"
    UV_PROJECT_ENVIRONMENT="$2" uv sync --project "$1" \
        --locked --no-dev --no-install-project --python "$3"
}

sync_env "${ENV_ROOT}/server" "${SERVER_VENV}" /usr/local/bin/python3.12

for project in "${ENV_ROOT}"/*/; do
    project=${project%/}
    name=${project##*/}
    [ "${name}" = server ] && continue
    (
        . "${project}/kernel.env"
        sync_env "${project}" "/opt/venvs/${name}" "${KERNEL_PYTHON}"
        "/opt/venvs/${name}/bin/python" -m ipykernel install \
            --prefix="${SERVER_VENV}" \
            --name="${name}" \
            --display-name="${KERNEL_DISPLAY_NAME}"
    )
done

rm -rf "${SERVER_VENV}/share/jupyter/kernels/python3"
