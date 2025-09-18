# docker/scheduler/Dockerfile
FROM alpine:3.20

# Core tools + Docker CLI + Compose v2 plugin + uuidgen
RUN apk add --no-cache \
    bash curl tzdata ca-certificates make git \
    docker-cli docker-cli-compose util-linux

# Supercronic
ARG SUPERCRONIC_VERSION=v0.2.34
RUN set -eux; \
    ARCH="$(apk --print-arch)"; \
    case "$ARCH" in \
      x86_64)  SUPERCR_ARCH=amd64 ;; \
      aarch64) SUPERCR_ARCH=arm64 ;; \
      *) echo "Unsupported arch for supercronic: $ARCH" >&2; exit 1 ;; \
    esac; \
    SUPERCR_URL="https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${SUPERCR_ARCH}"; \
    echo "Fetching $SUPERCR_URL"; \
    curl -fsSL -o /usr/local/bin/supercronic "$SUPERCR_URL"; \
    chmod +x /usr/local/bin/supercronic

# Optional: docker-compose v1 shim to the v2 plugin
RUN ln -sf /usr/libexec/docker/cli-plugins/docker-compose /usr/local/bin/docker-compose

ENV TZ=Europe/London
WORKDIR /repo
COPY . /repo
CMD ["/bin/sh","-lc","exec /usr/local/bin/supercronic -passthrough-logs /repo/cron/strava.cron"]
