#!/usr/bin/env python3
"""Prometheus exporter for SensorPush 2nd-generation BLE sensors.

Readings are decoded from BLE advertisements, which costs the sensor nothing.
Battery voltage and device id are not broadcast, so they need an occasional
GATT connection. See README.md for the rationale and configuration.
"""

import asyncio
import logging
import os
import signal
import sys
import time

from prometheus_client import Counter, Gauge, Info, start_http_server

from bleak import BleakClient, BleakScanner


def _env_int(name, default):
    raw = os.getenv(name, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        sys.exit(f"{name} must be an integer, got {raw!r}")


SCANNING_MODE = os.getenv("SCANNING_MODE", "active").lower()
BLE_ADAPTER = os.getenv("BLE_ADAPTER", "").strip()  # e.g. "hci0"; empty = default
DEVICE_NAME_PREFIX = os.getenv("DEVICE_NAME_PREFIX", "SensorPush").strip()

# Battery voltage is not broadcast and only updates on connect, so read it
# rarely. BATTERY_READ_INTERVAL=0 disables connections entirely.
BATTERY_READ_INTERVAL = _env_int("BATTERY_READ_INTERVAL", "21600")
BATTERY_CONNECT_TIMEOUT = _env_int("BATTERY_CONNECT_TIMEOUT", "30")
BATTERY_READ_RETRIES = _env_int("BATTERY_READ_RETRIES", "2")

STALE_AFTER = _env_int("STALE_AFTER", "900")  # unseen this long; 0 disables
MAINTENANCE_INTERVAL = _env_int("MAINTENANCE_INTERVAL", "30")

EXPORTER_PORT = _env_int("EXPORTER_PORT", "9108")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# BlueZ stop() should return promptly. Hanging here is the wedge we refuse.
_SCANNER_STOP_TIMEOUT = 10

# Rejected up front: a zero tick spins the event loop, zero retries reports
# failures without ever connecting, an empty name prefix matches every
# advertiser, and a misspelled mode would silently scan actively.
if MAINTENANCE_INTERVAL < 1:
    sys.exit(f"MAINTENANCE_INTERVAL must be >= 1, got {MAINTENANCE_INTERVAL}")
if BATTERY_READ_RETRIES < 1:
    sys.exit(f"BATTERY_READ_RETRIES must be >= 1, got {BATTERY_READ_RETRIES}")
if BATTERY_CONNECT_TIMEOUT < 1:
    sys.exit(
        f"BATTERY_CONNECT_TIMEOUT must be >= 1, got {BATTERY_CONNECT_TIMEOUT}"
    )
if BATTERY_READ_INTERVAL < 0:
    sys.exit(
        f"BATTERY_READ_INTERVAL must be >= 0, got {BATTERY_READ_INTERVAL}"
    )
if STALE_AFTER < 0:
    sys.exit(f"STALE_AFTER must be >= 0, got {STALE_AFTER}")
if not 1 <= EXPORTER_PORT <= 65535:
    sys.exit(f"EXPORTER_PORT must be 1-65535, got {EXPORTER_PORT}")
if not DEVICE_NAME_PREFIX:
    sys.exit("DEVICE_NAME_PREFIX must be non-empty")
if SCANNING_MODE not in ("active", "passive"):
    sys.exit(f"SCANNING_MODE must be 'active' or 'passive', got {SCANNING_MODE!r}")
if LOG_LEVEL not in {
    "CRITICAL",
    "FATAL",
    "ERROR",
    "WARNING",
    "WARN",
    "INFO",
    "DEBUG",
}:
    sys.exit(
        f"LOG_LEVEL must be a standard logging level, got {LOG_LEVEL!r}"
    )

VERSION = "3.0.1"

# GATT characteristics: https://www.sensorpush.com/bluetooth-api
DEVICE_ID_CHAR = "ef090001-11d6-42ba-93b8-9dd7ec090aa9"
BATTERY_VOLTAGE_CHAR = "ef090007-11d6-42ba-93b8-9dd7ec090aa9"

# Advertisement decoding is adapted from Bluetooth-Devices/sensorpush-ble
# (originally Ernst79/bleparser); copyrights are in LICENSE. Kept here to stay
# dependency-free and to emit SI units matching our metric names.
SENSORPUSH_SERVICE_UUID_V2 = "ef090000-11d6-42ba-93b8-9dd7ec090ab0"
SENSORPUSH_SERVICE_UUID_HT1 = "ef090000-11d6-42ba-93b8-9dd7ec090aa9"

SENSORPUSH_DEVICE_TYPES = {1: "HT1", 64: "HTP.xw", 65: "HT.w", 66: "TC.x"}

# Per device-type: list of [min, max, step] used to unpack the bit-packed payload.
# Order matches SENSORPUSH_VALUE_KEYS below. Note the pressure block is in Pascals.
SENSORPUSH_PACK_PARAMS = {
    64: [[-40.0, 140.0, 0.0025], [0.0, 100.0, 0.0025], [30000.0, 125000.0, 1.0]],
    65: [[-40.0, 125.0, 0.0025], [0.0, 100.0, 0.0025]],
    66: [[-200.0, 1800.0, 0.0625]],
}
SENSORPUSH_VALUE_KEYS = {
    64: ["temperature_c", "humidity_pct", "pressure_pa"],
    65: ["temperature_c", "humidity_pct"],
    66: ["temperature_c"],
}
# Payload bytes each type is known to send, per upstream. A short payload would
# otherwise decode to the minimum of every range, i.e. a believable -40 C.
SENSORPUSH_PAYLOAD_LEN = {64: 5, 65: 3, 66: 2}

# AD flags values used as a broad allow-list for Linux passive scanning.
PASSIVE_FLAGS = (b"\x06", b"\x1a", b"\x02", b"\x04", b"\x05")


def _bluez_kwargs():
    return {"adapter": BLE_ADAPTER} if BLE_ADAPTER else {}


def _short_address(address: str) -> str:
    """Last two octets of a MAC, e.g. 'AA:BB:CC:DD:EE:FF' -> 'EEFF'."""
    return "".join(address.replace("-", ":").split(":")[-2:]).upper() or address


def _relative_humidity_from_raw(num: int) -> float:
    value = (-6.0) + (125.0 * (num / (2.0 ** 12.0)))
    return round(min(max(value, 0.0), 100.0), 2)


def _temperature_c_from_raw(num: int) -> float:
    return round((-46.85) + (175.72 * (num / (2.0 ** 14.0))), 2)


def _find_latest_data(manufacturer_data: dict, is_ht1: bool):
    """Pick the manufacturer-data 'page' that carries current values (page 0)."""
    for company_id in reversed(manufacturer_data):
        cid = int(company_id)
        if not is_ht1 and (cid & 0x03) != 0:  # page_id 0 == current values
            continue
        return cid.to_bytes(2, byteorder="little") + manufacturer_data[company_id]
    return None


def _decode_ht1(data: bytes) -> dict:
    if len(data) < 4:
        return {}
    if ((data[3] & 124) >> 2) != 1:
        return {}
    humidity = _relative_humidity_from_raw((data[0] & 255) + ((data[1] & 15) << 8))
    temperature = _temperature_c_from_raw(
        ((data[1] & 255) >> 4) + ((data[2] & 255) << 4) + ((data[3] & 3) << 12)
    )
    return {"temperature_c": temperature, "humidity_pct": humidity}


def _decode_values(data: bytes, device_type_id: int) -> dict:
    if device_type_id == 1:
        return _decode_ht1(data)

    pack_params = SENSORPUSH_PACK_PARAMS.get(device_type_id)
    keys = SENSORPUSH_VALUE_KEYS.get(device_type_id)
    if not pack_params or not keys:
        return {}
    if len(data) < 2 + SENSORPUSH_PAYLOAD_LEN[device_type_id]:  # 2 = company id
        return {}

    packed = 0
    for i in range(1, len(data)):
        packed += data[i] << (8 * (i - 1))

    out = {}
    mod = 1
    div = 1
    for i, (min_v, max_v, step) in enumerate(pack_params):
        span = int((max_v - min_v) / step + step / 2.0) + 1
        mod *= span
        count = int((packed % mod) / div)
        out[keys[i]] = round(count * step + min_v, 2)
        div *= span
    return out


def _is_ht1(name: str, service_uuids) -> bool:
    return (
        name == "s"
        and service_uuids
        and SENSORPUSH_SERVICE_UUID_HT1 in service_uuids
    )


def is_sensorpush(name: str, service_uuids) -> bool:
    """Conservatively decide whether an advertisement is from a SensorPush sensor."""
    if name and name.startswith(DEVICE_NAME_PREFIX):
        return True
    if not service_uuids:
        return False
    if SENSORPUSH_SERVICE_UUID_V2 in service_uuids:
        return True
    return _is_ht1(name, service_uuids)


def parse_advertisement(name: str, service_uuids, manufacturer_data: dict):
    """
    Decode a SensorPush advertisement.

    Returns (model, readings) or None. readings keys are a subset of
    {"temperature_c", "humidity_pct", "pressure_pa"}.
    """
    if not manufacturer_data:
        return None

    is_ht1 = _is_ht1(name, service_uuids)
    data = _find_latest_data(manufacturer_data, is_ht1)
    if data is None:
        return None

    device_type_id = 1 if is_ht1 else 64 + (data[0] >> 2)
    model = SENSORPUSH_DEVICE_TYPES.get(device_type_id)
    if model is None:
        return None  # unknown device_type_id

    readings = _decode_values(data, device_type_id)
    if not readings:
        return None
    return model, readings


logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("sensorpush-exporter")

# Readings are keyed by MAC only. Everything that can change or arrive late
# (name, model, device id) lives in sensorpush_device_info, so no series ever
# has to be deleted and recreated just because metadata was resolved.
_LABELS = ["device_address"]

temperature_gauge = Gauge(
    "sensorpush_temperature_celsius", "Temperature reading in Celsius", _LABELS
)
humidity_gauge = Gauge(
    "sensorpush_humidity_percent", "Relative humidity percentage", _LABELS
)
pressure_gauge = Gauge(
    "sensorpush_pressure_pascals", "Barometric pressure in Pascals (HTP.xw only)", _LABELS
)
battery_voltage_gauge = Gauge(
    "sensorpush_battery_voltage_millivolts",
    "Battery voltage in millivolts (read via occasional GATT connection)",
    _LABELS,
)
battery_temperature_gauge = Gauge(
    "sensorpush_battery_temperature_celsius",
    "Sensor temperature at last battery reading, in Celsius (diagnostic)",
    _LABELS,
)
rssi_gauge = Gauge("sensorpush_rssi_dbm", "Signal strength in dBm", _LABELS)
last_update_timestamp = Gauge(
    "sensorpush_last_update_timestamp",
    "Unix timestamp of the last advertisement decoded (use to detect stale data)",
    _LABELS,
)
battery_last_read_timestamp = Gauge(
    "sensorpush_battery_last_read_timestamp",
    "Unix timestamp of the last successful battery read",
    _LABELS,
)
advertisements_received_total = Counter(
    "sensorpush_advertisements_received_total",
    "Total SensorPush advertisements successfully decoded",
    _LABELS,
)
battery_read_errors_total = Counter(
    "sensorpush_battery_read_errors_total",
    "Total battery-read GATT connections that failed after retries",
    _LABELS,
)
device_info = Info(
    "sensorpush_device",
    "Device metadata; join readings on device_address. device_id is empty "
    "until the first successful battery read resolves it",
    _LABELS,
)
exporter_info = Info("sensorpush_exporter", "Information about the exporter")
exporter_info.info(
    {
        "version": VERSION,
        "scanning_mode": SCANNING_MODE,
        "battery_read_interval": str(BATTERY_READ_INTERVAL),
        "stale_after": str(STALE_AFTER),
    }
)

_READING_GAUGES = {
    "temperature_c": temperature_gauge,
    "humidity_pct": humidity_gauge,
    "pressure_pa": pressure_gauge,
}

# Every per-device metric, so a departing device can be pruned from all at once.
_DEVICE_METRICS = (
    *_READING_GAUGES.values(),
    battery_voltage_gauge,
    battery_temperature_gauge,
    rssi_gauge,
    last_update_timestamp,
    battery_last_read_timestamp,
    advertisements_received_total,
    battery_read_errors_total,
    device_info,
)


def _remove_device(address: str):
    for metric in _DEVICE_METRICS:
        try:
            metric.remove(address)
        except KeyError:  # not every metric exists for every device
            pass


class SensorPushExporter:
    """Continuous BLE advertisement scanner + occasional battery reader."""

    def __init__(self):
        self.devices = {}
        self.scanner = None
        self._stop_event = asyncio.Event()
        self._logged_empty = False

    def _process_advertisement(self, device, adv_data):
        name = adv_data.local_name or device.name
        service_uuids = adv_data.service_uuids
        if not is_sensorpush(name, service_uuids):
            return

        try:
            parsed = parse_advertisement(
                name, service_uuids, adv_data.manufacturer_data
            )
        except (TypeError, ValueError, IndexError, OverflowError) as exc:
            logger.debug(
                "Failed to decode advertisement from %s: %s",
                device.address,
                exc,
            )
            return
        if parsed is None:
            return
        model, readings = parsed

        address = device.address
        dev = self.devices.get(address)
        if dev is None:
            series = {
                "ads": advertisements_received_total.labels(address),
                "rssi": rssi_gauge.labels(address),
                "seen": last_update_timestamp.labels(address),
            }
            for key in readings:
                series[key] = _READING_GAUGES[key].labels(address)
            dev = {
                "name": name or f"{model} {_short_address(address)}",
                "address": address,
                "model": model,
                "device_id": "",
                "last_seen": 0.0,
                "last_battery_read": 0.0,
                "series": series,
            }
            self.devices[address] = dev
            logger.info("Discovered %s (%s) model=%s", dev["name"], address, model)
            self._publish_info(dev)
        else:
            info_changed = False
            if name and name != dev["name"]:
                # First ads can lack scan-response data; the real name arrives later.
                dev["name"] = name
                info_changed = True
            if model != dev["model"]:
                dev["model"] = model
                info_changed = True
            if info_changed:
                self._publish_info(dev)
        dev["last_seen"] = time.time()

        series = dev["series"]
        series["ads"].inc()
        for key, value in readings.items():
            child = series.get(key)
            if child is None:
                child = _READING_GAUGES[key].labels(address)
                series[key] = child
            child.set(value)
        if adv_data.rssi is not None:
            series["rssi"].set(adv_data.rssi)
        series["seen"].set(dev["last_seen"])

    def _publish_info(self, dev):
        device_info.labels(dev["address"]).info(
            {
                "device_name": dev["name"],
                "model": dev["model"],
                "device_id": dev["device_id"],
            }
        )

    async def read_battery(self, dev):
        address = dev["address"]
        name = dev["name"]
        client_kwargs = {"timeout": BATTERY_CONNECT_TIMEOUT}
        if BLE_ADAPTER:
            client_kwargs["bluez"] = _bluez_kwargs()

        last_error = None
        for attempt in range(1, BATTERY_READ_RETRIES + 1):
            try:
                async with BleakClient(address, **client_kwargs) as client:
                    raw = await client.read_gatt_char(BATTERY_VOLTAGE_CHAR)
                    try:
                        id_raw = await client.read_gatt_char(DEVICE_ID_CHAR)
                        new_id = str(int.from_bytes(id_raw, byteorder="little", signed=False))
                        if new_id != dev["device_id"]:
                            dev["device_id"] = new_id
                            self._publish_info(dev)
                            logger.info("[%s] resolved device_id: %s", name, new_id)
                    except Exception as exc:
                        logger.debug("[%s] device id read failed: %s", name, exc)

                if len(raw) >= 2:
                    voltage_mv = int.from_bytes(raw[0:2], byteorder="little", signed=False)
                    battery_voltage_gauge.labels(address).set(voltage_mv)
                    logger.info("[%s] battery: %d mV", name, voltage_mv)
                if len(raw) >= 4:
                    sensor_temp_c = int.from_bytes(raw[2:4], byteorder="little", signed=True)
                    # Junk reads happen, but -1 is plausible, so only bound
                    # magnitude. Upper bound matches the HTP.xw sensor range.
                    if -60 <= sensor_temp_c <= 140:
                        battery_temperature_gauge.labels(address).set(sensor_temp_c)
                dev["last_battery_read"] = time.time()
                battery_last_read_timestamp.labels(address).set(dev["last_battery_read"])
                return
            except Exception as exc:
                last_error = exc
                if attempt < BATTERY_READ_RETRIES:
                    if self._stop_event.is_set():
                        return
                    await asyncio.sleep(2)

        logger.warning("[%s] battery read failed: %s", name, last_error)
        battery_read_errors_total.labels(address).inc()
        # Back off until the next interval so we don't hammer an unreachable sensor.
        dev["last_battery_read"] = time.time()

    async def battery_cycle(self):
        """Read the battery of the single most overdue device, scanner paused."""
        if BATTERY_READ_INTERVAL <= 0:
            return
        now = time.time()
        due = [
            dev
            for dev in self.devices.values()
            if now - dev["last_battery_read"] >= BATTERY_READ_INTERVAL
        ]
        if not due:
            return

        # One device per cycle. Scanning is impossible while connected, so a
        # whole batch of unreachable sensors would blank every reading for
        # minutes. Reading one at a time also keeps sensors from staying in
        # lockstep, since each cycle re-dates exactly one of them.
        dev = min(due, key=lambda d: d["last_battery_read"])
        logger.info("Reading battery for %s (%d due)", dev["name"], len(due))
        await self._stop_scanner()
        try:
            if not self._stop_event.is_set():
                await self.read_battery(dev)
        finally:
            if not self._stop_event.is_set():
                await self._start_scanner()

    def _prune_stale(self, now):
        if STALE_AFTER <= 0:
            return
        for address in list(self.devices):
            dev = self.devices[address]
            if (now - dev["last_seen"]) > STALE_AFTER:
                _remove_device(address)
                del self.devices[address]
                logger.info(
                    "Pruned stale device %s (%s); last seen %ds ago",
                    dev["name"],
                    address,
                    int(now - dev["last_seen"]),
                )

    def _build_scanner(self):
        bluez = _bluez_kwargs()

        if SCANNING_MODE == "passive":
            # Imported lazily: only the passive path needs these, and bleak has
            # relocated them before.
            from bleak.args.bluez import OrPattern
            from bleak.assigned_numbers import AdvertisementDataType

            bluez["or_patterns"] = [
                OrPattern(0, AdvertisementDataType.FLAGS, flags) for flags in PASSIVE_FLAGS
            ]

        return BleakScanner(
            detection_callback=self._process_advertisement,
            scanning_mode=SCANNING_MODE,
            bluez=bluez,
        )

    async def _start_scanner(self):
        self.scanner = self._build_scanner()
        await self.scanner.start()

    async def _stop_scanner(self):
        scanner = self.scanner
        if scanner is None:
            return
        self.scanner = None
        try:
            await asyncio.wait_for(scanner.stop(), _SCANNER_STOP_TIMEOUT)
        except asyncio.TimeoutError:
            logger.error(
                "scanner.stop() timed out after %ds; exiting",
                _SCANNER_STOP_TIMEOUT,
            )
            sys.exit(1)
        except Exception as exc:
            logger.warning("Error stopping scanner: %s", exc)

    async def _stop_requested(self, timeout) -> bool:
        """Wait up to `timeout` seconds; True if a shutdown was requested."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def run(self):
        logger.info("SensorPush Prometheus Exporter v%s starting...", VERSION)
        logger.info("Scanning mode: %s", SCANNING_MODE)
        logger.info(
            "Battery reads: %s",
            "disabled" if BATTERY_READ_INTERVAL <= 0 else f"every {BATTERY_READ_INTERVAL}s",
        )
        if BLE_ADAPTER and sys.platform != "linux":
            logger.warning(
                "BLE_ADAPTER=%s is BlueZ-only and is ignored on %s",
                BLE_ADAPTER,
                sys.platform,
            )

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop)

        await self._start_scanner()
        logger.info("Listening for SensorPush advertisements (no per-poll connections)")

        while not await self._stop_requested(MAINTENANCE_INTERVAL):
            await self.battery_cycle()
            self._prune_stale(time.time())

            if not self.devices:
                if not self._logged_empty:
                    logger.warning(
                        "No SensorPush devices seen yet; still listening..."
                    )
                    self._logged_empty = True
            else:
                self._logged_empty = False

        await self._stop_scanner()
        logger.info("Scanner stopped")

    def stop(self):
        logger.info("Stopping exporter...")
        self._stop_event.set()


def main():
    logger.info("Starting Prometheus metrics server on port %d", EXPORTER_PORT)
    try:
        start_http_server(EXPORTER_PORT)
    except OSError as exc:
        sys.exit(f"Failed to bind metrics port {EXPORTER_PORT}: {exc}")
    asyncio.run(SensorPushExporter().run())


if __name__ == "__main__":
    main()
