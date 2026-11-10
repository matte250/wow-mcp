"""Text from the AI shown in game (Messages.lua, tools/messages.py)."""

import sys
import time

import pytest

from wow_mcp.transport import Transport
from wow_mcp.tools import messages

# How the Lua test scripts publish, through the real tools.
PUBLISH_SCRIPT = """
import json, sys
from wow_mcp.tools import messages
args = json.loads(sys.argv[1])
if "sender" in args:
    messages.publish_message(args["text"], args.get("style", "chat"), args["sender"])
else:
    messages.show_message(args["text"], args.get("style", "chat"))
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

TELL, RAID_WARNING = 3081, 8959


@pytest.fixture
def game(run_client, tmp_path, monkeypatch):
    script = tmp_path / "publish_messages.py"
    script.write_text(PUBLISH_SCRIPT)
    monkeypatch.setenv("WOWMCP_PUBLISH", f"{sys.executable} {script}")
    return lambda lua: run_client(RELOAD + lua)


def test_styles_and_no_repeats(game):
    game(r"""
        local ns = client.LoadAddon()
        client.Wait()
        client.Publish('{"text":"Hello | there\\nsecond line\\n\\nthird","style":"chat"}')
        client.Wait()
        assert(#client.chat == 3, table.concat(client.chat, "\n"))
        assert(client.chat[1] == "|cff33ff99[Claude]|r Hello || there", client.chat[1])
        assert(client.chat[2] == "    second line" and client.chat[3] == "    third")
        assert(#client.CenterText() == 0 and client.sounds[1] == 3081)

        client.Publish('{"text":"Pull in 5","style":"center"}')
        client.Wait()
        assert(#client.chat == 3, "center only")
        assert(client.CenterText()[1] == "Pull in 5" and client.sounds[2] == 8959)

        client.Publish('{"text":"Both ways","style":"both","sender":"Bridge"}')
        client.Wait()
        assert(client.chat[4] == "|cff33ff99[Bridge]|r Both ways")
        local center = client.CenterText()
        assert(#center == 2 and center[1] == "Pull in 5" and center[2] == "Both ways", "newest below")
        assert(WowMCPDB.messageId == 3 and #client.sounds == 3)

        -- Shown for about 10 s, then faded out.
        client.Wait(18)
        assert(#client.CenterText() == 2)
        client.Wait(8)
        assert(#client.CenterText() == 0)

        -- /reload: the stored payload carries all three, none is shown again.
        ns = Reload()
        client.Wait()
        assert(ns.run.seq == 3 and #client.chat == 0 and #client.sounds == 0 and #client.CenterText() == 0)
        client.Publish('{"text":"After the reload"}')
        client.Wait()
        assert(#client.chat == 1 and client.chat[1]:find("After the reload", 1, true))
        assert(WowMCPDB.messageId == 4)

        -- /mcp messages repeats the recent ones quietly.
        SlashCmdList.WOWMCP("messages")
        local chat = table.concat(client.chat, "\n")
        assert(chat:find("Recent messages:", 1, true) and chat:find("[Bridge]|r Both ways", 1, true), chat)
        assert(#client.sounds == 1)
    """)


def test_center_keeps_three(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        for i = 1, 4 do
            client.Publish('{"text":"Alert ' .. i .. '","style":"center"}')
            client.Wait()
        end
        local center = client.CenterText()
        assert(#center == 3 and center[1] == "Alert 2" and center[3] == "Alert 4", table.concat(center, ","))
        client.Wait(40)
        assert(#client.CenterText() == 0)
        -- The font strings are reused.
        client.Publish('{"text":"Again","style":"center"}')
        client.Wait()
        assert(client.CenterText()[1] == "Again" and #client.fontStrings == 3)
    """)


def test_messages_sent_while_the_game_was_closed(game):
    game(r"""
        client.Publish('{"text":"Old news","style":"center"}')
        -- Logging in two hours later: in chat with the time it was sent, not on screen.
        client.epoch = os.time() + 7200
        client.LoadAddon()
        assert(#client.CenterText() == 0)
        assert(#client.chat == 1 and client.chat[1]:find("%[Claude%]|r Old news |cffaaaaaa%(%d%d:%d%d%)|r$"), client.chat[1])
    """)


def test_server_state_reset(game):
    # The server's ids started over (its state file was deleted): new messages still show.
    game(r"""
        WowMCPDB = { messageId = 57 }
        client.LoadAddon()
        client.Wait()
        client.Publish('{"text":"Fresh start"}')
        client.Wait()
        assert(#client.chat == 1 and WowMCPDB.messageId == 1)
    """)


def test_publish_message(addons):
    for i in range(12):
        assert messages.publish_message(f"message {i}", "both" if i == 11 else "chat") == i + 1
    state = Transport(addons).load_state()
    assert [m["id"] for m in state["messages"]] == list(range(3, 13))
    assert state["messages"][-1] | {"at": 0} == {"id": 12, "text": "message 11", "style": "both", "from": "Claude", "at": 0}
    assert abs(state["messages"][-1]["at"] - time.time()) < 5

    assert messages.publish_message("  x" * 3000, sender="  ") == 13
    last = Transport(addons).load_state()["messages"][-1]
    assert len(last["text"]) == messages.MAX_TEXT and last["text"].endswith("…") and last["from"] == "Claude"
    with pytest.raises(ValueError, match="style"):
        messages.publish_message("hi", "loud")
    with pytest.raises(ValueError, match="empty"):
        messages.publish_message("   ")


def test_show_message_tool(addons):
    result = messages.show_message("On my way", "center")
    assert result.startswith("Message #1 sent to the game, shown in the middle of the screen.")
    assert "isn't running, so it will show when the player next logs in" in result


def test_show_message_live(live_game, tmp_path):
    log = tmp_path / "chat.txt"
    setup = f"""
        local log = io.open([[{log}]], "w")
        DEFAULT_CHAT_FRAME = {{ AddMessage = function(_, msg) log:write(msg, "\\n") log:flush() end }}
    """
    with live_game(setup):
        result = messages.show_message("Hi from the tool", "both")
        assert "isn't running" not in result
        for _ in range(30):
            if "[Claude]|r Hi from the tool" in log.read_text():
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"message not shown: {log.read_text()!r}")
