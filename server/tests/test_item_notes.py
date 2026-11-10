"""Notes on item tooltips (Tooltips.lua, tools/item_notes.py)."""

import sys

import pytest

from wow_mcp.tools import item_notes
from wow_mcp.tools.item_notes import ItemNote, clear_item_notes, set_item_notes

# client.Publish() hands control to this script, which publishes through the real tools.
PUBLISH_SCRIPT = """
import json, sys
from wow_mcp.tools.item_notes import ItemNote, clear_item_notes, set_item_notes
args = json.loads(sys.argv[1])
if args.get("clear"):
    clear_item_notes()
else:
    set_item_notes([ItemNote(**n) for n in args["notes"]], replace=args.get("replace", False))
"""


@pytest.fixture
def publish_notes(addons, tmp_path, monkeypatch):
    script = tmp_path / "publish_notes.py"
    script.write_text(PUBLISH_SCRIPT)
    monkeypatch.setenv("WOWMCP_PUBLISH", f"{sys.executable} {script}")


def test_notes_on_item_tooltips(publish_notes, run_client):
    run_client(r"""
        local ns = client.LoadAddon()
        client.Wait()
        local Item, Spell = Enum.TooltipDataType.Item, Enum.TooltipDataType.Spell
        local function Linen() return client.ShowTooltip({ type = Item, id = 2589, lines = { { leftText = "Linen Cloth" } } }) end
        assert(#Linen() == 1, "no notes yet")

        client.Publish('{"notes":[{"item_id":2589,"text":"Keep |cffff0000 for First Aid"},{"item_id":4306,"text":"Vendor"}]}')
        client.Wait()
        local lines = Linen()
        assert(#lines == 2 and lines[1].text == "Linen Cloth")
        assert(lines[2].text == "|cff33ff99AI:|r Keep ||cffff0000 for First Aid", lines[2].text)
        assert(lines[2].r == 0.4 and lines[2].g == 0.8 and lines[2].b == 1 and lines[2].wrap)
        assert(client.chat[#client.chat]:find("Item notes updated: 2 item(s)", 1, true))

        -- Only item tooltips, and only items with a note; a link works when there's no id.
        assert(#client.ShowTooltip({ type = Spell, id = 2589, lines = { { leftText = "Linen" } } }) == 1)
        assert(#client.ShowTooltip({ type = Item, id = 2592, lines = { { leftText = "Wool Cloth" } } }) == 1)
        local linked = client.ShowTooltip({ type = Item, hyperlink = "|cffffffff|Hitem:4306::::::::12:::::|h[Silk Cloth]|h|r", lines = {} })
        assert(#linked == 1 and linked[1].text:find("Vendor", 1, true))

        -- Merging by item: Silk's note changes, Linen's stays.
        client.Publish('{"notes":[{"item_id":4306,"text":"Keep for tailoring"}]}')
        client.Wait()
        assert(client.ShowTooltip({ type = Item, id = 4306 })[1].text:find("Keep for tailoring", 1, true))
        assert(#Linen() == 2)

        -- Replacing drops the others.
        client.Publish('{"replace":true,"notes":[{"item_id":4306,"text":"Sell"}]}')
        client.Wait()
        assert(#Linen() == 1)
        assert(client.chat[#client.chat]:find("1 item(s)", 1, true))

        -- /reload: the notes come back from Inbox.lua without another chat line.
        wipe(client.loaded); wipe(client.tickers); wipe(client.timers); wipe(client.frames); wipe(client.chat)
        wipe(client.tooltipPostCalls)
        client.LoadAddon()
        assert(client.ShowTooltip({ type = Item, id = 4306 })[1].text:find("Sell", 1, true))
        assert(#client.chat == 0, table.concat(client.chat, "\n"))

        client.Wait()
        client.Publish('{"clear":true}')
        client.Wait()
        assert(#client.ShowTooltip({ type = Item, id = 4306 }) == 0)
        assert(client.chat[#client.chat]:find("Item notes cleared.", 1, true))
    """)


def test_notes_merge_in_server_state(addons, monkeypatch):
    monkeypatch.setattr(item_notes, "MAX_NOTES", 3)
    result = set_item_notes([ItemNote(item_id=1, text="a"), ItemNote(item_id=2, text="b")])
    assert result.startswith("Set 2 note(s); 2 item(s) now have a note")
    set_item_notes([ItemNote(item_id=1, text="a2"), ItemNote(item_id=3, text="c")])
    result = set_item_notes([ItemNote(item_id=4, text="d")])
    assert "Removed the 1 oldest note(s) to stay within 3." in result

    state = item_notes.transport().load_state()
    assert state["itemNotes"] == {"id": 3, "notes": [
        {"item": 1, "text": "a2"}, {"item": 3, "text": "c"}, {"item": 4, "text": "d"},
    ]}, "item 2 was the oldest; updating item 1 made it newer"

    set_item_notes([ItemNote(item_id=5, text="e")], replace=True)
    assert item_notes.transport().load_state()["itemNotes"]["notes"] == [{"item": 5, "text": "e"}]
    assert clear_item_notes() == "Removed all item tooltip notes."
    assert item_notes.transport().load_state()["itemNotes"] == {"id": 5, "notes": []}
