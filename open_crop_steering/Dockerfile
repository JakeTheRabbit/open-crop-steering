# syntax=docker/dockerfile:1
# =============================================================================
# Open Crop Steering — multi-stage add-on / standalone image.
#
# The SAME image runs both ways (plan locked decision #1): as an HA add-on
# (s6 + bundled Postgres, mode auto-detected via SUPERVISOR_TOKEN) and via
# docker-compose.standalone.yml. Three build stages keep the final image
# lean — no Node toolchain, no compilers, no build caches survive into it.
#
# IMPORTANT — build context: this Dockerfile references ../frontend and
# ../backend, so it must be built with the REPO ROOT as the build context:
#
#     docker build -f open_crop_steering/Dockerfile -t open-crop-steering .
#
# The HA add-on builder is pointed at the repo root for the same reason
# (the CI workflow / add-on repo metadata sets the build context). The
# stages only READ frontend/ — they never write it (another agent owns it).
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1 — frontend: build the Next.js static export.
#
# `npm ci` installs exactly the lockfile; `npm run build` runs `next build`
# which, with `output: 'export'` in next.config.js, emits a fully static
# bundle into frontend/out/. Only that out/ directory is copied forward.
# -----------------------------------------------------------------------------
FROM node:22-alpine AS frontend
WORKDIR /build/frontend
# Copy manifests first so the npm-ci layer caches across source-only edits.
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
# Now the rest of the frontend source + build.
COPY frontend/ ./
RUN npm run build
# Result: /build/frontend/out — the static export.

# -----------------------------------------------------------------------------
# Stage 2 — wheels: build Python wheels for the backend + its deps.
#
# Building wheels in a throwaway stage means the final image needs no C
# compiler / build headers — only the prebuilt wheels are carried forward.
# -----------------------------------------------------------------------------
FROM python:3.12-alpine3.21 AS wheels
WORKDIR /build
# Build deps for the wheels that need compilation (asyncpg, cryptography,
# bcrypt). These never reach the final image.
RUN apk add --no-cache build-base libffi-dev openssl-dev postgresql-dev
COPY pyproject.toml ./
COPY backend/ ./backend/
# Build a wheel for the backend package + wheels for every runtime dep
# into /wheels. `--no-deps` on the project wheel, then a full resolve.
RUN python -m pip install --upgrade pip wheel build \
 && python -m pip wheel --wheel-dir=/wheels .

# -----------------------------------------------------------------------------
# Stage 3 — final: HA base image + Postgres 16 + s6 services.
#
# BUILD_FROM is substituted by the HA Supervisor from build.yaml's
# build_from map (the HA amd64 python base — s6-overlay v3 + bashio
# already on board). A plain default is given so a bare `docker build`
# (standalone CI) still works.
# -----------------------------------------------------------------------------
ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.21
FROM ${BUILD_FROM}

# Bundled PostgreSQL 16 server (plan stack: "bundled Postgres 16"), the
# `pg_dump` client for cold backups, plus tzdata. The HA base is Alpine,
# so postgresql16 comes from apk. su-exec drops privileges to the
# postgres user in the s6 run script.
RUN apk add --no-cache \
      postgresql16 \
      postgresql16-client \
      postgresql16-contrib \
      su-exec \
      tzdata

# Install the backend + all runtime deps from the prebuilt wheels. No
# network, no compiler — just the wheels from stage 2.
COPY --from=wheels /wheels /tmp/wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/tmp/wheels \
      open-crop-steering-backend \
 && rm -rf /tmp/wheels

# The static Next.js export — served by the FastAPI app at / (main.py
# reads OCS_FRONTEND_DIR). Baked in so the add-on is self-contained.
COPY --from=frontend /build/frontend/out /opt/frontend
ENV OCS_FRONTEND_DIR=/opt/frontend

# s6 service definitions + the cont-init.d startup script. rootfs/ mirrors
# the container filesystem: rootfs/etc/services.d/<name>/{run,finish} and
# rootfs/etc/cont-init.d/*. chmod +x so s6 can exec them.
COPY open_crop_steering/rootfs /
RUN chmod a+x \
      /etc/cont-init.d/*.sh \
      /etc/services.d/*/run \
      /etc/services.d/*/finish

# AppArmor profile (least-privilege) — referenced by config.yaml; HA
# applies it when the host supports AppArmor.
COPY open_crop_steering/apparmor.txt /apparmor.txt

# Ingress port the api service binds.
EXPOSE 8099

# No CMD / ENTRYPOINT — the HA base image's s6-overlay init is the
# entrypoint; it runs cont-init.d then supervises every services.d entry.
