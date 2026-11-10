"""Slots and doorbells (transport.py)."""

from wow_mcp import kinds
from wow_mcp.transport import BELL_CLEAR_AHEAD, SILENT_WAV, Transport, bell_path


def test_request_numbers_encode_the_kind(addons):
    transport = Transport(addons)
    numbers = [transport.request(kind) for kind in (1, 0, 0, 1, 3)]
    assert numbers == [1, 8, 16, 17, 19]
    assert [n % kinds.COUNT for n in numbers] == [1, 0, 0, 1, 3]
    assert all(bell_path(transport.addon_dir, "req", n).read_bytes() == SILENT_WAV for n in numbers)
    assert bell_path(transport.addon_dir, "req", 2).read_bytes() == b"", "skipped numbers are never rung"


def test_publish_keeps_upcoming_signals_empty(addons):
    transport = Transport(addons)
    stale = bell_path(transport.addon_dir, "sig", 1 + BELL_CLEAR_AHEAD)
    stale.write_bytes(SILENT_WAV)
    transport.publish(lambda state, seq: None)
    assert bell_path(transport.addon_dir, "sig", 1).read_bytes() == SILENT_WAV
    assert stale.read_bytes() == b""
    assert bell_path(transport.addon_dir, "sig", 2).read_bytes() == b""
