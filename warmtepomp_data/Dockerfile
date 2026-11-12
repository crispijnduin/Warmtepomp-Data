# Home Assistant app. De Supervisor bouwt dit lokaal; BUILD_FROM wordt per
# architectuur meegegeven en valt terug op de algemene basis-image.
ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM ${BUILD_FROM}

# De basis-image is Alpine; Python en pip komen daar niet standaard in.
RUN apk add --no-cache python3 py3-pip tzdata

WORKDIR /opt/warmtepomp
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --break-system-packages . \
    && rm -rf /root/.cache

# De s6-servicedefinitie die de capture start.
COPY rootfs /

LABEL \
    org.opencontainers.image.title="Home Assistant App: Warmtepomp Data" \
    org.opencontainers.image.description="Legt alle beschikbare data van de warmtepomp vast" \
    org.opencontainers.image.source="https://github.com/crispijnduin/Warmtepomp-Data" \
    org.opencontainers.image.licenses="MIT"
