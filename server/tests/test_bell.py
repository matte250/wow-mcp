"""The bell from the game's side (Core.lua): the stub client streams long sounds and keeps short ones like the
real one (stub/apis/sound.lua); the addon reads frames from them, does what they say, confirms them, reads as
little as it can, and checks at login and in its first exchange with the server that the files are read afresh."""

import asyncio
import json
import re
import time
from pathlib import Path

import pytest

from wow_mcp import bell, bridge, kinds, transport
from wow_mcp.tools import messages, player, status
from wow_mcp.transport import Transport, silent_wav

ADDON = Path(__file__).parents[2] / "addon" / "WowMCP"

# For Lua scripts: write bell files as the server would, and count what the client read from the bell.
BELL = """
local BELL = client.addonsDir .. "/WowMCP/bell/"
local SILENCE = client.ReadFile(BELL .. "on.wav")

local function Set(name, on)
    local f = assert(io.open(BELL .. name, "wb"))
    if on then f:write(SILENCE) end
    f:close()
end

-- A frame as the server writes it (bits from bell.py): lead.wav, the data from bit `from` to `to`, seal.wav.
local function Write(bits, parity, from, to)
    if (from or 0) == 0 then Set("lead.wav", parity == 1) end
    for i = from or 0, to or 23 do Set(string.format("%02d.wav", i), (bits >> i) & 1 == 1) end
    if (to or 23) == 23 then Set("seal.wav", parity == 1) end
end

-- The game's reads of bell files since `since` (not the startup scan), by file name.
local function Reads(since)
    local count, by = 0, {}
    for _, read in ipairs(client.reads) do
        local name = read.path:match("^WowMCP/bell/(.+)$")
        if name and not read.scan and read.at > since then
            count, by[name] = count + 1, (by[name] or 0) + 1
        end
    end
    return count, by
end
"""


def test_the_client_streams_long_sounds_and_keeps_short_ones(run_client, addons):
    """The stub as the probes measured the client: a long sound is read on every play and plays as long as it is;
    a short one is read once and plays as it was for the rest of the launch; an empty one never plays and is read
    every time; startup reads every file, and only files there then can be played."""
    probe = addons / "WowMCP" / "probe"
    probe.mkdir()
    (probe / "long.wav").write_bytes(bell.silence(3))
    (probe / "short.wav").write_bytes(silent_wav())  # 2 s
    (probe / "empty.wav").write_bytes(b"")
    run_client(BELL + r"""
        local P = "Interface\\AddOns\\WowMCP\\probe\\"
        local function Plays(file)
            local willPlay, handle = PlaySoundFile(P .. file, "Master")
            return willPlay and C_Sound.IsPlaying(handle), handle
        end
        local function ReadsOf(file)
            local n = 0
            for _, read in ipairs(client.reads) do
                if read.path == "WowMCP/probe/" .. file and not read.scan then n = n + 1 end
            end
            return n
        end
        local scanned = 0
        for _, read in ipairs(client.reads) do if read.scan then scanned = scanned + 1 end end
        assert(scanned > 4400, "startup reads every file: " .. scanned)

        local playing, handle = Plays("long.wav")
        assert(playing and ReadsOf("long.wav") == 1)
        client.Wait(4)  -- 2 s
        assert(C_Sound.IsPlaying(handle))
        client.Wait(2)
        assert(not C_Sound.IsPlaying(handle), "3 s long")
        assert(Plays("short.wav") and not Plays("empty.wav"))
        client.Tick()
        -- The server empties all three.
        for _, file in ipairs({ "long.wav", "short.wav", "empty.wav" }) do io.open(client.addonsDir .. "/WowMCP/probe/" .. file, "wb"):close() end
        assert(not Plays("long.wav") and ReadsOf("long.wav") == 2, "streamed: read again")
        assert(Plays("short.wav") and ReadsOf("short.wav") == 1, "kept from the first read")
        assert(not Plays("empty.wav") and ReadsOf("empty.wav") == 2)
        -- Played twice in a frame: read once.
        Plays("empty.wav")
        assert(ReadsOf("empty.wav") == 2)
        -- A file made after startup can't be played until the client restarts; nor can paths spelled otherwise.
        local f = io.open(client.addonsDir .. "/WowMCP/probe/new.wav", "wb"); f:close()
        assert(not PlaySoundFile(P .. "new.wav", "Master"))
        assert(PlaySoundFile(P:upper() .. "LONG.WAV", "Master"), "the case doesn't matter")
        client.Launch()
        assert(PlaySoundFile(P .. "new.wav", "Master"))
        assert(not Plays("short.wav"), "a restart forgets what was kept")
    """)


def test_torn_frames_are_read_again(run_client):
    """A frame read while the server writes it has markers that differ; one torn some other way fails its CRC.
    Neither does anything: it's read again a few times, and the next whole frame is taken."""
    frames = [bell.request(kinds.STATE, n).bits(n % 2) for n in range(5, 10)]
    run_client(BELL + f"""
        local ns = client.LoadAddon()
        local answered = {{}}
        ns.Answer = function(id, kind) answered[#answered + 1] = id .. ":" .. kind end
        local frames = {{ {", ".join(map(str, frames))} }}
        client.Wait()
        assert(ns.run.live and ns.run.frame == 0, "read the bell as installed")
        -- Two whole frames: the markers valid, then empty, so the game has seen the files change.
        Write(frames[1], 1)
        client.Settle()
        Write(frames[2], 0)
        client.Settle()
        assert(table.concat(answered, " ") == "5:0 6:0" and ns.run.verified)
        client.Wait(12)

        -- The server flipped lead.wav and wrote half the data, and the game reads now (after a loading screen).
        Write(frames[3], 1, 0, 11)
        local since = client.now
        client.Fire("LOADING_SCREEN_DISABLED")
        client.Wait(10)
        local _, by = Reads(since)
        assert(#answered == 2 and by["lead.wav"] == 5, "read, and again 4 times: " .. tostring(by["lead.wav"]))
        -- The rest: seal.wav flips, the whole frame is read and taken.
        Write(frames[3], 1, 12, 23)
        client.Settle()
        assert(table.concat(answered, " ") == "5:0 6:0 7:0", table.concat(answered, " "))

        -- A whole frame with a bit read wrong fails its CRC: not taken, read again 4 times; the next one is taken.
        since = client.now
        Write(frames[4] ~ (1 << 9), 0)
        client.Wait(10)
        _, by = Reads(since)
        assert(#answered == 3 and by["lead.wav"] == 5 and ns.run.frame == frames[4] ~ (1 << 9))
        Write(frames[5], 1)
        client.Settle()
        assert(table.concat(answered, " ") == "5:0 6:0 7:0 9:0", table.concat(answered, " "))
    """)


def test_how_often_the_game_reads(run_client):
    """While nothing happens, the game reads seal.wav every 2 s; every 0.25 s while it's busy (the server sent
    something within 90 s, it logged in within 90 s, an answer is due, the auction house or a mailbox is open); a
    new frame costs 26 reads. The old doorbells read 18 files a second, always."""
    run_client(BELL + r"""
        client.framesPerTick = 2  -- render frames 0.25 s apart, so the bell steps as in the game
        local ns = client.LoadAddon()
        client.Wait(4)
        local function Rate(seconds)
            local since = client.now
            client.Wait(seconds * 2)
            local count, by = Reads(since)
            return count / seconds, by
        end

        assert(Rate(60) == 4, "just logged in")
        client.Wait(64)
        local idle, by = Rate(60)
        assert(idle == 0.5 and by["seal.wav"] == 30, "idle: " .. idle)

        client.OpenAuctionHouse()
        local busy, by = Rate(20)
        assert(busy == 4 and by["seal.wav"] == 80, "at the auction house: " .. busy)
        client.CloseAuctionHouse()
        assert(Rate(20) == 0.5)
        client.OpenMailbox()
        assert(Rate(10) == 4)
        client.CloseMailbox()
        client.Type("/ai anyone?")
        assert(Rate(10) == 4)
        ns.AIChat.Answer("Yes.", "chat", "Claude", nil, nil, nil, nil)
        assert(Rate(10) == 0.5)

        -- New data, twice: the first exchanges show the game the files change (the markers valid, then empty).
        client.Publish('{"markers":[{"zone":"Durotar","x":50,"y":50,"label":"Razor Hill"}]}')
        client.Publish('{"clear":true}')
        client.Settle()
        assert(ns.run.verified and client.loaded.WowMCP_S002)
        client.Wait(10)
        -- Then a frame costs a look that sees seal.wav flip and the whole frame once (26 files), and the server
        -- counts as busy for 90 s: seal.wav every step meanwhile.
        local since = client.now
        client.Publish('{"markers":[{"zone":"Orgrimmar","x":50,"y":50,"label":"Gate"}]}')
        client.Settle()
        local count, by = Reads(since)
        assert(client.loaded.WowMCP_S003 and by["lead.wav"] == 1 and by["00.wav"] == 1 and by["23.wav"] == 1)
        assert(count == 26 + by["seal.wav"] - 1 and by["seal.wav"] <= 5, count)
        assert(Rate(60) == 4, "busy after the frame")
        client.Wait(64)
        assert(Rate(20) == 0.5, "and idle again 90 s after it")
    """)


def test_self_test_at_login(run_client):
    """Before reading the bell: a long silent sound must play and an empty one mustn't. Bell files the client didn't
    find at startup (installed after it started) can't be played at all: restart WoW. Sound off: test_map."""
    run_client(BELL + r"""
        local function Reload()
            for _, t in ipairs({ client.loaded, client.tickers, client.timers, client.frames, client.chat }) do wipe(t) end
            return client.LoadAddon()
        end
        local BELL_DIR = client.addonsDir .. "/WowMCP/bell"
        assert(os.rename(BELL_DIR, BELL_DIR .. "-away"))
        client.Launch()
        assert(os.rename(BELL_DIR .. "-away", BELL_DIR))
        local ns = client.LoadAddon()
        client.Wait()
        assert(not ns.run.live and ns.run.liveProblem == "the game hasn't seen the live-update files yet: fully restart WoW",
            tostring(ns.run.liveProblem))
        assert(table.concat(client.chat, "\n"):find("Live updates are off: the game hasn't seen the live-update files yet", 1, true))
        local before = Reads(0)
        client.Wait(10)
        assert(Reads(0) == before, "nothing read once off")

        client.Launch()  -- the restart
        ns = Reload()
        client.Wait()
        assert(ns.run.live and not ns.run.liveProblem)

        Set("off.wav", true)  -- an empty file that plays would make every bit look set
        ns = Reload()
        client.Wait()
        assert(not ns.run.live and ns.run.liveProblem == "an empty sound file plays")
    """)


# Lua for live_game: logs what the game says in chat, and its AI tab's last line and slots left, and types the
# lines the test writes into TYPED.
GAME = """
local function Log(line)
    local f = assert(io.open("LOG", "a"))
    f:write(line, "\\n")
    f:close()
end
local addMessage = DEFAULT_CHAT_FRAME.AddMessage
DEFAULT_CHAT_FRAME.AddMessage = function(self, msg)
    Log("chat: " .. msg)
    return addMessage(self, msg)
end
local loadAddon = client.LoadAddon
function client.LoadAddon()
    local ns = loadAddon()
    ns.RegisterLong(3, function(bytes) Log("long 3: " .. bytes) end)
    C_Timer.NewTicker(0.5, function()
        local f = io.open("TYPED")
        if f then
            local lines = f:read("a")
            f:close()
            os.remove("TYPED")
            for line in lines:gmatch("[^\\n]+") do client.Type(line) end
        end
        for i = 1, 10 do
            local name = GetChatWindowInfo(i)
            if name == "AI" then
                local lines = client.Lines(_G["ChatFrame" .. i])
                local out = assert(io.open("STATUS", "w"))
                out:write((lines[#lines] or "") .. "\\n" .. ns.SlotsLeft() .. "\\n")
                out:close()
            end
        end
    end)
    return ns
end
"""


@pytest.fixture
def game(live_game, tmp_path):
    """live_game with GAME: returns (start, log, typed, status line, slots left)."""
    log, typed, line = tmp_path / "game.log", tmp_path / "typed.txt", tmp_path / "status.txt"

    def start(setup="", seconds=10):
        lua = GAME.replace("LOG", log.as_posix()).replace("TYPED", typed.as_posix()).replace("STATUS", line.as_posix())
        return live_game(lua + setup, seconds)

    def status_line():
        text = line.read_text(encoding="utf-8").split("\n") if line.exists() else ["", "0"]
        return text[0], int(text[1] or 0)

    def type_in(text):
        typed.with_suffix(".tmp").write_text(text + "\n")
        typed.with_suffix(".tmp").rename(typed)

    return start, log, type_in, status_line


def wait_for(condition, seconds=10):
    deadline = time.monotonic() + seconds
    while not (result := condition()):
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)
    return result


def test_requests_end_to_end(live_game, addons):
    """A tool asks the game: the request frame, its receipt, the reply over out/. The first exchange after a /reload
    also shows the game the files change (the handshake), so get_status then says live updates are verified."""
    with live_game():
        location = asyncio.run(player.get_player_location())
        first = asyncio.run(status.get_status())
        second = asyncio.run(status.get_status())
    assert location.zone == "Durotar"
    assert first.live_updates and first.live_verified and second.ok and second.slots_left == 300
    state = Transport(addons).bell.load_state()
    assert state["verified"] and state["missed"] == 0


def test_ai_status_is_live_and_costs_no_slot(game):
    """The bridge's notes about a question come over the bell: the ack, then the step and the exact token count as
    the AI tab's status line shows them, every one at once, without a live-update slot."""
    start, log, type_in, status_line = game
    with start(seconds=12):
        type_in("/ai any good flips near me?")  # the first post of this UI session: question 1
        wait_for(lambda: "Thinking…" in status_line()[0])
        assert messages.publish_status("ack", question=(77, 1), quiet=120)
        assert messages.publish_status("progress", "checking auction prices", tokens=3449)
        wait_for(lambda: re.search(r"Thinking… 0:\d\d · \|A:[^|]+\|a 3\.4k tokens · checking auction prices\|r$",
                                   status_line()[0]))
        # The count as it is: no more written since, so it doesn't run on.
        assert messages.publish_status("progress", bridge.WRITING, tokens=3449)
        wait_for(lambda: status_line()[0].endswith(" 3.4k tokens · writing|r"))
        time.sleep(0.3)
        assert status_line()[0].endswith(" 3.4k tokens · writing|r") and status_line()[1] == 300, "no slot used"


def test_long_messages(live_game, addons, tmp_path, monkeypatch):
    """A message longer than a frame: its frames one by one, then the whole of it for the feature that takes it."""
    log = tmp_path / "game.log"
    setup = GAME.replace("LOG", log.as_posix()).replace("TYPED", (tmp_path / "t").as_posix()).replace(
        "STATUS", (tmp_path / "s").as_posix())
    with live_game(setup):
        assert transport.Transport(addons).send(bell.long(3, b"Razor Hill, 52.1, 41.7"))
        wait_for(lambda: log.exists() and "long 3: " in log.read_text())
    assert "long 3: Razor Hill, 52.1, 41.7" in log.read_text().splitlines()


def test_a_client_that_keeps_the_files(live_game, addons, monkeypatch):
    """Were the client to keep the bell's files like short sounds (here on.wav is short, so every valid file is), the
    handshake's test frame wouldn't read as written: the game turns live updates off and says so, and the server
    sees no receipt, which get_status passes on."""
    (addons / "WowMCP" / bell.FOLDER / bell.ON).write_bytes(silent_wav())
    monkeypatch.setattr(transport, "RECEIPT_WAIT", 1.0)
    log = addons.parent / "game.log"
    setup = GAME.replace("LOG", log.as_posix()).replace("TYPED", (addons.parent / "t").as_posix()).replace(
        "STATUS", (addons.parent / "s").as_posix())
    with live_game(setup, seconds=12):
        assert asyncio.run(player.get_player_location()).zone == "Durotar", "the first frame is read as written"
        wait_for(lambda: log.exists() and "Live updates are off" in log.read_text())
        result = asyncio.run(status.get_status())
    assert ("chat: |cff33ff99WowMCP|r Live updates are off: the live-update files don't change on disk as written "
            "(the game may keep them in memory); fully restart WoW. New data will show after /reload.") in log.read_text()
    assert not result.game_answered and Transport(addons).bell.load_state()["missed"] >= 2
    assert "The game hasn't confirmed a live update since" in result.advice[0]


def test_sound_off(live_game, addons):
    """Game sound off: live updates are off (the game says so, test_map), and the server sees no receipts, which
    get_status says, with what to do."""
    with live_game("client.sound = false"):
        result = asyncio.run(status.get_status())
    assert not result.game_answered and result.game_problem.startswith("The game didn't answer.")
    assert result.advice[0].startswith("The game hasn't confirmed a live update yet (it reads them by playing silent "
                                       "sound files, so game sound must be on). Ask the player to type /mcp status")


def test_steps_are_numbered_alike():
    """The bell carries a step as its number: Chat.lua's STEPS lists them as bridge.STEP_CODES does, every step the
    bridge can name is there, and they fit in 5 bits."""
    chat = (ADDON / "Chat.lua").read_text(encoding="utf-8")
    listed = re.search(r"local STEPS = \{\s*\[0\] = \"\",(.*?)\n\}", chat, re.S).group(1)
    assert ["", *re.findall(r'"([^"]+)"', listed)] == list(bridge.STEP_CODES)
    assert set(bridge.STEPS.values()) | {bridge.THINKING, bridge.WRITING, bridge.LOOKING, bridge.UPDATING,
                                         bridge.OTHER_TOOLS} <= set(bridge.STEP_CODES)
    assert len(bridge.STEP_CODES) <= 32 and len(set(bridge.STEP_CODES)) == len(bridge.STEP_CODES)
    assert bridge.step_code("a step nobody numbered") == bridge.STEP_CODES.index(bridge.OTHER_TOOLS)
    # And the frame types and the question ids as Core.lua and Chat.lua have them.
    core = (ADDON / "Core.lua").read_text(encoding="utf-8")
    assert "local MORE, DATA, REQUEST, ACK, PROGRESS, TEST, LONG = 0, 1, 2, 3, 4, 5, 6" in core
    assert (bell.MORE, bell.DATA, bell.REQUEST, bell.ACK, bell.PROGRESS, bell.TEST, bell.LONG) == tuple(range(7))
    assert f"local QUESTION_IDS = {bell.QUESTION_IDS} " in chat
    assert json.dumps(bell.FILES)  # what the installer makes
