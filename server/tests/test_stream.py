"""The game-to-server stream of file opens (stream.py, Stream.lua)."""

import threading
import time

import pytest

from wow_mcp import stream


def test_find_message_skips_noise_and_old_replies():
    old = stream.encode_message(6, {"old": True})
    reply = stream.encode_message(7, {"zone": "Redridge Mountains", "items": list(range(50))})
    noise = [0x123, 0xFFF, 0x000]
    symbols = noise + old + noise + reply
    assert stream.find_message(symbols, 7) == {"zone": "Redridge Mountains", "items": list(range(50))}
    assert stream.find_message(symbols[:-1], 7) is None, "incomplete"
    assert stream.find_message(noise + old, 7) is None, "only an older reply"
    damaged = reply[:]
    damaged[len(damaged) // 2] ^= 0x5A5
    with pytest.raises(stream.Corrupt):
        stream.find_message(damaged, 7)


def test_receiver_reads_file_opens(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    for symbol in range(4096):
        (out / f"{symbol:03x}.wav").touch()
    # Long runs of the same symbol: consecutive opens of one file must not merge.
    value = {"padding": "\0" * 300, "text": "x" * 500}
    symbols = stream.encode_message(3, value)

    def play():
        time.sleep(0.2)
        for symbol in symbols:
            with open(out / f"{symbol:03x}.wav", "rb") as f:
                f.read()

    with stream.Receiver(out) as receiver:
        threading.Thread(target=play).start()
        assert receiver.receive(3, start_timeout=3, idle_timeout=1) == value
    with stream.Receiver(out) as receiver, pytest.raises(stream.Timeout):
        receiver.receive(4, start_timeout=0.3, idle_timeout=0.3)
