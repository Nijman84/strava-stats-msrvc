# docker/scheduler/Dockerfile
FROM alpine:3.20

# Core tools
RUN apk add --no-cache bash curl tzdata ca-certificates make git docker-cli

# Install Compose v2 plugin (arch-aware)
ARG COMPOSE_VERSION=2.29.2
RUN mkdir -p /usr/libexec/docker/cli-plugins \
 && ARCH="$(uname -m)" \
 && case "$ARCH" in \
      x86_64)   TARGET="linux-x86_64" ;; \
      aarch64)  TARGET="linux-aarch64" ;; \
      armv7l)   TARGET="linux-armv7" ;; \
      *) echo "Unsupported arch: $ARCH" >&2; exit 1 ;; \
    esac \
 && curl -sSL "https://github.com/docker/compose/releases/download/v${COMPOSE_VERSION}/docker-compose-${TARGET}" \
    -o /usr/libexec/docker/cli-plugins/docker-compose \
 && chmod +x /usr/libexec/docker/cli-plugins/docker-compose \
 ln -sf /usr/libexec/docker/cli-plugins/docker-compose /usr/local/bin/docker-compose

# Supercronic scheduler
ARG SUPERCRONIC_VERSION=v0.2.4
RUN curl -sSL -o /usr/local/bin/supercronic \
      "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64" \
 && chmod +x /usr/local/bin/supercronic

ENV TZ=Europe/London
WORKDIR /repo
COPY . /repo

# Ensure we can talk to the host Docker daemon (socket must be mounted in docker-compose.yml)
CMD ["supercronic", "-passthrough-logs", "/repo/cron/strava.cron"]
