# SensorPush Prometheus Exporter

Lightweight, **battery-friendly** Prometheus exporter for SensorPush 2nd-generation
BLE sensors (HT.w, HTP.xw, TC.x). Optimized for long-running operation on a
Raspberry Pi. Unofficial; not affiliated with SensorPush.

## How it works (and why it's easy on the battery)

SensorPush sensors **broadcast** temperature, humidity and pressure in their BLE
advertisement packets. Reading those is completely passive — the sensor advertises
(~every 1.3 s by default) whether or not anyone is listening, so harvesting the data
costs the sensor **nothing**. This is the same approach the SensorPush app and Home
Assistant use.

Opening a GATT **connection**, by contrast, forces the sensor's radio into a
high-power state. This exporter therefore:

- Runs **one continuous scanner** and decodes advertisements in real time
  (temperature / humidity / pressure / RSSI) — **no per-poll connections**.
- Connects only **occasionally** (default every 6 h) to read **battery voltage** and
  the device id, which are the only values not broadcast. Set
  `BATTERY_READ_INTERVAL=0` to never connect at all.

The result is roughly **~0–4 connections per day per sensor** instead of ~1,440,
near-real-time data (~1.3 s vs. 60 s), and far fewer failures (advertisements can't
"fail to connect").

> Upgrading? v3 reduced every reading metric to a single `device_address` label.
> See [Changes in v3](#changes-in-v3).

## Quick Start

On a Raspberry Pi (arm64):

```bash
docker compose pull
docker compose up -d
```

That pulls `ghcr.io/rmarshall31/sensorpush-exporter:latest` (linux/arm64). The
package is private until you set it public in GitHub Packages after the first
CI push. Until then, or to pick up local edits, build instead:

```bash
docker compose up -d --build
```

Keep `--build` when you have changed `exporter.py`; Compose otherwise reuses
the existing image. The exporter auto-discovers every SensorPush sensor in
range.

### Running without Docker

Linux and macOS both work directly, which is handy for development:

```bash
pip install -r requirements.txt && ./exporter.py
```

On macOS, `BLE_ADAPTER` has no effect and `SCANNING_MODE=passive` is unsupported
by the OS; use the `active` default. Grant the terminal Bluetooth permission when
prompted.

## Metrics

Every per-device metric carries exactly one label, `device_address` (the sensor's
MAC). Human-readable metadata lives in `sensorpush_device_info`, which you join
on `device_address`. Keeping it out of the reading series means a name or id that
arrives late never forces a series to be deleted and recreated.

| Metric | Source | Notes |
|---|---|---|
| `sensorpush_device_info` | both | metadata: `device_name`, `model`, `device_id` |
| `sensorpush_temperature_celsius` | advertisement | near real-time |
| `sensorpush_humidity_percent` | advertisement | near real-time |
| `sensorpush_pressure_pascals` | advertisement | HTP.xw only |
| `sensorpush_rssi_dbm` | advertisement | signal strength |
| `sensorpush_last_update_timestamp` | advertisement | use to alert on stale data |
| `sensorpush_advertisements_received_total` | advertisement | counter; reliability signal |
| `sensorpush_battery_voltage_millivolts` | connection | refreshed every `BATTERY_READ_INTERVAL` |
| `sensorpush_battery_temperature_celsius` | connection | diagnostic; best-effort |
| `sensorpush_battery_last_read_timestamp` | connection | last successful battery read |
| `sensorpush_battery_read_errors_total` | connection | counter; failed battery connections |
| `sensorpush_exporter_info` | — | version / config |

`model` is one of `HT1`, `HT.w`, `HTP.xw`, `TC.x`. `device_id` is the id the
SensorPush app shows; it is empty until the first successful battery read, and
stays empty permanently if `BATTERY_READ_INTERVAL=0`.

**Attaching names to readings** — the join you want in most dashboards:

```promql
sensorpush_temperature_celsius
  * on(device_address) group_left(device_name, model)
  sensorpush_device_info
```

**Detecting a dead/out-of-range sensor:**

```promql
time() - sensorpush_last_update_timestamp > 300
```

This only fires while the series still exists. `STALE_AFTER` deletes a sensor's
metrics once it has been unseen that long, and the expression then has nothing
to evaluate, so the alert silently resolves rather than staying firing. Keep
`STALE_AFTER` well above your alert threshold plus its pending period, or set it
to `0` to never prune — at the cost of keeping metrics for any sensor that
leaves for good, including someone else's that drifted into range.

**Detecting a dying battery** (SensorPush documents the sensors as functional down
to roughly 2400 mV):

```promql
sensorpush_battery_voltage_millivolts < 2500
```

## Configuration

Set via environment variables (see `docker-compose.yml`):

| Variable | Default | Description |
|----------|---------|-------------|
| `SCANNING_MODE` | `active` | `active` (works everywhere) or `passive` (lowest power, Linux/BlueZ only) |
| `BATTERY_READ_INTERVAL` | `21600` | Seconds between battery reads. `0` = never connect (fully passive) |
| `STALE_AFTER` | `900` | Drop a device's metrics if unseen for this many seconds. `0` disables |
| `BATTERY_CONNECT_TIMEOUT` | `30` | Connection timeout for battery reads |
| `BATTERY_READ_RETRIES` | `2` | Connection attempts per battery read |
| `BLE_ADAPTER` | _(auto)_ | Force an adapter, e.g. `hci0` |
| `DEVICE_NAME_PREFIX` | `SensorPush` | Advertised-name prefix used to identify sensors |
| `EXPORTER_PORT` | `9108` | Metrics port |
| `LOG_LEVEL` | `INFO` | Logging level |
| `MAINTENANCE_INTERVAL` | `30` | Main-loop tick (battery-due check + stale prune) |

### Active vs. passive scanning

Both modes **never connect** to read the sensor data, so both are dramatically
better for battery life than the old approach. The difference:

- **`active`** (default): the adapter sends scan requests. Works on stock Raspberry
  Pi OS with no extra setup. Recommended.
- **`passive`**: the adapter only listens — marginally lighter and quieter on the
  air — but on Linux it requires **BlueZ ≥ 5.55 with the `--experimental` flag** on
  the **host** and Linux kernel ≥ 5.10. Enable it by adding `Experimental = true`
  under `[General]` in `/etc/bluetooth/main.conf` and restarting `bluetooth`. If a
  sensor isn't detected in passive mode, switch back to `active`.

## Maximizing battery life

- Leave `SCANNING_MODE=active` (or use `passive` if your host supports it).
- Increase `BATTERY_READ_INTERVAL` (e.g. `86400` for daily) or set it to `0` if you
  don't need the battery metric — connections are the only thing that drains the
  sensor.
- On the sensor side, the SensorPush app lets you raise the **advertising interval**
  (default 1285 ms, max ~20.5 s); longer intervals trade data freshness for battery.
- Do **not** lower **Tx power** to save battery. SensorPush recommends the maximum
  (+5 dBm), noting battery life still exceeds two years, because weaker signal
  causes exactly the two failure modes this exporter cares about: missed
  advertisements and failed battery connections.

## Prometheus configuration

```yaml
scrape_configs:
  - job_name: 'sensorpush'
    static_configs:
      - targets: ['localhost:9108']
    scrape_interval: 30s
```

## Troubleshooting

**No devices found:**

- Verify the sensor is in range (~30 ft) and has a battery installed.
- Confirm it's visible to the host: `bluetoothctl scan on` (look for `SensorPush ...`).
- Check the Bluetooth service: `systemctl status bluetooth`.

**Bluetooth adapter not accessible:**

- Ensure `/var/run/dbus` is mounted (see `docker-compose.yml`).
- The container must run as root. Host `bluetoothd` D-Bus policy is by uid;
  `privileged: true` is not a substitute.
- Check logs: `docker compose logs -f`.

**Battery voltage never appears:**

- Only one sensor is read per `MAINTENANCE_INTERVAL` tick, because scanning stops
  while a connection is open. With several sensors, expect the last one's first
  reading a few minutes after startup, then every `BATTERY_READ_INTERVAL`.
- Weak-signal sensors may fail to connect — temperature and humidity still stream
  from advertisements regardless. Watch `sensorpush_battery_read_errors_total`.

## Changes in v3

**Breaking:** reading metrics dropped the `device_name` and `device_id` labels and
now carry only `device_address`. Queries that grouped by name need the
`group_left` join shown under [Metrics](#metrics).

- New `sensorpush_device_info` exposes `device_name`, `model`, and `device_id`
  keyed by `device_address`. `model` was previously decoded and discarded, so
  sensor models are now visible for the first time.
- `device_id` is `""` rather than `unknown` before it resolves, and resolving it
  no longer deletes and recreates every series for that sensor.
- A sensor whose first advertisement lacked scan-response data used to keep a
  synthesized name forever; the real name is now adopted when it arrives.
- Removed `INITIAL_SETTLE`. A sensor's first battery read now happens on the
  first maintenance tick after it is discovered, and only one sensor is read per
  tick.
- Invalid `MAINTENANCE_INTERVAL`, `BATTERY_READ_RETRIES`, or `SCANNING_MODE` now
  exit at startup instead of being silently tolerated.

## API reference

Based on the [SensorPush Bluetooth API](https://www.sensorpush.com/bluetooth-api).
Advertisement decoding adapted (MIT) from
[Bluetooth-Devices/sensorpush-ble](https://github.com/Bluetooth-Devices/sensorpush-ble).

## License

MIT. See [LICENSE](LICENSE).
