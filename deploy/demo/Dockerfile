# The public demo API: `privatemesh serve --v2 --demo`.
#
# Build context is the repository root (`docker compose -f deploy/demo/compose.yaml build`), because
# the wheel needs `pyproject.toml`, `README.md` (its own `readme` field) and `src/`. No compiler is
# needed: every dependency in pyproject.toml (httpx, fastapi, uvicorn, pydantic) ships a manylinux
# wheel for this base image, so there is nothing here for build-essential to do.

FROM python:3.12-slim AS build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src/ src/
RUN pip install --no-cache-dir .

FROM python:3.12-slim

COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# Non-root: the process never needs to write anywhere but its own home (demo mode's per-session
# stores are all `:memory:` — see `demo_session.build_demo_world` — the one thing it persists is the
# whole-demo spend ledger, `PM_DEMO_LEDGER`, which compose.yaml mounts as a named volume under this
# same home directory).
RUN useradd --system --create-home --uid 10001 --shell /usr/sbin/nologin privatemesh
COPY --chmod=755 deploy/demo/entrypoint.sh /usr/local/bin/entrypoint.sh
# Pre-create the ledger directory, owned by the non-root user, BEFORE compose ever mounts the
# named `demo_ledger` volume there: Docker's local volume driver copies an existing directory's
# ownership into a freshly created volume on first mount, but only if that directory already
# exists in the image. Without this, the mount point is created fresh as root:root and
# `SpendLedger.__init__`'s `sqlite3.connect` fails with "unable to open database file" the moment
# a non-root process tries to create the db file inside it — caught by the local proof in
# docs/DEPLOY.md, not guessed.
RUN mkdir -p /home/privatemesh/.privatemesh && chown -R privatemesh:privatemesh /home/privatemesh
USER privatemesh
WORKDIR /home/privatemesh
ENV HOME=/home/privatemesh

EXPOSE 8765

# `/health` is mounted unconditionally (`api.py`, `@app.get("/health")`) and needs no credential.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8765/health', timeout=2).read()" || exit 1

# `--host 0.0.0.0` so the container's published port reaches uvicorn; the image bakes in no model
# key and no `--port` override beyond the compose file's own mapping. A missing/invalid
# `NEBIUS_API_KEY`, or any real provider credential in the environment, makes this refuse to start
# (`wiring.build_demo` -> `demo_session.refuse_if_providers_configured`) rather than serve and 500 —
# see `tests/test_demo_mode.py::test_serve_demo_refuses_without_a_model_key_and_says_which_one` and
# `::test_serve_demo_refuses_to_start_while_a_real_credential_is_present`.
#
# `entrypoint.sh` is the ONLY place the model key touches this image: it reads `NEBIUS_API_KEY_FILE`
# (a docker secret mount, see compose.yaml) into `NEBIUS_API_KEY` at container start, so the key is
# never an image layer, a build ARG, or a line in a committed file.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["privatemesh", "serve", "--v2", "--demo", "--host", "0.0.0.0", "--port", "8765"]
