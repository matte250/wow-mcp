"""Reminders triggered by time or place (Reminders.lua, tools/reminders.py)."""

import asyncio
import sys
import time
from datetime import datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from wow_mcp.transport import Transport
from wow_mcp.tools import reminders

# How the Lua test scripts publish, through the real tools.
PUBLISH_SCRIPT = """
import asyncio, json, sys
from wow_mcp.tools import reminders
args = json.loads(sys.argv[1])
if "cancel" in args:
    reminders.cancel_reminder(args["cancel"])
else:
    if "near" in args:
        args["near"] = reminders.Near(**args["near"])
    asyncio.run(reminders.set_reminder(**args))
"""

# /reload: Lua state, loaded slots and frames reset; saved variables don't.
RELOAD = """
local function Reload()
    for _, t in ipairs({ client.loaded, client.tickers, client.timers, client.frames, client.chat, client.sounds, client.fontStrings }) do
        wipe(t)
    end
    return client.LoadAddon()
end
"""

ALARM = 12867


@pytest.fixture
def game(run_client, tmp_path, monkeypatch):
    script = tmp_path / "publish_reminders.py"
    script.write_text(PUBLISH_SCRIPT)
    monkeypatch.setenv("WOWMCP_PUBLISH", f"{sys.executable} {script}")
    return lambda lua: run_client(RELOAD + lua)


def test_time_reminder_fires_once(game):
    game(r"""
        local ns = client.LoadAddon()
        client.Wait()
        client.Publish('{"text":"Check the | auction house","in_minutes":30}')
        client.Wait()
        local r = ns.run.payload.reminders[1]
        assert(r.id == 1 and r.text == "Check the | auction house")
        assert(client.chat[1]:find("WowMCP|r Reminder set: Check the || auction house |cffaaaaaa%(at %d%d:%d%d%)|r$"), client.chat[1])

        -- Ten seconds before it's due.
        client.epoch = r.at - client.now - 10
        client.Wait(19)
        assert(#client.CenterText() == 0 and #client.sounds == 0)
        client.Wait(2)
        assert(client.CenterText()[1] == "|cffffd100Reminder:|r Check the || auction house")
        assert(client.chat[#client.chat] == "|cff33ff99[Claude]|r |cffffd100Reminder:|r Check the || auction house")
        assert(#client.sounds == 1 and client.sounds[1] == 12867)
        assert(WowMCPDB.reminders[1].fired == r.at)

        -- Once: not on later checks, and not after a /reload.
        client.Wait(10)
        assert(#client.sounds == 1)
        Reload()
        client.Wait(10)
        assert(#client.sounds == 0 and #client.chat == 0)
        SlashCmdList.WOWMCP("reminders")
        assert(client.chat[1]:find("No reminders waiting", 1, true))
    """)


def test_time_reminder_due_while_offline(game):
    game(r"""
        client.Publish('{"text":"Log out","in_minutes":1}')
        client.epoch = os.time() + 3600
        client.LoadAddon()
        client.Wait()
        assert(client.sounds[1] == 12867)
        assert(client.chat[#client.chat]:find("Reminder:|r Log out |cffaaaaaa%(due %d%d:%d%d%)|r$"), client.chat[#client.chat])
    """)


def test_place_reminders(game):
    game(r"""
        function GetSubZoneText() return client.subzone or "" end
        client.player = { map = 1453, x = 0.5, y = 0.5 }  -- Stormwind City, the other continent
        client.LoadAddon()
        client.Wait()
        client.Publish('{"text":"Train skinning","zone":"Orgrimmar"}')
        client.Publish('{"text":"Sell junk","subzone":"Razor Hill"}')
        client.Publish('{"text":"Talk to Gornek","near":{"zone":"Durotar","x":60,"y":50,"yards":25}}')
        client.Publish('{"text":"Find Atlantis","near":{"zone":"Atlantis","x":1,"y":1}}')
        client.Wait()
        local chat = table.concat(client.chat, "\n")
        assert(chat:find("Reminder set: Train skinning |cffaaaaaa(in Orgrimmar)|r", 1, true), chat)
        assert(chat:find("Reminder set: Talk to Gornek |cffaaaaaa(within 25 yards of Durotar 60.0, 50.0)|r", 1, true), chat)
        assert(chat:find("Couldn't find this zone on the map:|r Atlantis. This reminder can't fire.", 1, true), chat)
        client.Wait(4)
        assert(#client.sounds == 0)

        local function Fired()
            local texts = {}
            for _, line in ipairs(client.chat) do
                local text = line:match("Reminder:|r (.*)$")
                if text then table.insert(texts, text) end
            end
            return table.concat(texts, ",")
        end

        -- Durotar 40, 50 is 40 yards from the spot; 55, 50 is 10.
        client.player = { map = 1411, x = 0.4, y = 0.5 }
        client.Wait(2)
        assert(Fired() == "")
        client.player.x = 0.55
        client.Wait(2)
        assert(Fired() == "Talk to Gornek")

        client.subzone = "razor hill"
        client.Wait(2)
        assert(Fired() == "Talk to Gornek,Sell junk")

        -- Entering Orgrimmar fires on the zone event, before the next timer check.
        client.player = { map = 1454, x = 0.5, y = 0.5 }
        client.Fire("ZONE_CHANGED_NEW_AREA")
        assert(Fired() == "Talk to Gornek,Sell junk,Train skinning")
        assert(#client.sounds == 3)
        client.Wait(10)
        assert(#client.sounds == 3)
    """)


def test_zone_by_name_and_with_subzone(game):
    game(r"""
        function GetSubZoneText() return client.subzone or "" end
        client.LoadAddon()
        client.Wait()
        -- Stormwind resolves to Stormwind City; the stub's zone text says Durotar.
        client.Publish('{"text":"Bank","zone":"The Durotar","subzone":"Razor Hill"}')
        client.Publish('{"text":"Wrong zone","zone":"Stormwind","subzone":"Razor Hill"}')
        client.Wait(4)
        assert(#client.sounds == 0)
        client.subzone = "Razor Hill"
        client.Wait()
        assert(#client.sounds == 1 and client.chat[#client.chat]:find("Reminder:|r Bank", 1, true))
    """)


def test_cancel_and_list(game):
    game(r"""
        local ns = client.LoadAddon()
        client.Wait()
        client.Publish('{"text":"Train skinning","zone":"Orgrimmar"}')
        client.Publish('{"text":"Stretch","in_minutes":45}')
        client.Wait()
        wipe(client.chat)
        SlashCmdList.WOWMCP("reminders")
        assert(#client.chat == 2 and client.chat[1]:find("Train skinning |cffaaaaaa(in Orgrimmar)", 1, true), client.chat[1])
        assert(WowMCPDB.reminders[1] and WowMCPDB.reminders[2])

        client.Publish('{"cancel":1}')
        client.Wait()
        assert(WowMCPDB.reminders[1] == nil and WowMCPDB.reminders[2])
        client.player = { map = 1454, x = 0.5, y = 0.5 }
        client.Wait(4)
        assert(#client.sounds == 0, "cancelled")
    """)


def test_server_state_reset(game):
    # The server's ids started over: reminder 1 is a new one, not the one that fired before.
    game(r"""
        WowMCPDB = { reminders = { [1] = { created = 5, fired = 10 } } }
        client.LoadAddon()
        client.Wait()
        client.Publish('{"text":"Train skinning","zone":"Durotar"}')
        client.Wait(4) -- the bell's frame is taken on the third tick, the reminder checked on the next
        assert(#client.sounds == 1 and WowMCPDB.reminders[1].fired)
        assert(WowMCPDB.reminders[1].created ~= 5)
    """)


def run(coro):
    return asyncio.run(coro)


def state(addons) -> dict:
    return Transport(addons).load_state()


def test_set_reminder_checks_its_arguments(addons):
    with pytest.raises(ToolError, match="exactly one trigger"):
        run(reminders.set_reminder("x"))
    with pytest.raises(ToolError, match="exactly one trigger"):
        run(reminders.set_reminder("x", in_minutes=5, zone="Orgrimmar"))
    with pytest.raises(ToolError, match="empty"):
        run(reminders.set_reminder("  ", in_minutes=5))
    with pytest.raises(ToolError, match="Can't read at_time"):
        run(reminders.set_reminder("x", at_time="tomorrow"))
    with pytest.raises(ToolError, match="already passed"):
        run(reminders.set_reminder("x", at_time="2020-01-01 10:00"))
    with pytest.raises(ToolError, match="isn't a time of day"):
        run(reminders.set_reminder("x", at_time="25:00"))
    assert "reminders" not in state(addons)


def test_parse_time():
    now = datetime(2026, 11, 10, 21, 15)
    assert reminders.parse_time("21:30", now) == datetime(2026, 11, 10, 21, 30)
    assert reminders.parse_time("9.05", now) == datetime(2026, 11, 11, 9, 5), "the next time it comes round"
    assert reminders.parse_time("21:15", now) == datetime(2026, 11, 11, 21, 15)
    assert reminders.parse_time("2026-11-12T08:00", now) == datetime(2026, 11, 12, 8, 0)


def test_set_reminder(addons):
    result = run(reminders.set_reminder("Stretch", in_minutes=30))
    assert result.startswith("Reminder #1 set: 'Stretch', at ")
    assert "local time. (It's" in result and "isn't running" in result
    result = run(reminders.set_reminder("Train", zone="Orgrimmar", subzone="Valley of Honor"))
    assert "#2 set: 'Train', when the player is in Valley of Honor, Orgrimmar." in result
    result = run(reminders.set_reminder("Gornek", near=reminders.Near(zone="Durotar", x=42.123, y=68.9)))
    assert "within 30 yards of Durotar 42.1, 68.9" in result
    day = datetime.now() + timedelta(days=2)
    run(reminders.set_reminder("Raid", at_time=f"{day:%Y-%m-%d} 20:00"))
    with pytest.raises(ToolError, match="more than 30 days"):
        run(reminders.set_reminder("Raid", at_time=f"{day.year + 1}-01-02 20:00"))

    saved = state(addons)["reminders"]
    assert [r["id"] for r in saved] == [1, 2, 3, 4]
    assert abs(saved[0]["at"] - (time.time() + 1800)) < 5 and abs(saved[0]["created"] - time.time()) < 5
    assert saved[1]["zone"] == "Orgrimmar" and saved[1]["subzone"] == "Valley of Honor" and "at" not in saved[1]
    assert saved[2]["near"] == {"zone": "Durotar", "x": 42.12, "y": 68.9, "yards": 30}
    assert saved[3]["at"] == int(datetime(day.year, day.month, day.day, 20, 0).timestamp())


def test_cleanup_and_limit(addons, monkeypatch):
    transport = Transport(addons)
    long_ago = int(time.time()) - 2 * 86400
    transport.publish(lambda s, seq: s.update(reminders=[{"id": 1, "text": "old", "at": long_ago, "created": long_ago}]))
    run(reminders.set_reminder("new", zone="Orgrimmar"))
    assert [r["text"] for r in state(addons)["reminders"]] == ["new"], "long overdue time reminders are dropped"

    for i in range(reminders.MAX_REMINDERS - 1):
        run(reminders.set_reminder(f"r{i}", zone="Durotar"))
    with pytest.raises(ToolError, match="already 25 reminders"):
        run(reminders.set_reminder("one too many", zone="Durotar"))

    # When the game says some have fired, they make room.
    async def fired():
        return {2: {"id": 2, "fired": 123}, 3: {"id": 3}}, None

    monkeypatch.setattr(reminders, "game_reminders", fired)
    run(reminders.set_reminder("fits", zone="Durotar"))
    ids = [r["id"] for r in state(addons)["reminders"]]
    assert 2 not in ids and 3 in ids and len(ids) == reminders.MAX_REMINDERS


def test_list_and_cancel(addons, monkeypatch):
    assert run(reminders.list_reminders()).reminders == []
    for text in ("a", "b", "c"):
        run(reminders.set_reminder(text, zone="Durotar"))

    listed = run(reminders.list_reminders())
    assert [r.status for r in listed.reminders] == ["unknown"] * 3
    assert "isn't running" in listed.note

    async def known():
        return {1: {"id": 1, "fired": int(datetime(2025, 3, 4, 9, 41).timestamp())}, 2: {"id": 2}}, None

    monkeypatch.setattr(reminders, "game_reminders", known)
    listed = run(reminders.list_reminders())
    assert [(r.id, r.status) for r in listed.reminders] == [(1, "fired"), (2, "waiting"), (3, "not in game yet")]
    assert listed.reminders[0].fired_at == "2025-03-04 09:41" and listed.note is None
    assert listed.reminders[1].trigger == "when the player is in Durotar"

    assert reminders.cancel_reminder(2) == "Reminder #2 cancelled."
    with pytest.raises(ToolError, match="no reminder #2"):
        reminders.cancel_reminder(2)
    assert [r["id"] for r in state(addons)["reminders"]] == [1, 3]


def test_reminders_live(live_game, tmp_path):
    log = tmp_path / "chat.txt"
    setup = f"""
        time = os.time  -- real time, so in_minutes from the server lines up
        local log = io.open([[{log}]], "w")
        DEFAULT_CHAT_FRAME = {{ AddMessage = function(_, msg) log:write(msg, "\\n") log:flush() end }}
    """
    with live_game(setup, seconds=20):
        assert "Reminder #1 set" in run(reminders.set_reminder("Stretch", in_minutes=0.05))
        run(reminders.set_reminder("Train skinning", zone="Orgrimmar"))
        time.sleep(0.5)
        listed = run(reminders.list_reminders())
        assert [r.status for r in listed.reminders] == ["waiting", "waiting"], listed

        for _ in range(60):
            if "Reminder:|r Stretch" in log.read_text():
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"reminder didn't fire: {log.read_text()!r}")
        listed = run(reminders.list_reminders())
        assert [r.status for r in listed.reminders] == ["fired", "waiting"]
        assert listed.reminders[0].fired_at

        reminders.cancel_reminder(2)
        time.sleep(0.5)
        assert [r.status for r in run(reminders.list_reminders()).reminders] == ["fired"]
