# syntax=docker/dockerfile:1.7

ARG BUILDER_IMAGE="deepthink-registry.us-east-1.cr.aliyuncs.com/deepthink/base-openmontage:py3.11-node22-bookworm-build-20260817@sha256:29a3d14090c4ae0e440cb0618478b650b347ea8f682ae3a7ba9ddc3518f3532c"
ARG RUNTIME_IMAGE="deepthink-registry.us-east-1.cr.aliyuncs.com/deepthink/base-openmontage:py3.11-node22-bookworm-20260817@sha256:be2ee7b358e5768fc6c2b68b42d0c63b1a0c2756afcd9325832ea1b974b03b6c"

FROM ${BUILDER_IMAGE} AS dependencies

ARG INSTALL_PIPER_TTS="true"

ENV VIRTUAL_ENV="/opt/openmontage/venv" \
    PATH="/opt/openmontage/venv/bin:${PATH}"

WORKDIR /workspace

USER root
RUN mkdir -p "${VIRTUAL_ENV}" /home/node/.cache/pip /home/node/.npm \
    && chown -R 1000:1000 /opt/openmontage /home/node/.cache /home/node/.npm /workspace
USER 1000:1000

COPY --chown=1000:1000 requirements.txt ./requirements.txt

RUN --mount=type=cache,target=/home/node/.cache/pip,uid=1000,gid=1000 \
    python3 -m venv "${VIRTUAL_ENV}" \
    && python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install --requirement requirements.txt \
    && if [ "${INSTALL_PIPER_TTS}" = "true" ]; then python -m pip install piper-tts; fi

WORKDIR /workspace/remotion-composer

COPY --chown=1000:1000 remotion-composer/package.json remotion-composer/package-lock.json ./

RUN --mount=type=cache,target=/home/node/.npm,uid=1000,gid=1000 \
    npm ci \
    && npm audit --omit=dev --audit-level=high \
    && npx remotion browser ensure

FROM dependencies AS test

WORKDIR /workspace

COPY --chown=1000:1000 requirements-dev.txt ./requirements-dev.txt

RUN --mount=type=cache,target=/home/node/.cache/pip,uid=1000,gid=1000 \
    python -m pip install --requirement requirements-dev.txt

COPY --chown=1000:1000 . .
COPY --from=test-suite --chown=1000:1000 / ./tests
COPY --from=contracts --chown=1000:1000 /openmontage/v1 /contracts/openmontage/v1

CMD ["python", "-m", "pytest", "tests", "-q"]

FROM ${RUNTIME_IMAGE} AS runtime

ARG BUILD_DATE="unknown"
ARG VCS_REF="unknown"
ARG VERSION="unknown"

LABEL org.opencontainers.image.title="OpenMontage" \
      org.opencontainers.image.description="OpenMontage video production runtime with Backlot, Remotion, FFmpeg, and optional Piper TTS" \
      org.opencontainers.image.source="https://github.com/iranyxiao-lab/OpenMontage" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}"

ENV VIRTUAL_ENV="/opt/openmontage/venv" \
    PATH="/opt/openmontage/venv/bin:${PATH}" \
    BACKLOT_PORT="4750" \
    OPENMONTAGE_PROJECTS_DIR="/workspace/projects"

WORKDIR /workspace

COPY --chown=1000:1000 . .
COPY --from=dependencies --chown=1000:1000 /opt/openmontage/venv /opt/openmontage/venv
COPY --from=dependencies --chown=1000:1000 /workspace/remotion-composer/node_modules ./remotion-composer/node_modules

RUN mkdir -p "${OPENMONTAGE_PROJECTS_DIR}" \
    && python -c "import fastapi, openai, PIL, pydantic, redis, uvicorn" \
    && node --version \
    && ffmpeg -version >/dev/null \
    && test -f remotion-composer/node_modules/.remotion/chrome-headless-shell/VERSION

USER 1000:1000

EXPOSE 4750

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl --fail --silent --show-error "http://127.0.0.1:${BACKLOT_PORT}/api/health" >/dev/null || exit 1

CMD ["sh", "-c", "exec python -m uvicorn backlot.server:app --host 0.0.0.0 --port \"${BACKLOT_PORT}\""]
