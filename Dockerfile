# Weft, served over streamable HTTP.
#
#   docker build -t weft .
#   docker run --rm --init -p 8000:8000 -e WEFT_AUTH_TOKEN -v weft-data:/data weft
#
# The image runs as an unprivileged user. It refuses to start without WEFT_AUTH_TOKEN, because it
# binds to every interface. The README has the full run command and the connector steps.
#
# Stages: base (dependencies, source, browser), test (adds the test suite), runtime (the default).

FROM python:3.12-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    PATH=/app/.venv/bin:$PATH

# The same uv that wrote uv.lock, so the install below is the one that was tested.
RUN pip install uv==0.11.23

WORKDIR /app

# Dependencies first: editing the source does not reinstall them. --frozen installs exactly what
# uv.lock says and fails if pyproject.toml has moved on without it.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-editable

COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-editable

# The headless shell Playwright 1.65 drives, with the system libraries it needs. This is the one
# step that reads the network at build time besides the dependencies above. Playwright's bundled
# Node links libatomic, which the slim image lacks, so the driver exits 127 before --with-deps can
# install anything. libatomic1 goes in first, and the apt lists are removed in the same layer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && playwright install --with-deps chromium-headless-shell \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 weft \
    && useradd --system --uid 10001 --gid weft --create-home --home-dir /home/weft \
        --shell /usr/sbin/nologin weft \
    && mkdir /data \
    && chown weft:weft /data


FROM base AS test
# docker build --target test -t weft-test . && docker run --rm weft-test
RUN uv sync --frozen --extra dev --no-editable
COPY tests ./tests
USER weft
CMD ["pytest", "-p", "no:cacheprovider"]


FROM base AS runtime

ENV WEFT_DATA_DIR=/data \
    WEFT_TRANSPORT=http \
    WEFT_HTTP_HOST=0.0.0.0

USER weft
VOLUME ["/data"]
EXPOSE 8000

# 2xx from /healthz or a non-zero exit. The proxy handler is empty so that a proxy variable in
# the container's environment cannot send the probe anywhere else.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:' + os.environ.get('WEFT_HTTP_PORT', '8000') + '/healthz', timeout=3)"]

ENTRYPOINT ["weft"]
