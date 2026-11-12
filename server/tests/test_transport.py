"""Slots and the bell (transport.py, bell.py): the frames as written, their receipts and the server's waiting."""

import json
import threading
import time

import pytest

from wow_mcp import bell, kinds, stream, transport
from wow_mcp.install import install
from wow_mcp.transport import Transport


def on_disk(addon_dir):
    """(seal, lead, bits) of the bell as the files are now."""
    def on(name):
        return int((addon_dir / bell.FOLDER / name).stat().st_size > 0)
    return on(bell.SEAL), on(bell.LEAD), sum(on(name) << i for i, name in enumerate(bell.DATA_FILES))


def written(addon_dir):
    """The frame on disk, decoded."""
    seal, lead, bits = on_disk(addon_dir)
    assert seal == lead, "a whole frame"
    return bell.decode(bits, seal)


class FakeGame(threading.Thread):
    """Reads the bell like the addon, in steps, and confirms each new frame over out/ as its receipt (the addon
    plays out/ files: here they're opened). Until it has read a marker valid and then empty, its receipts say
    it hasn't verified the files. `confirm=False`: it reads but never confirms (its receipts get lost)."""

    def __init__(self, addon_dir, confirm=True, step=0.02):
        super().__init__(daemon=True)
        self.addon_dir, self.confirm, self.step = addon_dir, confirm, step
        self.frames, self.taken = [], on_disk(addon_dir)[2]
        self.saw_valid = self.verified = False
        self.stop = threading.Event()

    def run(self):
        while not self.stop.wait(self.step):
            seal, lead, bits = on_disk(self.addon_dir)
            if seal != lead:
                continue
            if seal:
                self.saw_valid = True
            elif self.saw_valid:
                self.verified = True
            frame = bell.decode(bits, seal)
            if bits == self.taken or not frame:
                continue
            self.taken = bits
            self.frames.append(frame)
            if self.confirm:
                receipt = bits | seal * bell.PARITY | (bell.VERIFIED if self.verified else 0)
                for symbol in stream.encode_receipt(receipt):
                    with open(self.addon_dir / "out" / f"{symbol:03x}.wav", "rb"):
                        pass

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.join(2)


def test_frames_carry_their_messages():
    frames = [bell.data(70001), bell.request(kinds.ITEMS, 8193), bell.ack(21, 1800), bell.ack(3, 99999),
              bell.progress(7, 3449), bell.progress(0, None), bell.test(0), bell.test(1)]
    assert frames[0] == bell.Frame(bell.DATA, 70001 - 65536)
    assert frames[1] == bell.Frame(bell.REQUEST, kinds.ITEMS | 1 << 3), "13 bits of request id"
    assert frames[2] == bell.Frame(bell.ACK, 21 % 16 | 1800 << 4)
    assert frames[3].payload >> 4 == bell.MAX_QUIET
    assert frames[4] == bell.Frame(bell.PROGRESS, 7 | bell.tokens_code(3449) << 5)
    assert frames[5].payload >> 5 == bell.UNCOUNTED
    for parity in (0, 1):
        for frame in frames:
            bits = frame.bits(parity)
            assert bits < 1 << bell.DATA_BITS and bell.decode(bits, parity) == frame
            assert bell.decode(bits, 1 - parity) is None, "the CRC covers the parity"
    # A long message: its kind and length, then two bytes a frame.
    assert bell.long(9, b"abc") == [bell.Frame(bell.LONG, 9 | 3 << 4), bell.Frame(bell.MORE, 0x6162),
                                    bell.Frame(bell.MORE, 0x6300)]
    with pytest.raises(ValueError):
        bell.long(16, b"")


def test_torn_frames_are_caught():
    """Any one or two bits read wrong are caught by the CRC; a frame read as nothing but empty files, or nothing
    but valid ones (the client keeping them all), is never a frame."""
    for frame in (bell.data(1), bell.request(kinds.STATUS, 1234), bell.progress(12, 54321)):
        for parity in (0, 1):
            bits = frame.bits(parity)
            for i in range(bell.DATA_BITS):
                assert bell.decode(bits ^ 1 << i, parity) is None
                for j in range(i):
                    assert bell.decode(bits ^ 1 << i ^ 1 << j, parity) is None
    full = (1 << bell.DATA_BITS) - 1
    assert bell.decode(0, 0) is None and bell.decode(0, 1) is None
    assert bell.decode(full, 1) is None and bell.decode(full, 0) is None


def test_token_counts_as_the_game_shows_them():
    """Exact as Chat.lua shows them ("850", "3.4k", rounded to hundreds) up to 99.9k, then in ten thousands."""
    for count, back in [(0, 0), (999, 999), (1000, 1000), (1049, 1000), (1050, 1100), (3449, 3400), (3450, 3500),
                        (99949, 99900), (99950, 100000), (123456, 120000), (5_000_000, 660000)]:
        assert bell.tokens_count(bell.tokens_code(count)) == back, count
        shown = (count + 50) // 100  # what the game shows, in hundreds (Chat.lua's Tokens)
        if count < 99950:
            assert (back + 50) // 100 == shown, count
    assert bell.tokens_count(bell.tokens_code(None)) is None
    assert all(bell.tokens_code(n) < bell.UNCOUNTED for n in range(0, 10**7, 997))


def test_writing_a_frame(addons):
    """lead.wav, the data, seal.wav; the parity flips with every frame; a valid file is a hard link to on.wav (no
    data written), an empty one empty."""
    t = Transport(addons)
    on = t.bell.dir / bell.ON
    assert on_disk(t.addon_dir) == (0, 0, 0), "as installed"
    receipt = t.bell.write(bell.data(5))
    seal, lead, bits = on_disk(t.addon_dir)
    assert (seal, lead) == (1, 1) and receipt == bits | bell.PARITY and written(t.addon_dir) == bell.data(5)
    valid = [name for i, name in enumerate(bell.DATA_FILES) if bits >> i & 1]
    assert valid and all((t.bell.dir / name).samefile(on) for name in [*valid, bell.SEAL, bell.LEAD])
    t.bell.write(bell.data(6))
    assert on_disk(t.addon_dir)[:2] == (0, 0) and written(t.addon_dir) == bell.data(6)
    assert (t.bell.dir / bell.SEAL).stat().st_size == 0 and on.stat().st_nlink == 1 + bin(on_disk(t.addon_dir)[2]).count("1")


def test_publish_without_the_game(addons):
    """The game isn't running: the slots, then one frame for whenever it reads it, and no waiting."""
    t = Transport(addons)
    began = time.monotonic()
    seq = t.publish(lambda state, seq: state.update(map={"id": seq}))
    assert time.monotonic() - began < 1
    assert written(t.addon_dir) == bell.data(seq)
    assert "map = {" in (addons / "WowMCP_S300" / "Inbox.lua").read_text()


def test_each_frame_waits_for_its_receipt(addons, monkeypatch):
    """With the game running, the server writes a frame only once the game has the one before, so it misses none,
    however fast they come; the first receipts after a /reload are answered with test frames until the game has
    seen a marker go from valid to empty (the handshake)."""
    t = Transport(addons)
    monkeypatch.setattr(t, "listening", lambda: True)
    with FakeGame(t.addon_dir) as game:
        for n in range(1, 6):
            assert t.send([bell.progress(n, n * 100)])
        assert t.send(bell.long(2, b"marker"))
    assert [f for f in game.frames if f.type != bell.TEST] == [
        *(bell.progress(n, n * 100) for n in range(1, 6)), *bell.long(2, b"marker")]
    # The first frame left the markers valid; the test frame emptied them: verified from then on.
    assert game.frames[:3] == [bell.progress(1, 100), bell.test(0), bell.progress(2, 200)]
    state = t.bell.load_state()
    assert state["verified"] and state["missed"] == 0 and time.time() - state["confirmed"] < 10


def test_without_receipts_the_server_moves_on(addons, monkeypatch):
    """A game that doesn't confirm (a loading screen, sound off): each frame waits RECEIPT_WAIT, and after a few,
    SHORT_WAIT, so tools don't stall; get_status can tell (bell-state.json)."""
    monkeypatch.setattr(transport, "RECEIPT_WAIT", 0.4)
    monkeypatch.setattr(transport, "SHORT_WAIT", 0.1)
    t = Transport(addons)
    monkeypatch.setattr(t, "listening", lambda: True)
    took = []
    with FakeGame(t.addon_dir, confirm=False) as game:
        for n in range(4):
            began = time.monotonic()
            assert not t.send([bell.data(n)])
            took.append(time.monotonic() - began)
    assert all(0.4 <= s < 0.6 for s in took[:2]) and all(0.1 <= s < 0.3 for s in took[2:]), took
    assert game.frames == [bell.data(n) for n in range(4)], "written all the same"
    assert t.bell.load_state() == {"confirmed": None, "missed": 4, "verified": False}


def test_requests(addons, monkeypatch):
    """A request names its kind and an id, which the reply carries; it waits for the game to take it, on the
    receiver that then waits for the reply."""
    t = Transport(addons)
    with FakeGame(t.addon_dir) as game, stream.Receiver(t.addon_dir / "out") as receiver:
        ids = [t.request(kind, receiver) for kind in (kinds.QUESTS, kinds.STATE, kinds.STATE)]
    assert ids == [1, 2, 3]
    assert [f for f in game.frames if f.type == bell.REQUEST] == [
        bell.request(kinds.QUESTS, 1), bell.request(kinds.STATE, 2), bell.request(kinds.STATE, 3)]
    monkeypatch.setattr(transport, "RECEIPT_WAIT", 0.2)
    with stream.Receiver(t.addon_dir / "out") as receiver, pytest.raises(stream.Timeout):
        t.request(kinds.STATUS, receiver)
    assert json.loads(t.state_path.read_text())["req"] == 4


def test_install_makes_the_bell_and_clears_the_old_doorbells(tmp_path, capsys, monkeypatch):
    addons = tmp_path / "AddOns"
    old = addons / "WowMCP"
    for pool, count in (("sig", 1000), ("req", 500), ("ctl", 1)):
        (old / pool).mkdir(parents=True)
        for n in range(count):
            (old / pool / f"{n:04d}.wav").touch()
    done = install(addons)
    assert done.removed == 1501 and not any((old / pool).exists() for pool in ("sig", "req", "ctl"))
    assert sorted(p.name for p in (old / bell.FOLDER).iterdir()) == sorted(bell.FILES)
    assert (old / bell.FOLDER / bell.ON).read_bytes() == bell.silence()
    assert all((old / bell.FOLDER / name).stat().st_size == 0 for name in bell.FILES if name != bell.ON)
    assert done.created > len(bell.FILES)

    # Again: nothing new, the bell as the server left it.
    Transport(addons).bell.write(bell.data(3))
    frame = on_disk(old)
    assert install(addons) == type(done)(0, 0) and on_disk(old) == frame

    # What the installer says: the old files gone, and a restart when files are new.
    from wow_mcp import install as installer
    (old / bell.FOLDER / "07.wav").unlink()
    (old / "sig").mkdir()
    (old / "sig" / "0000.wav").touch()
    monkeypatch.setattr("sys.argv", ["wow-mcp-install", str(addons)])
    installer.main()
    said = capsys.readouterr().out
    assert "Removed the 1 files of the old doorbells (sig/, req/, ctl/)." in said
    assert "1 new files: fully quit and restart WoW once so it can see them." in said


def test_no_hard_links(addons, monkeypatch):
    """A drive without hard links: valid files are copies."""
    def refuse(*args):
        raise OSError("not on this drive")

    monkeypatch.setattr(transport.os, "link", refuse)
    t = Transport(addons)
    t.bell.write(bell.data(1))
    assert written(t.addon_dir) == bell.data(1)
    assert (t.bell.dir / bell.SEAL).read_bytes() == bell.silence()
    assert not any(p.name.endswith(".tmp") for p in t.bell.dir.iterdir())
