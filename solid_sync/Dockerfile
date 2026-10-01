# Supervisor 2026.04+ no longer supplies BUILD_FROM without build.yaml.
# https://developers.home-assistant.io/docs/apps/configuration/#app-dockerfile
ARG BUILD_FROM=ghcr.io/home-assistant/base:3.24-2026.08.0
FROM $BUILD_FROM

ARG BUILD_VERSION=0.6.2
ARG BUILD_ARCH
LABEL io.hass.version="$BUILD_VERSION" \
      io.hass.type="app" \
      io.hass.arch="$BUILD_ARCH"

WORKDIR /app

RUN apk add --no-cache python3 py3-aiohttp

COPY app /app
COPY run.sh /run.sh

RUN sed -i 's/\r$//' /run.sh && chmod a+x /run.sh

CMD ["/run.sh"]
