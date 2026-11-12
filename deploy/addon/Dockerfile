ARG BUILD_FROM
FROM $BUILD_FROM

# De basis-images van Home Assistant zijn Alpine-gebaseerd.
RUN apk add --no-cache python3 py3-pip

WORKDIR /opt/warmtepomp
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip3 install --no-cache-dir --break-system-packages .

COPY deploy/addon/run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
