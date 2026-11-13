"""Glow in the game's windows (Glow.lua, tools/glow.py)."""

import sys

import pytest
from pydantic import ValidationError

from wow_mcp.tools import glow
from wow_mcp.tools.glow import Highlight, clear_ui_highlights, highlight_in_ui

# client.Publish() hands control to this script, which publishes through the real tools.
PUBLISH_SCRIPT = """
import json, sys
from wow_mcp.tools.glow import Highlight, clear_ui_highlights, highlight_in_ui
args = json.loads(sys.argv[1])
if args.get("clear"):
    clear_ui_highlights()
else:
    highlight_in_ui([Highlight(**h) for h in args["highlights"]], replace=args.get("replace", False))
"""


@pytest.fixture
def publish_glow(addons, tmp_path, monkeypatch):
    script = tmp_path / "publish_glow.py"
    script.write_text(PUBLISH_SCRIPT)
    monkeypatch.setenv("WOWMCP_PUBLISH", f"{sys.executable} {script}")


def test_marked_things_glow_where_they_show(publish_glow, run_client):
    run_client(r"""
        client.spells[133] = { name = "Fireball" }
        client.spells[143] = { name = "Fireball" }
        client.spells[116] = { name = "Frostbolt" }
        client.GlowUI()
        local ns = client.LoadAddon()
        client.Wait()
        local function Count(t) local n = 0 for _ in pairs(t) do n = n + 1 end return n end
        local bags = ContainerFrameCombinedBags
        client.actions[3] = { "spell", 133 }
        client.actions[4] = { "item", 2589 }

        client.Publish('{"highlights":[{"item_id":2589,"color":"green"},{"item_id":4306,"color":"red"},'
            .. '{"spell_id":133},{"frame":"MerchantRepairAllButton"}]}')
        client.Wait()
        assert(client.chat[#client.chat]:find("marked 2 item(s), 1 spell(s), 1 UI element(s)", 1, true), client.chat[#client.chat])
        -- Closed bags and windows don't glow; the action bars do.
        local shown = client.Glowing()
        assert(shown[ActionButton3] == "gold" and shown[ActionButton4] == "green", "spell and item on the bars")
        assert(Count(shown) == 2, Count(shown))

        -- Opening the bags: Linen Cloth is in backpack slot 2.
        bags:Show()
        client.Fire("BAG_UPDATE_DELAYED")
        client.Wait(1)
        shown = client.Glowing()
        assert(shown[bags.buttons[2]] == "green")
        assert(shown[bags.buttons[1]] == nil)
        -- The glow is ours, laid over the button: tinted, with the edge in the mark's colour.
        local g = nil
        for _, frame in ipairs(client.frames) do if frame.target == bags.buttons[2] then g = frame end end
        assert(g and g.parent == UIParent, "a child of UIParent, never of the button")
        assert(g.halo.desaturated and g.halo.blend == "ADD" and g.edges[1].color[2] == 1)
        assert(g:GetFrameLevel() == bags.buttons[2]:GetFrameLevel() + 5)

        -- Closing the bags hides the glow at once, before the next poll.
        bags:Hide()
        client.Tick()
        assert(not g:IsShown())
        bags:Show()
        client.Wait(1)
        assert(client.Glowing()[bags.buttons[2]] == "green")

        -- An item moved away stops glowing within the next poll.
        client.bags[0].slots[2], client.bags[0].slots[9] = nil, { id = 2589, count = 7 }
        client.Wait(1)
        shown = client.Glowing()
        assert(shown[bags.buttons[2]] == nil and shown[bags.buttons[9]] == "green")

        -- The vendor sells Silk Cloth (red) as its third item, and the repair button glows while it's open.
        client.OpenMerchant({ name = "Vendor", npc = 1, items = { { id = 159, price = 25 }, { id = 2592, price = 10 }, { id = 4306, price = 30 } } })
        MerchantFrame:Show()
        client.Wait(2)
        shown = client.Glowing()
        assert(shown[MerchantItem3ItemButton] == "red" and shown[MerchantItem1ItemButton] == nil)
        assert(shown[MerchantRepairAllButton] == "gold")
        MerchantFrame.selectedTab = 2 -- buyback: not the vendor's items
        client.Wait(1)
        assert(client.Glowing()[MerchantItem3ItemButton] == nil)

        -- Loot.
        client.loot[2] = 4306
        LootFrame:Show()
        client.Fire("LOOT_OPENED")
        client.Wait(1)
        assert(client.Glowing()[LootButton2] == "red")

        -- A trainer: any rank of the marked spell (133 is Fireball rank 1), and a recipe's item.
        client.trainer = { { name = "Frostbolt", id = 116 }, { name = "Fireball", id = 143 }, { name = "Recipe", id = 4306, item = true } }
        ClassTrainerFrame:Show()
        client.Fire("TRAINER_SHOW")
        client.Wait(1)
        shown = client.Glowing()
        assert(shown[ClassTrainerSkill2] == "gold" and shown[ClassTrainerSkill1] == nil and shown[ClassTrainerSkill3] == "red")
        ClassTrainerFrame:Hide()

        -- /mcp glow off hides everything until the next update; on brings it back.
        SlashCmdList.WOWMCP("glow off")
        assert(Count(client.Glowing()) == 0)
        client.Wait(2)
        assert(Count(client.Glowing()) == 0)
        SlashCmdList.WOWMCP("glow on")
        client.Wait(1)
        assert(client.Glowing()[LootButton2] == "red")

        -- A frame that doesn't exist is reported once.
        client.Publish('{"replace":true,"highlights":[{"frame":"NoSuchFrame.Button"}]}')
        client.Wait()
        local said = 0
        for _, line in ipairs(client.chat) do if line:find("Can't find 'NoSuchFrame.Button'", 1, true) then said = said + 1 end end
        assert(said == 1, said)
        assert(Count(client.Glowing()) == 0, "replace dropped the others")

        -- /reload: the marks come back from Inbox.lua without another chat line.
        client.Publish('{"replace":true,"highlights":[{"item_id":2589}]}')
        client.Wait()
        wipe(client.loaded); wipe(client.tickers); wipe(client.timers); wipe(client.frames); wipe(client.chat)
        client.GlowUI()
        client.LoadAddon()
        ContainerFrameCombinedBags:Show()
        client.Wait(2)
        assert(client.Glowing()[ContainerFrameCombinedBags.buttons[9]] == "gold")
        assert(#client.chat == 0, table.concat(client.chat, "\n"))

        client.Publish('{"clear":true}')
        client.Wait()
        assert(Count(client.Glowing()) == 0)
        assert(client.chat[#client.chat]:find("Glow cleared.", 1, true))
    """)


def test_better_bags_buttons_glow(publish_glow, run_client):
    run_client(r"""
        client.GlowUI({ betterBags = true })
        client.LoadAddon()
        client.Wait()
        client.Publish('{"highlights":[{"item_id":4865,"color":"purple"}]}')
        client.Wait()
        local items = LibStub("AceAddon-3.0"):GetAddon("BetterBags"):GetModule("ItemFrame").buttonsBySlotkey
        assert(client.Glowing()[items["0_3"].frame] == nil, "BetterBags' bag is closed")
        client.betterBags:Show()
        client.Wait(1)
        assert(client.Glowing()[items["0_3"].frame] == "purple")
        assert(client.Glowing()[items.Container.frame] == nil)
    """)


def test_marks_merge_in_server_state(addons, monkeypatch):
    monkeypatch.setattr(glow, "MAX_MARKS", 3)
    result = highlight_in_ui([Highlight(item_id=1, color="green"), Highlight(spell_id=2)])
    assert result.startswith("Marked 2 thing(s); 2 now glow")
    highlight_in_ui([Highlight(item_id=1, color="red"), Highlight(frame="MerchantFrame.SellAllJunkButton")])
    result = highlight_in_ui([Highlight(item_id=4)])
    assert "Stopped the 1 oldest to stay within 3." in result

    state = glow.transport().load_state()
    assert state["glow"] == {"id": 3, "marks": [
        {"item": 1, "color": "red"}, {"frame": "MerchantFrame.SellAllJunkButton", "color": "gold"},
        {"item": 4, "color": "gold"},
    ]}, "the spell was the oldest; marking item 1 again made it newer"

    highlight_in_ui([Highlight(spell_id=5)], replace=True)
    assert glow.transport().load_state()["glow"]["marks"] == [{"spell": 5, "color": "gold"}]
    assert clear_ui_highlights() == "Nothing glows in game any more."
    assert glow.transport().load_state()["glow"] == {"id": 5, "marks": []}


@pytest.mark.parametrize("bad", [{}, {"item_id": 1, "spell_id": 2}, {"frame": "Foo; os.exit()"}, {"frame": "a..b"}])
def test_a_highlight_names_one_thing(bad):
    with pytest.raises(ValidationError):
        Highlight(**bad)
