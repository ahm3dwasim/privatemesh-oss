#!/bin/sh
# The one place the model key touches this image. `NEBIUS_API_KEY_FILE` is a path compose.yaml
# mounts from a docker secret (a file on the host, never committed, never a build ARG or image
# layer); this reads it into the env var the app actually looks at (`config.Settings.load`) and
# then runs the real command. With no secret mounted and no `NEBIUS_API_KEY` already set — the
# local-proof-without-a-key case — this is a no-op and the app's own startup refusal handles it.
set -eu

if [ -z "${NEBIUS_API_KEY:-}" ] && [ -n "${NEBIUS_API_KEY_FILE:-}" ] && [ -r "${NEBIUS_API_KEY_FILE}" ]; then
    NEBIUS_API_KEY="$(cat "${NEBIUS_API_KEY_FILE}")"
    export NEBIUS_API_KEY
fi

exec "$@"
