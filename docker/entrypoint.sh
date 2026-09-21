#!/bin/sh
set -eu

ROLE="${ROLE:-api}"

case "$ROLE" in
  api)
    exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
    ;;
  worker)
    exec python -m app.worker
    ;;
  *)
    echo "Unknown ROLE '$ROLE'. Expected api or worker." >&2
    exit 1
    ;;
esac
