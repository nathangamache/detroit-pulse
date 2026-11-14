# Pulse UI as static files for the godseye gateway's /pulse route.
# Builds frontend/ with Vite (base /pulse/, API at the relative /pulse/api), then the
# `pulse-frontend` one-shot service copies dist/ into the `pulse_frontend` volume, which the
# gateway mounts read-only as PULSE_STATIC_DIR (docs/godseye-integration.md). No server runs.
ARG NODE_IMAGE=node:24.22.3-bookworm-slim@sha256:dc26ae92c9fb15cf6b1d63e20f0944b2efe0f115b20e00be48fb23fceb061c54
ARG PY_IMAGE=python:3.12.15-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70

FROM ${NODE_IMAGE} AS build
ENV npm_config_fund=false npm_config_audit=false npm_config_update_notifier=false
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts
COPY frontend/ ./
RUN npx vite build

# copier: reuses the Pulse base image (already pulled), runs as an unprivileged user
FROM ${PY_IMAGE}
RUN useradd --uid 10010 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin pulse \
 && mkdir /out && chown 10010:10010 /out
COPY --from=build /ui/dist /dist
USER 10010:10010
CMD ["sh", "-c", "find /out -mindepth 1 -delete && cp -R /dist/. /out/ && chmod -R a+rX /out && echo \"pulse UI copied to /out ($(find /out -type f | wc -l) files)\""]
