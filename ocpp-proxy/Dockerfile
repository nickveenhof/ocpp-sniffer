ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:latest
FROM ${BUILD_FROM}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN \
    apk add --no-cache \
        python3 \
        py3-pip \
    && pip3 install --break-system-packages \
        "aiohttp>=3.9.0" \
        "websockets>=12.0" \
        "PyYAML>=6.0.0"

WORKDIR /app
COPY src/ ./src/

COPY run.sh /
RUN chmod a+x /run.sh

EXPOSE 9000

LABEL \
    io.hass.name="OCPP Sniffer" \
    io.hass.description="Transparent OCPP proxy for RFID capture and evcc integration" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="addon" \
    io.hass.version="${BUILD_VERSION}" \
    org.opencontainers.image.title="OCPP Sniffer" \
    org.opencontainers.image.description="Transparent OCPP proxy for RFID capture and evcc integration" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.source="https://github.com/nickveenhof/ocpp-sniffer"

CMD [ "/run.sh" ]
