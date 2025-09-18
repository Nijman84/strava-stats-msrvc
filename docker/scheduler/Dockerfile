# docker/scheduler/Dockerfile
# Small, reliable: Docker CLI (with compose) + supercronic + make + tzdata.

FROM docker:27.3.1-cli

# shells & tools
RUN apk add --no-cache bash make tzdata curl ca-certificates

# supercronic (tiny, robust cron for containers)
ARG SUPERCRONIC_VERSION=v0.2.33
ADD https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64 /usr/local/bin/supercronic
RUN chmod +x /usr/local/bin/supercronic

# Ensure Docker Compose v2 plugin is present in a standard plugin dir
# (see Docker docs: plugin search paths include /usr/local/lib/docker/cli-plugins) 
# Pin a known-good Compose release; adjust as you like.
ARG COMPOSE_VERSION=v2.29.7
RUN mkdir -p /usr/local/lib/docker/cli-plugins && \
    curl -fsSL -o /usr/local/lib/docker/cli-plugins/docker-compose \
      https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-x86_64 && \
    chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# Default TZ honoured via environment; your compose sets TZ=Europe/London
WORKDIR /repo

# supercronic makes cron lines observable and robust
# Entrypoint runs whatever cron file you pass as the CMD/command.
ENTRYPOINT ["/usr/local/bin/supercronic", "-json"]
CMD ["/etc/cron/strava.cron"]
