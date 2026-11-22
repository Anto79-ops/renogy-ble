"""Tests for reconnecting and the automatic Bluetooth adapter reset.

Run from the repository root:  python -m unittest discover tests
"""

import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ble_client  # noqa: E402
from ble_client import BLEDeviceManager, DeviceConfig, PersistentBLEConnection  # noqa: E402

ROVER = "AA:AA:AA:AA:AA:01"
HUB = "AA:AA:AA:AA:AA:02"


class FakeConnection(PersistentBLEConnection):
    """A connection whose link is a flag instead of a radio."""

    def __init__(self, mac, configs, log):
        super().__init__(mac, configs)
        self.adapter = self._adapter_spec or "hci0"
        self.up = False          # is the link up right now
        self.reachable = True    # does a connection attempt succeed
        self.answers = True      # do the devices return data
        self.drop_in_poll = False
        self.log = log

    @property
    def is_connected(self):
        return self.up

    async def connect(self, attempts=3):
        self.log.append(("connect", self.mac_address, attempts))
        self.up = self.reachable
        return self.up

    async def disconnect(self):
        self.log.append(("disconnect", self.mac_address))
        self.up = False

    async def poll_device(self, config):
        self.log.append(("poll", self.mac_address))
        if self.drop_in_poll:
            self.up = False
            return {}
        return {"value": 1} if self.answers else {}


def make_manager(rover_adapter=None, **kwargs):
    configs = [
        DeviceConfig("Rover", ROVER, "", "controller", bt_adapter=rover_adapter),
        DeviceConfig("Battery", HUB, "", "battery", device_id=48),
        DeviceConfig("Inverter", HUB, "", "inverter", device_id=32),
    ]
    manager = BLEDeviceManager(configs, **kwargs)
    log = []
    for mac, real in list(manager._connections.items()):
        manager._connections[mac] = FakeConnection(mac, real.device_configs, log)
    return manager, log


class Recovery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = 1000.0
        self.reset = mock.Mock(return_value=True)
        self.restart = mock.Mock(return_value=True)
        for patcher in (
            mock.patch.object(ble_client.asyncio, "sleep", mock.AsyncMock()),
            mock.patch.object(ble_client, "time", mock.Mock(
                monotonic=lambda: self.clock, time=lambda: 1.7e9 + self.clock)),
            mock.patch.object(ble_client, "reset_bluetooth_adapter", self.reset),
            mock.patch.object(ble_client, "restart_bluetooth_service", self.restart),
            mock.patch.object(ble_client, "list_bt_adapters", lambda: {}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_healthy_system_never_resets(self):
        manager, _ = make_manager()
        for _ in range(10):
            await manager.poll_all()
        self.assertEqual(self.reset.call_count, 0)

    async def test_reset_after_three_failed_polls(self):
        manager, log = make_manager()
        await manager.poll_all()
        rover = manager._connections[ROVER]
        rover.up = rover.reachable = False

        await manager.poll_all()
        await manager.poll_all()
        self.assertEqual(self.reset.call_count, 0)
        del log[:]
        await manager.poll_all()
        self.assertEqual(self.reset.call_count, 1)
        self.assertEqual(self.restart.call_count, 0)
        # Everything is disconnected for the reset, then the working module is
        # reconnected before the stuck one
        after = [entry for entry in log if entry[0] in ("disconnect", "connect")]
        self.assertEqual(after[-2:], [("connect", HUB, 3), ("connect", ROVER, 1)])
        self.assertIn(("disconnect", HUB), after)
        self.assertTrue(manager._connections[HUB].is_connected)

    async def test_working_module_is_polled_first(self):
        manager, log = make_manager()
        await manager.poll_all()
        rover = manager._connections[ROVER]
        rover.up = rover.reachable = False
        del log[:]
        await manager.poll_all()
        self.assertEqual(log[0], ("poll", HUB))
        self.assertEqual(log[-1], ("connect", ROVER, 3))
        del log[:]
        await manager.poll_all()
        self.assertEqual(log[-1], ("connect", ROVER, 1))  # one try per poll from now on

    async def test_resets_back_off_and_start_over_after_recovery(self):
        manager, _ = make_manager()
        rover = manager._connections[ROVER]
        rover.reachable = False

        async def polls(count, step=120):
            for _ in range(count):
                await manager.poll_all()
                self.clock += step

        await polls(3)
        self.assertEqual(self.reset.call_count, 1)
        await polls(4)                      # 8 more minutes: still inside 10 min
        self.assertEqual(self.reset.call_count, 1)
        await polls(2)
        self.assertEqual(self.reset.call_count, 2)
        await polls(8)                      # 18 min since reset 2: the wait is now 20 min
        self.assertEqual(self.reset.call_count, 2)
        await polls(2)
        self.assertEqual(self.reset.call_count, 3)

        rover.reachable = True              # back: the wait starts over
        await polls(2)
        self.assertEqual(rover.failed_cycles, 0)
        self.assertEqual(manager._reset_state["hci0"][0], ble_client.RESET_MIN_INTERVAL)

    async def test_link_lost_during_every_poll_counts_as_failed(self):
        manager, _ = make_manager()
        rover = manager._connections[ROVER]
        rover.drop_in_poll = True
        for _ in range(3):
            await manager.poll_all()
        self.assertEqual(self.reset.call_count, 1)

    async def test_connected_but_silent_device_does_not_reset(self):
        manager, _ = make_manager()
        manager._connections[HUB].answers = False   # e.g. inverter and battery off
        for _ in range(10):
            await manager.poll_all()
        self.assertEqual(self.reset.call_count, 0)

    async def test_only_the_adapter_of_the_stuck_module_is_reset(self):
        manager, log = make_manager(rover_adapter="hci1")
        self.assertEqual(manager.adapters(), ["hci0", "hci1"])
        await manager.poll_all()
        rover = manager._connections[ROVER]
        rover.up = rover.reachable = False
        del log[:]
        for _ in range(3):
            await manager.poll_all()
        self.reset.assert_called_once_with("hci1")
        self.assertNotIn(("disconnect", HUB), log)     # the hub is left alone
        self.assertTrue(manager._connections[HUB].is_connected)

    async def test_connection_stats_follow_an_outage_and_a_reset(self):
        manager, _ = make_manager(rover_adapter="hci1")
        await manager.poll_all()
        key = f"{ROVER}_controller"
        self.assertEqual(manager.connection_stats(key), {
            "adapter": "hci1", "adapter_resets": 0, "reconnects_24h": 0, "failed_polls": 0,
            "timeouts_24h": 0, "last_issue": None, "last_issue_time": None,
        })
        rover = manager._connections[ROVER]
        rover.up = rover.reachable = False
        await manager.poll_all()
        await manager.poll_all()
        self.assertEqual(manager.connection_stats(key)["failed_polls"], 2)
        await manager.poll_all()
        stats = manager.connection_stats(key)
        self.assertEqual((stats["adapter_resets"], stats["failed_polls"]), (1, 3))
        self.assertIn("hci1 was reset because Rover did not answer for 3 polls: still no link", stats["last_issue"])
        self.assertIsNotNone(stats["last_issue_time"])
        # The hub is on the other adapter: nothing to report there
        hub = manager.connection_stats(f"{HUB}_battery")
        self.assertEqual((hub["adapter"], hub["adapter_resets"], hub["last_issue"]), ("hci0", 0, None))
        rover.reachable = True
        await manager.poll_all()
        self.assertEqual(manager.connection_stats(key)["failed_polls"], 0)

    async def test_auto_reset_can_be_turned_off(self):
        manager, _ = make_manager(auto_reset=False)
        manager._connections[ROVER].reachable = False
        for _ in range(10):
            await manager.poll_all()
        self.assertEqual(self.reset.call_count, 0)

    async def test_service_restart_when_adapter_reset_fails(self):
        self.reset.return_value = False
        manager, _ = make_manager(reset_after=1)
        manager._connections[ROVER].reachable = False
        await manager.poll_all()
        self.assertEqual(self.restart.call_count, 1)


class AdapterChoice(unittest.TestCase):
    PRESENT = {"hci0": "AA:BB:CC:DD:EE:01", "hci1": "AA:BB:CC:DD:EE:02"}

    def connection(self, bt_adapter, present=None):
        config = DeviceConfig("Rover", ROVER, "", "controller", bt_adapter=bt_adapter)
        with mock.patch.object(ble_client, "list_bt_adapters", lambda: self.PRESENT if present is None else present):
            return PersistentBLEConnection(ROVER, [config])

    def test_default_adapter_when_nothing_is_set(self):
        self.assertEqual(self.connection(None).adapter, "hci0")

    def test_by_name_and_by_address(self):
        self.assertEqual(self.connection("hci1").adapter, "hci1")
        self.assertEqual(self.connection("aa:bb:cc:dd:ee:02").adapter, "hci1")

    def test_address_follows_the_hardware_when_names_swap(self):
        swapped = {"hci0": "AA:BB:CC:DD:EE:02", "hci1": "AA:BB:CC:DD:EE:01"}
        self.assertEqual(self.connection("AA:BB:CC:DD:EE:02", swapped).adapter, "hci0")

    def test_missing_adapter_falls_back_to_the_default(self):
        only_builtin = {"hci0": "AA:BB:CC:DD:EE:01"}
        with self.assertLogs(ble_client.logger, level="WARNING"):
            self.assertEqual(self.connection("AA:BB:CC:DD:EE:02", only_builtin).adapter, "hci0")
        with self.assertLogs(ble_client.logger, level="WARNING"):
            self.assertEqual(self.connection("hci1", only_builtin).adapter, "hci0")

    def test_hciconfig_output_is_parsed(self):
        output = (
            "hci1:\tType: Primary  Bus: USB\n\tBD Address: AA:BB:CC:DD:EE:02  ACL MTU: 1021:8  SCO MTU: 64:1\n\tUP RUNNING \n\n"
            "hci0:\tType: Primary  Bus: UART\n\tBD Address: AA:BB:CC:DD:EE:01  ACL MTU: 1021:8  SCO MTU: 64:1\n\tUP RUNNING \n"
        )
        with mock.patch.object(ble_client.subprocess, "run", return_value=mock.Mock(stdout=output)):
            self.assertEqual(
                ble_client.list_bt_adapters(),
                {"hci1": "AA:BB:CC:DD:EE:02", "hci0": "AA:BB:CC:DD:EE:01"},
            )


class ConnectionCounters(unittest.IsolatedAsyncioTestCase):
    def connection(self):
        with mock.patch.object(ble_client, "list_bt_adapters", lambda: {}):
            return PersistentBLEConnection(ROVER, [DeviceConfig("Rover", ROVER, "", "controller")])

    async def test_a_reconnect_is_counted_but_the_first_connect_is_not(self):
        connection = self.connection()
        connection._connect_once = mock.AsyncMock(return_value=True)
        with mock.patch.object(ble_client, "list_bt_adapters", lambda: {}):
            await connection.connect()
            self.assertEqual(len(connection.reconnect_times), 0)
            self.assertIsNone(connection.last_issue(255))
            await connection.connect()          # no client object: counts as not connected
        self.assertEqual(len(connection.reconnect_times), 1)
        self.assertIn("re-established", connection.last_issue(255)[1])

    async def test_a_failed_connect_is_the_last_issue(self):
        connection = self.connection()
        connection._connect_once = mock.AsyncMock(side_effect=TimeoutError())
        with mock.patch.object(ble_client, "list_bt_adapters", lambda: {}), \
                mock.patch.object(ble_client.asyncio, "sleep", mock.AsyncMock()):
            self.assertFalse(await connection.connect(attempts=2))
        self.assertEqual(connection.last_issue(255)[1], "Could not connect over hci0: TimeoutError")

    def test_issue_of_another_device_on_the_hub_is_not_shown(self):
        connection = self.connection()
        connection.note_issue("No reply to register 4000", 32)
        self.assertIsNone(connection.last_issue(48))
        connection.note_issue("Could not connect over hci0: TimeoutError")
        self.assertIn("Could not connect", connection.last_issue(48)[1])
        self.assertIn("Could not connect", connection.last_issue(32)[1])


class ConnectionSensors(unittest.TestCase):
    def test_discovery_once_and_state_every_time(self):
        import json
        from mqtt_handler import MQTTHandler, MQTTConfig
        handler = MQTTHandler(MQTTConfig(host="localhost"))
        handler.client = mock.Mock()
        handler.client.publish.return_value = mock.Mock(rc=0)
        stats = {"adapter": "hci1", "adapter_resets": 1, "reconnects_24h": 2, "failed_polls": 0,
                 "timeouts_24h": 3, "last_issue": "x", "last_issue_time": "2026-11-22T14:00:00-06:00"}
        handler.publish_connection("Solar Controller", "AA:AA:AA:12:34:56", stats)
        handler.publish_connection("Solar Controller", "AA:AA:AA:12:34:56", stats)
        topics = [call.args[0] for call in handler.client.publish.call_args_list]
        configs = [t for t in topics if t.endswith("/config")]
        self.assertEqual(len(configs), 5)
        self.assertEqual(topics.count("renogy/solar_controller_123456/connection"), 2)
        payload = json.loads(handler.client.publish.call_args_list[0].args[1])
        # Not tied to the device's own availability: stays up while it is unreachable
        self.assertEqual(payload["availability_topic"], "renogy/bridge/availability")
        self.assertNotIn("availability", payload)
        self.assertEqual(payload["unique_id"], "solar_controller_123456_connection_adapter_resets")


class LinkLostDuringPoll(unittest.IsolatedAsyncioTestCase):
    async def test_one_reconnect_then_the_poll_is_abandoned(self):
        connects = []

        class Dead(PersistentBLEConnection):
            is_connected = False

            async def connect(self, attempts=3):
                connects.append(attempts)
                return False

            async def read_registers(self, *args):
                raise AssertionError("must not read without a link")

        config = DeviceConfig("Rover", ROVER, "", "controller")
        with mock.patch.object(ble_client.asyncio, "sleep", mock.AsyncMock()), \
                mock.patch.object(ble_client, "list_bt_adapters", lambda: {}):
            data = await Dead(ROVER, [config]).poll_device(config)
        self.assertEqual(data, {})
        self.assertEqual(connects, [1])

    async def test_disconnect_callback_while_connecting_is_counted_not_logged(self):
        connection = PersistentBLEConnection(ROVER, [])
        connection.client = object()
        with self.assertNoLogs(ble_client.logger, level="WARNING"):
            connection._on_disconnect(connection.client)
            connection._on_disconnect(connection.client)
        self.assertEqual(connection._attempt_drops, 2)
        connection._connected = True
        with self.assertLogs(ble_client.logger, level="WARNING"):
            connection._on_disconnect(connection.client)
        self.assertFalse(connection._connected)
        connection._connected = True
        connection._on_disconnect(object())       # stale client: ignored
        self.assertTrue(connection._connected)


if __name__ == "__main__":
    unittest.main()
