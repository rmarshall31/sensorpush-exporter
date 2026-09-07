FROM python:3.14-slim-bookworm

LABEL maintainer="SensorPush Exporter"
LABEL description="Prometheus exporter for SensorPush BLE sensors"

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    bluez \
    bluez-tools \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 -s /bin/bash exporter

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY exporter.py .

RUN chmod +x exporter.py

RUN chown -R exporter:exporter /app

USER exporter

# EXPOSE cannot read runtime env; override both together if you change the port.
ENV EXPORTER_PORT=9108
EXPOSE 9108

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request as r;r.urlopen('http://localhost:$EXPORTER_PORT')"

CMD ["python", "-u", "exporter.py"]
