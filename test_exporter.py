import unittest
from types import SimpleNamespace
from unittest import mock

from prometheus_client import REGISTRY

import exporter

HT_W = 65


def _ht_w(temp_c, rh):
    """(company id, payload) of an HT.w page-0 reading."""
    (t_min, t_max, t_step), (h_min, _, h_step) = exporter.SENSORPUSH_PACK_PARAMS[HT_W]
    t_span = int((t_max - t_min) / t_step + t_step / 2.0) + 1
    packed = round((temp_c - t_min) / t_step) + round((rh - h_min) / h_step) * t_span
    raw = packed.to_bytes(4, "little")
    return ((HT_W - 64) << 2) | raw[0] << 8, raw[1:]


def _full_cache():
    """BlueZ's ManufacturerData once every page-0 company id has been seen."""
    cache = dict(_ht_w(20.0 + i * 0.0025, 50.0) for i in range(256))
    assert len(cache) == 256
    return cache


class CachedManufacturerDataTest(unittest.TestCase):
    def setUp(self):
        self.exporter = exporter.SensorPushExporter()
        self.address = "F8:FB:90:00:00:01"
        self.device = SimpleNamespace(address=self.address, name=None)

    def tearDown(self):
        exporter._remove_device(self.address)

    def _advertise(self, manufacturer_data, now=1000.0):
        adv = SimpleNamespace(
            local_name="SensorPush HT.w 0000",
            service_uuids=[],
            manufacturer_data=manufacturer_data,
            rssi=-70,
        )
        with mock.patch.object(exporter.time, "time", return_value=now):
            self.exporter._process_advertisement(self.device, adv)

    def _sample(self, name):
        return REGISTRY.get_sample_value(name, {"device_address": self.address})

    def test_new_reading_overwrites_key_in_full_cache(self):
        cache = _full_cache()
        self._advertise(cache)

        company_id, payload = _ht_w(22.5, 55.0)
        # The fix only shows if the overwritten key is not the newest in order.
        self.assertIn(company_id, cache)
        self.assertNotEqual(company_id, list(cache)[-1])
        self.assertNotEqual(cache[company_id], payload)
        self._advertise({**cache, company_id: payload})

        self.assertEqual(self._sample("sensorpush_temperature_celsius"), 22.5)
        self.assertEqual(self._sample("sensorpush_humidity_percent"), 55.0)

    def test_unchanged_cache_keeps_reading_and_timestamp(self):
        cache = _full_cache()
        self._advertise(cache, now=1000.0)
        # A full cache on first sight has no way to tell which entry is new.
        self.assertIsNone(self._sample("sensorpush_temperature_celsius"))
        self.assertIsNone(self._sample("sensorpush_last_update_timestamp"))

        company_id, payload = _ht_w(22.5, 55.0)
        cache = {**cache, company_id: payload}
        self._advertise(cache, now=1036.0)
        self._advertise(dict(cache), now=1072.0)

        self.assertEqual(self._sample("sensorpush_temperature_celsius"), 22.5)
        self.assertEqual(self._sample("sensorpush_last_update_timestamp"), 1036.0)
        self.assertEqual(self._sample("sensorpush_advertisements_received_total"), 3)


if __name__ == "__main__":
    unittest.main()
