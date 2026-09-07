FROM python:3.14-slim-bookworm

LABEL org.opencontainers.image.source="https://github.com/rmarshall31/sensorpush-exporter"
LABEL org.opencontainers.image.description="Prometheus exporter for SensorPush BLE sensors"
LABEL org.opencontainers.image.licenses="MIT"

# BLE uses the host bluetoothd over the mounted D-Bus socket, not in-image
# BlueZ. Run as root: system-bus policy is by uid, and privileged does not
# change that.

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY exporter.py .

# EXPOSE cannot read runtime env; override both together if you change the port.
ENV EXPORTER_PORT=9108
EXPOSE 9108

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request as r;r.urlopen('http://localhost:$EXPORTER_PORT')"

CMD ["python", "-u", "exporter.py"]
