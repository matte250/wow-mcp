"""The game-to-server stream of file opens (stream.py, Stream.lua)."""

import threading
import time

import pytest

from wow_mcp import stream


def open_files(out, symbols):
    for symbol in symbols:
        with open(out / f"{symbol:03x}.wav", "rb") as f:
            f.read()


@pytest.fixture
def out(tmp_path):
    folder = tmp_path / "out"
    folder.mkdir()
    for symbol in range(4096):
        (folder / f"{symbol:03x}.wav").touch()
    return folder


def test_find_message_skips_noise_and_old_replies():
    old = stream.encode_message(6, {"old": True})
    reply = stream.encode_message(7, {"zone": "Redridge Mountains", "items": list(range(50))})
    noise = [0x123, 0xFFF, 0x000]
    symbols = noise + old + noise + reply
    assert stream.find_message(symbols, 7) == {"zone": "Redridge Mountains", "items": list(range(50))}
    assert stream.find_message(symbols[:-1], 7) is None, "incomplete"
    assert stream.find_message(noise + old, 7) is None, "only an older reply"
    assert stream.find_message([], 7) is None
    damaged = reply[:]
    damaged[len(damaged) // 2] ^= 0x5A5
    with pytest.raises(stream.Corrupt):
        stream.find_message(damaged, 7)


def test_receiver_reads_file_opens(out):
    # Long runs of the same symbol: consecutive opens of one file must not merge.
    value = {"padding": "\0" * 300, "text": "x" * 500}
    symbols = stream.encode_message(3, value)

    def play():
        time.sleep(0.2)
        open_files(out, symbols)

    with stream.Receiver(out) as receiver:
        threading.Thread(target=play).start()
        assert receiver.receive(3, start_timeout=3, idle_timeout=1) == value
    with stream.Receiver(out) as receiver, pytest.raises(stream.Timeout):
        receiver.receive(4, start_timeout=0.3, idle_timeout=0.3)


def test_find_posts_between_replies_and_across_reloads():
    session, reloaded = 0x03E8_1234, 0x03E9_0042
    first = stream.encode_post(session, 1, {"question": "where am I?", "player": "Thorgrim"})
    second = stream.encode_post(session, 2, {"question": "and now?"})
    after_reload = stream.encode_post(reloaded, 1, {"question": "again"})
    reply = stream.encode_message(1, {"location": {"zone": "Durotar"}})
    noise = [0x123, 0x575, 0xFFF]  # 0x575 is how every message starts
    files = noise + first + reply + second + noise + after_reload

    posts = stream.find_posts(files)
    assert [(p.session, p.id, p.value) for p in posts] == [
        (session, 1, {"question": "where am I?", "player": "Thorgrim"}),
        (session, 2, {"question": "and now?"}),
        (reloaded, 1, {"question": "again"}),
    ]
    assert posts[0].end == len(noise + first)
    assert stream.find_message(files, 1) == {"location": {"zone": "Durotar"}}, "replies are still found"
    assert stream.find_posts(files[:-1]) == posts[:2], "the last one is incomplete"
    assert stream.find_posts(files, {posts[0].key, posts[1].key}) == posts[2:], "already handled"
    assert stream.find_posts(noise + reply) == []

    damaged = first[:]
    damaged[len(damaged) // 2] ^= 0x5A5
    [post] = stream.find_posts(damaged)
    assert post.key == (session, 1) and post.value is None and "damaged" in post.error


def test_receiver_returns_each_post_once(out):
    first_value, second_value = {"question": "where am I?"}, {"question": "x" * 200}
    first, second = stream.encode_post(7, 1, first_value), stream.encode_post(7, 2, second_value)
    reply = stream.encode_message(3, {"location": {}})
    with stream.Receiver(out) as receiver:
        open_files(out, first + reply + second[:10])
        assert [p.value for p in receiver.receive_posts(1)] == [first_value]
        assert receiver.symbols == reply + second[:10], "what came before is let go"
        open_files(out, second[10:])
        assert [p.value for p in receiver.receive_posts(1)] == [second_value]
        assert receiver.receive_posts(0.1) == []
        # Opens that can't be part of a post any more are dropped as they come.
        open_files(out, reply * (stream.POST_WINDOW // len(reply) + 2))
        assert receiver.receive_posts(1) == []
        assert len(receiver.symbols) <= stream.POST_WINDOW


def test_addon_posts(run_client, tmp_path):
    """Stream.Post from Lua decodes here; replies and posts share one queue, and post ids
    start over after a /reload in a new session."""
    opens = tmp_path / "opens.txt"
    run_client("""
        time = function() return 1000 end
        local ns = client.LoadAddon()
        local opened = {}
        local play = PlaySoundFile
        function PlaySoundFile(path, ...)
            opened[#opened + 1] = path:match("out.(%x%x%x)%.wav$")
            return play(path, ...)
        end
        assert(ns.Stream.Post({ question = "first" }) == 1)
        ns.Stream.Send(5, { reply = true })
        assert(ns.Stream.Post({ question = "second" }) == 2)
        client.Wait(10)
        time = function() return 1001 end
        ns = client.LoadAddon() -- /reload
        assert(ns.Stream.Post({ question = "third" }) == 1)
        client.Wait(10)
        local f = assert(io.open("OPENS", "w"))
        f:write(table.concat(opened, " "))
        f:close()
    """.replace("OPENS", str(opens)))
    symbols = [int(s, 16) for s in opens.read_text().split()]
    posts = stream.find_posts(symbols)
    assert [(p.id, p.value) for p in posts] == [(1, {"question": "first"}), (2, {"question": "second"}),
                                                (1, {"question": "third"})]
    assert posts[0].session == posts[1].session != posts[2].session
    assert stream.find_message(symbols, 5) == {"reply": True}
