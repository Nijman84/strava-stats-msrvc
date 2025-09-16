# docker/scheduler/Dockerfile
FROM alpine:3.20

# Tools we need: docker client, make, bash, tzdata, ca-certs, curl (to fetch supercronic)
RUN apk add --no-cache docker-cli make bash tzdata ca-certificates curl

# Install supercronic (static binary)
ENV SUPERCRONIC_VERSION=v0.2.29
RUN curl -fsSL -o /usr/local/bin/supercronic \
    https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64 \
 && chmod +x /usr/local/bin/supercronic

WORKDIR /repo
ENTRYPOINT ["/usr/local/bin/supercronic"]
CMD ["/repo/cron/strava.cron"]
