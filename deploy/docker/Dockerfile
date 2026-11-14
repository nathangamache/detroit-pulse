# Detroit Pulse runtime image: Python 3.12 + the locked runtime dependencies from uv.lock.
# One image runs every Pulse service in deploy/docker-compose.yml:
#   pulse-api         alembic upgrade head, then `python -m api --allow-non-loopback` on :7710
#   pulse-transcribe  run_pipeline.py --stages ingest,transcribe   (faster-whisper, CPU int8)
#   pulse-extract     run_pipeline.py --stages extract,resolver     (Claude batch / rules, local geocoder)
#   pulse-debug       deploy/docker/tcp_forward.py                   (debug profile only)
# Classic-builder compatible (no BuildKit-only syntax); the base is pinned by digest.
# No secrets, personal config (config/*.local.*), data or tests enter the image (.dockerignore);
# config and the geocoder index are mounted read-only at run time.
ARG PY_IMAGE=python:3.12.15-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70

FROM ${PY_IMAGE} AS build
ARG UV_VERSION=0.11.12
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PROJECT_ENVIRONMENT=/opt/venv
RUN pip install "uv==${UV_VERSION}"
WORKDIR /src
COPY pyproject.toml uv.lock ./
# runtime dependencies only: no dev group, no geocoder-build group (osmium/pyshp)
RUN uv sync --frozen --no-default-groups --no-install-project \
 && rm -rf /root/.cache

FROM ${PY_IMAGE}
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp XDG_CACHE_HOME=/tmp/.cache HF_HOME=/models/hf
# pulse (10010) runs everything; pulse-sdr (10030) is the group shared with the SDR containers so
# Pulse can read and (optionally) delete their recordings. The directories below are the mount
# points of named volumes: an empty volume copies their owner and mode on first use.
RUN groupadd --gid 10010 pulse \
 && groupadd --gid 10030 pulse-sdr \
 && useradd --uid 10010 --gid 10010 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin pulse \
 && mkdir -p /data/sdr /data/traintap /models /geocoder/index /fixtures \
 && chown 10010:10030 /data/sdr /data/traintap && chmod 2775 /data/sdr /data/traintap \
 && chown 10010:10010 /models \
 && rm -rf /usr/local/lib/python3.12/site-packages/pip /usr/local/bin/pip /usr/local/bin/pip3*
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY alembic.ini run_pipeline.py ./
COPY core ./core
COPY db ./db
COPY api ./api
COPY ingest ./ingest
COPY transcribe ./transcribe
COPY extract ./extract
COPY rail ./rail
COPY geocode ./geocode
COPY correlation ./correlation
COPY config ./config
COPY deploy/docker/tcp_forward.py ./deploy/docker/tcp_forward.py
RUN python -m compileall -q /app && chmod -R a+rX /app
USER 10010:10010
EXPOSE 7710
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7710/v1/health', timeout=4).status == 200 else 1)"]
CMD ["python", "-m", "api", "--allow-non-loopback"]
