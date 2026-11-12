# Container voor de capture-service.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Amsterdam

WORKDIR /app

# Eerst alleen de metadata, zodat de dependency-laag gecached blijft.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# De database hoort in een volume, niet in de image.
VOLUME ["/data"]
ENV WARMTEPOMP_DB_PATH=/data/warmtepomp.sqlite \
    WARMTEPOMP_EXPORT_DIR=/data/export

# Zonder database is er niets vastgelegd; dat is het signaal dat het misgaat.
HEALTHCHECK --interval=5m --timeout=20s --start-period=2m \
    CMD warmtepomp status || exit 1

ENTRYPOINT ["warmtepomp"]
CMD ["run"]
