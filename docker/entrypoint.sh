#!/bin/sh
set -eu

ROLE="${ROLE:-api}"

# The deployed Job gets mercury.yaml as a base64 secret, since Container Apps
# has no way to mount a file from a private repo. Local compose mounts the
# sample file instead and leaves this unset.
if [ -n "${MERCURY_CONFIG_B64:-}" ]; then
  mkdir -p /config
  echo "$MERCURY_CONFIG_B64" | base64 -d > /config/mercury.yaml
fi

case "$ROLE" in
  api)
    exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
    ;;
  worker)
    exec python -m app.worker
    ;;
  scheduler)
    exec python -m app.scheduler
    ;;
  *)
    echo "Unknown ROLE '$ROLE'. Expected api, worker or scheduler." >&2
    exit 1
    ;;
esac
