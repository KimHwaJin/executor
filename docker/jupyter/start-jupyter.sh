#!/bin/sh
set -eu

if [ -z "${JUPYTER_TOKEN:-}" ]; then
    echo "JUPYTER_TOKEN must be set at container runtime." >&2
    exit 1
fi

if [ -z "${JUPYTER_ROOT_DIR:-}" ]; then
    echo "JUPYTER_ROOT_DIR must not be empty." >&2
    exit 1
fi

exec /opt/venvs/jupyter/bin/jupyter lab "$@"
