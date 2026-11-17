"""Daly startup and cell registration using synthetic protocol payloads, no hardware."""

from contextlib import nullcontext
from struct import pack

import pytest

from bms import daly
from dbushelper import DbusHelper
import utils


class DalyReplies:
    """Payloads at the request_data boundary, after framing/checksum validation."""

    def __init__(self, cell_count):
        self.cell_count = cell_count
        self.millivolts = [3300 + i for i in range(cell_count)]
        self.commands = []

    def request(self, ser, command, sentences_to_receive=1):
        self.commands.append(command)
        if command == daly.Daly.command_status:
            return pack(">bb??bhx", self.cell_count, 2, False, True, 0, 12)
        if command == daly.Daly.command_soc:
            return pack(">hhhh", self.cell_count * 33, 0, 30000, 500)
        if command == daly.Daly.command_batt_code:
            return b"".join(pack(">B7s", i + 1, b"TEST") for i in range(5))
        if command == daly.Daly.command_rated_params:
            return pack(">LL", 100000, 3300)
        if command == daly.Daly.command_batt_details:
            return pack(">BBBBBxxx", 0, 0, 26, 1, 2)
        if command == daly.Daly.command_cell_volts:
            assert sentences_to_receive == (self.cell_count + 2) // 3
            padded = self.millivolts + [0, 0]
            return b"".join(pack(">Bhhhx", i + 1, *padded[i * 3 : i * 3 + 3]) for i in range(sentences_to_receive))
        raise AssertionError(f"Unexpected command {command!r}")


class RegisteredPaths(dict):
    def add_path(self, path, value, **kwargs):
        assert path not in self
        dict.__setitem__(self, path, value)

    def register(self):
        pass

    def __setitem__(self, path, value):
        if path not in self:
            raise KeyError(path)
        super().__setitem__(path, value)


def make_battery(monkeypatch, cell_count):
    replies = DalyReplies(cell_count)
    battery = daly.Daly("synthetic-daly", 9600, b"\x40")
    monkeypatch.setattr(daly, "open_serial_port", lambda *args: nullcontext(object()))
    monkeypatch.setattr(battery, "request_data", replies.request)
    return battery, replies


def make_helper(battery):
    helper = DbusHelper.__new__(DbusHelper)
    helper.battery = battery
    helper.setup_instance = lambda: True
    helper._dbusname = "synthetic-daly"
    helper._dbusservice = RegisteredPaths()
    helper.instance = 1
    helper.bms_id = "synthetic-daly"
    helper.settings = {"CustomName": "Synthetic Daly"}
    helper.callback_value_reset_soc_to = 0
    helper.bms_cable_alarm = 0
    # Persistent settings and history are outside this cell-publication test.
    helper.history_calculated_last_time = float("inf")
    helper.settings_saved_last_time = float("inf")
    helper.last_seen_saved_last_time = float("inf")
    assert helper.setup_vedbus()
    return helper


@pytest.mark.parametrize("cell_count", [4, 8, 16])
@pytest.mark.parametrize("data_format", [1, 2, 3])
def test_startup_registers_cell_paths_before_first_voltage_read(monkeypatch, cell_count, data_format):
    monkeypatch.setattr(utils, "BATTERY_CELL_DATA_FORMAT", data_format)
    monkeypatch.setattr(utils, "PUBLISH_BATTERY_DATA_AS_JSON", False)
    battery, replies = make_battery(monkeypatch, cell_count)
    assert battery.test_connection()
    assert battery.cell_count == cell_count
    assert daly.Daly.command_cell_volts not in replies.commands
    helper = make_helper(battery)
    paths = helper._dbusservice
    pattern = "/Cell/{}/Volts" if data_format & 2 else "/Voltages/Cell{}"
    for i in range(1, cell_count + 1):
        assert pattern.format(i) in paths
        assert paths[pattern.format(i)] is None
    assert all(cell.voltage is None for cell in battery.cells)

    assert battery.read_cells_volts(object())
    assert [cell.voltage for cell in battery.cells] == [v / 1000 for v in replies.millivolts]
    battery.soc_calc = battery.soc
    helper.publish_dbus()
    for i, cell in enumerate(battery.cells, 1):
        assert paths[pattern.format(i)] == cell.voltage


def test_repeated_settings_read_preserves_existing_cell_readings(monkeypatch):
    battery, replies = make_battery(monkeypatch, 16)
    assert battery.test_connection()
    assert battery.read_cells_volts(object())
    original_cells = list(battery.cells)
    assert battery.get_settings(object())
    assert battery.cells == original_cells
    assert [cell.voltage for cell in battery.cells] == [v / 1000 for v in replies.millivolts]


@pytest.mark.parametrize("cell_count", [None, 0])
def test_settings_without_a_cell_count_does_not_invent_cells(monkeypatch, cell_count):
    battery, _ = make_battery(monkeypatch, 16)
    battery.cell_count = cell_count
    assert battery.get_settings(object())
    assert battery.cells == []


def test_failed_startup_status_does_not_register_a_battery(monkeypatch):
    battery, _ = make_battery(monkeypatch, 16)
    monkeypatch.setattr(battery, "request_data", lambda *args, **kwargs: False)
    assert not battery.test_connection()
    assert battery.cells == []
