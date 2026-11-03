# syntax=docker/dockerfile:1

# Test Hub container image.
#
# Two stages, because the build needs a compiler toolchain's worth of Python
# packaging machinery (`build`, `hatchling`) that has no business being in the
# thing you run. Stage 1 produces a wheel from this checkout; stage 2 installs
# that wheel and nothing else, so the runtime image contains exactly what
# `pip install test-hub` would give you — which means the image is also a
# continuous test that the wheel is installable.
#
#   docker build -t test-hub .
#   docker run --rm -p 127.0.0.1:8000:8000 -v test-hub-data:/data \
#       -e TEST_HUB_INSECURE_NO_AUTH=true test-hub
#
# ---------------------------------------------------------------------------
# READ THIS BEFORE YOU RUN IT
# ---------------------------------------------------------------------------
# The image binds 0.0.0.0 because a container that binds 127.0.0.1 is a
# container you cannot reach. Test Hub refuses a non-loopback bind that has no
# authentication (config.Settings.check_bind_safety), so a bare `docker run`
# exits 78 with a message telling you what to do. That is on purpose. Pick one:
#
#   1. Real deployments — set token hashes. Generate them on any machine with
#      Test Hub installed:
#
#          test-hub token create --label ci
#
#      then pass the *hash* line it prints (never the token itself). It looks
#      like `pbkdf2_sha256$<iterations>$<salt-hex>$<hash-hex>`, so quote it —
#      an unquoted `$` is your shell's, not ours:
#
#          -e TEST_HUB_API_TOKEN_HASHES='pbkdf2_sha256$120000$<salt>$<hash>'
#
#      Several hashes are comma-separated, so tokens can be rotated without
#      downtime. A JSON array is accepted too, if that is what your tooling
#      emits:
#
#          -e TEST_HUB_API_TOKEN_HASHES='["pbkdf2_sha256$120000$<salt>$<hash>"]'
#
#      No token is baked into this image, and none ever should be: an image layer
#      is public to anyone who can pull it and survives every `docker rmi` of the
#      tag.
#
#   2. Local poking about — accept an open port on purpose:
#
#          -e TEST_HUB_INSECURE_NO_AUTH=true
#
#      or `--insecure-no-auth` appended to the command. It warns on every start
#      and banners the dashboard. Only ever combine it with a published port
#      bound to loopback, as docker-compose.yml does.
# ---------------------------------------------------------------------------

# ------------------------------------------------------------------ stage 1
FROM python:3.12-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /src

RUN pip install --no-cache-dir "build>=1.2"

# The whole source tree in one COPY. Splitting it to cache dependency layers
# buys nothing here: there is no dependency install in this stage, and the
# wheel build reads pyproject.toml, src/, README.md and LICENSE anyway.
COPY . .

# --wheel only. The sdist target also pulls in tests/ and docs/, which
# .dockerignore keeps out of the build context on purpose.
RUN python -m build --wheel --outdir /dist .

# ------------------------------------------------------------------ stage 2
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="Test Hub" \
      org.opencontainers.image.description="Universal test observability — every team, framework and test run in one place." \
      org.opencontainers.image.source="https://github.com/prashanth-sams/test-hub" \
      org.opencontainers.image.documentation="https://prashanth-sams.github.io/test-hub/" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TEST_HUB_DATA_DIR=/data \
    TEST_HUB_HOST=0.0.0.0 \
    TEST_HUB_PORT=8000

COPY --from=build /dist /tmp/dist
RUN pip install --no-cache-dir /tmp/dist/*.whl && rm -rf /tmp/dist

# uid 10001 is a fixed, high, non-system id: fixed so a bind-mounted host
# directory can be chowned to a number that does not change between rebuilds,
# high so it cannot collide with a system account the base image adds later.
RUN useradd --system --uid 10001 --gid 0 --no-create-home --home-dir /data \
        --shell /usr/sbin/nologin testhub \
 && mkdir -p /data \
 && chown 10001:0 /data \
 && chmod 0770 /data

# Declared after the chown so a fresh named or anonymous volume is seeded with
# the right ownership. A volume created before this line would come up
# root-owned and the service would fail to write its database.
VOLUME ["/data"]

USER 10001

# Everything Test Hub writes goes to TEST_HUB_DATA_DIR, but a command given a
# relative path does not — `docker compose exec test-hub …` would otherwise
# resolve it against `/`, which the compose file mounts read-only. Landing in
# /data puts it on the volume, where the operator can actually retrieve it.
WORKDIR /data

EXPOSE 8000

# python's urllib rather than curl: the slim base has no curl, and adding one
# for a health check is 5 MB and an extra CVE feed for something the runtime
# already does. Non-200 raises, which is a non-zero exit, which is unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, sys, urllib.request; port = os.environ.get('TEST_HUB_PORT', '8000'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + port + '/healthz', timeout=4).status == 200 else 1)"

# Host and port are repeated here rather than left to TEST_HUB_HOST/PORT so
# that `docker run test-hub test-hub serve --port 9000` and `docker run test-hub
# test-hub ingest ...` both behave the way the CLI documents.
CMD ["test-hub", "serve", "--host", "0.0.0.0", "--port", "8000"]
