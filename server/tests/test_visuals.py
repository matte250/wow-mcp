"""Visuals: tables, lists and texts the AI shows in a window of their own (the Visuals window in Messages.lua,
tools/visuals.py).

Stub measures (stub/client.lua): a character is 7 px wide, an inline texture 12 px. Stub items: Light Leather
(2318, icon 134252, white), Toughened Leather Gloves (4253, green), Linen Cloth (2589), Coarse Thread (2320, icon
132889), Medium Leather (2319, takes half a second to load). Stub money: GetMoneyString as the game's, with coin
icons (stub/apis/visuals.lua).
"""

import json
import sys
import time

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import TypeAdapter, ValidationError

from wow_mcp import game as wow_game
from wow_mcp.tools import visuals
from wow_mcp.transport import Transport

# How a Lua test script hands control back to publish through the real tools.
PUBLISH_SCRIPT = """
import json, sys
from wow_mcp import app
from wow_mcp.tools import messages, visuals
args = json.loads(sys.argv[1])
if "clear" in args:
    visuals.clear_visuals(args["clear"])
elif "raw" in args:  # past the tool's checks, as a newer server might send it
    app.transport().publish(lambda state, seq: state.update(visuals={"id": seq, "list": [{"id": seq, **args["raw"]}]}))
elif "message" in args:
    messages.publish_message(args["message"])
else:
    visuals.visualize(args["title"], args["blocks"], subtitle=args.get("subtitle"))
"""


def lua_json(value):
    """JSON for client.Publish, which passes it through the shell in single quotes."""
    return json.dumps(value).replace("'", "\\u0027")


def visual(title, *blocks, subtitle=None):
    return lua_json({"title": title, "blocks": list(blocks), "subtitle": subtitle})


def table(columns, rows, **more):
    return {"type": "table", "columns": columns, "rows": rows, **more}


def col(name, kind="text", **more):
    return {"name": name, "type": kind, **more}


PRELUDE = r"""
    local function chat() return table.concat(client.chat, "\n") end
    local function last() return client.chat[#client.chat] end
    local function has(text) return chat():find(text, 1, true) ~= nil end
    local function mcp(command) SlashCmdList.WOWMCP(command) end
    -- Text without colour codes and inline textures.
    local function plain(text)
        return (tostring(text or ""):gsub("|c%x%x%x%x%x%x%x%x", ""):gsub("|r", ""):gsub("|T.-|t ?", ""))
    end
    local function show(json)
        client.Publish(json)
        client.Wait()
    end
    local function win() return WowMCPVisualFrame end
    local function open() return WowMCPVisualFrame ~= nil and WowMCPVisualFrame:IsShown() end
    local function holder(kind, n) return WowMCPVisualFrame.Holders[kind][n or 1] end
    local function shown(list)
        local out = {}
        for _, f in ipairs(list or {}) do if f:IsShown() then out[#out + 1] = f end end
        return out
    end
    local function rows(h) return shown((h or holder("table")).Rows) end
    local function cells(row)
        local out = {}
        for _, c in ipairs(row.Cells) do if c:IsShown() then out[#out + 1] = c.Text.text end end
        return out
    end
    -- One column of the table's rows on show, as plain text.
    local function column(n, h)
        local out = {}
        for _, r in ipairs(rows(h)) do out[#out + 1] = plain(r.Cells[n].Text.text) end
        return table.concat(out, ",")
    end
    local function header(n, h) return (h or holder("table")).Headers[n] end
    local function status(h) return (h or holder("table")).Status:IsShown() and (h or holder("table")).Status.text or nil end
    -- The point a frame was last put at: { point, relativeTo, relativePoint, x, y }.
    local function at(frame) return frame.points[#frame.points] end
    -- /reload: Lua state, frames and loaded slots reset; saved variables don't.
    local function Reload()
        for _, t in ipairs({ client.loaded, client.tickers, client.timers, client.frames, client.chat, client.sounds,
                             client.fontStrings }) do
            wipe(t)
        end
        WowMCPVisualFrame = nil
        return client.LoadAddon()
    end
    local function noErrors() assert(not has("error"), chat()) end
"""


@pytest.fixture
def game(addons, run_client, tmp_path, monkeypatch):
    script = tmp_path / "publish_visuals.py"
    script.write_text(PUBLISH_SCRIPT)
    monkeypatch.setenv("WOWMCP_PUBLISH", f"{sys.executable} {script}")
    return lambda lua: run_client(PRELUDE + lua)


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------

NOW = int(time.time())
EVERY_TYPE = visual(
    "Every type",
    table(
        [col("Item", "item"), col("Name"), col("Count", "number"), col("Share", "percent"), col("Price", "money"),
         col("Change", "delta", unit="money"), col("Score", "delta", decimals=1), col("Seen", "time"),
         col("Left", "duration")],
        [[2318, "Tanned {item:2320}", 1234567, 12.5, 12005, -2500, 3.26, NOW, 3900],
         [4253, "Gloves", 0.5, 0.004, 900, 4000, -1, 1700000000, 59],
         ["{item:2589:Linen Cloth}", None, 12, 100, 0, 0, 0, None, 90061]],
    ),
    subtitle="Made up, just now",
)


def test_a_table_shows_each_type_of_value(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        show([==[""" + EVERY_TYPE + r"""]==])
        assert(open(), "a new visual opens the window")
        local f = win()
        assert(f.Title.text == "Every type" and f.Subtitle.text == "Made up, just now")
        assert(f.Footer.text:find("^Sent %d%d:%d%d$"), f.Footer.text)
        local names = {}
        for i = 1, 9 do names[i] = header(i).Text.text end
        assert(table.concat(names, ",") == "Item,Name,Count,Share,Price,Change,Score,Seen,Left", table.concat(names, ","))
        assert(not header(5).Arrow.shown, "not sorted")
        local list = rows()
        assert(#list == 3)

        local GOLD, SILVER, COPPER = "|TInterface\\MoneyFrame\\UI-GoldIcon:0:0:2:0|t", "|TInterface\\MoneyFrame\\UI-SilverIcon:0:0:2:0|t", "|TInterface\\MoneyFrame\\UI-CopperIcon:0:0:2:0|t"
        local one = cells(list[1])
        assert(one[1] == "|T134252:0|t |cffffffffLight Leather|r", one[1])
        assert(one[2] == "Tanned |T132889:0|t |cffffffffCoarse Thread|r", one[2])
        assert(one[3] == "1,234,567" and one[4] == "12.5%", one[3] .. " " .. one[4])
        assert(one[5] == "1" .. GOLD .. " 20" .. SILVER .. " 5" .. COPPER, one[5])
        assert(one[6] == "|cffff5050-25" .. SILVER .. "|r", one[6])
        assert(one[7] == "|cff33ff33+3.3|r", one[7])
        assert(one[8]:find("^%d%d:%d%d$"), "today: just the time " .. one[8])
        assert(one[9] == "1h 05m", one[9])
        local two = cells(list[2])
        assert(two[1] == "|T134400:0|t |cff1eff00Toughened Leather Gloves|r", two[1])
        assert(two[3] == "0.5" and two[4] == "0.004%", two[3] .. " " .. two[4])
        assert(two[5] == "9" .. SILVER and two[6] == "|cff33ff33+40" .. SILVER .. "|r", two[6])
        assert(two[7] == "|cffff5050-1.0|r" and two[8] == "2023-11-14" and two[9] == "59s", two[7] .. two[8] .. two[9])
        local three = cells(list[3])
        assert(plain(three[1]) == "Linen Cloth" and three[2] == "" and three[3] == "12" and three[4] == "100%")
        assert(three[5] == "0" .. COPPER and three[6] == "|cff808080" .. "0" .. COPPER .. "|r", three[6])
        assert(three[7] == "|cff808080" .. "0.0|r" and three[8] == "" and three[9] == "1d 1h", three[9])

        -- Numbers line up on the right, text and items on the left; so do their headers.
        assert(list[1].Cells[1].Text.justify == "LEFT" and list[1].Cells[2].Text.justify == "LEFT")
        for c = 3, 9 do
            assert(list[1].Cells[c].Text.justify == "RIGHT" and header(c).Text.justify == "RIGHT")
        end
        -- Every other row is shaded.
        assert(list[1].Back.color[4] == 0 and list[2].Back.color[4] > 0 and list[3].Back.color[4] == 0)
        noErrors()
    """)


def test_columns_fit_their_content_and_the_window_the_table(game):
    long = "A name much too long for any column of this table, it goes on and on"
    game(r"""
        client.LoadAddon()
        client.Wait()
        show([==[""" + visual("Fit", table([col("Name"), col("N", "number"), col("Fixed", width=60)],
                                         [[long, 1, "Short"], ["Bob", 22, long]])) + r"""]==])
        local f, h = win(), holder("table")
        -- Name: as wide as it may be (240); N: as its header and sort arrow; Fixed: as the AI said.
        assert(h.Headers[1].width == 240 and h.Headers[2].width == 30 and h.Headers[3].width == 60,
            h.Headers[1].width .. " " .. h.Headers[2].width .. " " .. h.Headers[3].width)
        local cell = rows()[1].Cells[1]
        assert(cell.width == 240 and cell.Text.width == 232)
        -- The window is the table and its margins.
        assert(f.width == 330 + 16 and h.width == 330, f.width)

        -- A short table in a window as narrow as it gets: the columns share the room, the fixed one keeps its width.
        show([==[""" + visual("Narrow", table([col("A"), col("B", width=40)], [["x", "y"]])) + r"""]==])
        h = holder("table")
        assert(win().width == 240 and h.Headers[1].width == 184 and h.Headers[2].width == 40, h.Headers[1].width)
        noErrors()
    """)


def test_highlights_and_totals(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        show([==[""" + visual("Gloves", table(
            [col("Item", "item"), col("Price", "money")],
            [[2318, 500], [4253, 9000], [2589, 300]],
            highlight=[{"row": 2, "label": "Best value"}, {"row": 3, "color": "blue", "label": "Equipped"}],
            totals=["Total", 9800])) + r"""]==])
        local list = rows()
        assert(not list[1].Tint.shown and not list[1].Mark.shown)
        assert(list[2].Tint.shown and list[2].Mark.shown)
        assert(list[2].Mark.color[1] == 1 and list[2].Mark.color[2] == 0.82, "gold")
        assert(list[3].Mark.color[3] == 1 and list[3].Mark.color[1] < 0.5, "blue")
        assert(list[2].Tint.color[4] < list[2].Mark.color[4], "a light tint, a strong bar")

        -- Totals: under the rows, set apart, in gold text, and not a row to sort.
        local h = holder("table")
        local totals = h.Totals
        assert(totals:IsShown() and totals.Line and totals.Line.color[2] == 0.82)
        assert(plain(totals.Cells[1].Text.text) == "Total" and plain(totals.Cells[2].Text.text) == "98")
        assert(totals.Cells[1].Text.template == "GameFontNormalSmall" and list[1].Cells[1].Text.template == "GameFontHighlightSmall")
        assert(at(totals)[5] < at(list[3])[5], "below the last row")
        h.Headers[2].OnMouseUp(h.Headers[2], "LeftButton")
        assert(column(2) == "90,5,3" and plain(h.Totals.Cells[2].Text.text) == "98", column(2))
        -- The highlight goes with its row.
        assert(rows()[1].Mark.shown and not rows()[2].Mark.shown and rows()[3].Mark.shown)

        -- Without totals, there's no totals row.
        show([==[""" + visual("Plain", table([col("A")], [["x"]])) + r"""]==])
        assert(not holder("table").Totals:IsShown())
        noErrors()
    """)


def test_hovering_cells(game):
    long = "Far too long for this narrow column, so it's cut"
    game(r"""
        client.LoadAddon()
        client.Wait()
        show([==[""" + visual("Hover", table(
            [col("Item", "item"), col("Note", width=60), col("N", "number")],
            [[2318, "{item:2320}", 1], [4253, "Short", 2], [2589, long, 3]],
            highlight=[{"row": 2, "label": "Best value"}])) + r"""]==])
        local list = rows()
        local item, note, number = list[1].Cells[1], list[1].Cells[2], list[1].Cells[3]

        -- An item: its own tooltip; the row lights up while the mouse is on it.
        item.OnEnter(item)
        assert(client.tooltip.item == 2318 and client.tooltip.owner == item and client.tooltip.shown)
        assert(list[1].Hover.shown)
        item.OnLeave(item)
        assert(not client.tooltip.shown and not list[1].Hover.shown)
        -- An item named in text: its tooltip too, with the text under it when it's cut short.
        note.OnEnter(note)
        assert(client.tooltip.item == 2320 and plain(client.tooltip.lines[2]) == "Coarse Thread", client.tooltip.lines[2])
        note.OnLeave(note)
        -- Nothing to tell: no tooltip.
        client.tooltip = { lines = {} }
        number.OnEnter(number)
        assert(client.tooltip.owner == nil and not client.tooltip.shown)
        number.OnLeave(number)

        -- Text that doesn't fit: all of it.
        local cut = list[3].Cells[2]
        assert(plain(cut.Text.text) == "Far too long for this narrow column, so it's cut" and cut.Text.width == 52)
        cut.OnEnter(cut)
        assert(client.tooltip.shown and client.tooltip.lines[1] == "Note", client.tooltip.lines[1])
        assert(client.tooltip.lines[2] == "Far too long for this narrow column, so it's cut")
        cut.OnLeave(cut)
        -- A highlighted row says why, on any cell; with an item's tooltip too.
        local why = list[2].Cells[3]
        why.OnEnter(why)
        assert(client.tooltip.lines[1] == "Best value" and #client.tooltip.lines == 1, table.concat(client.tooltip.lines, "|"))
        why.OnLeave(why)
        list[2].Cells[1].OnEnter(list[2].Cells[1])
        assert(client.tooltip.item == 4253 and client.tooltip.lines[2] == "Best value")
        list[2].Cells[1].OnLeave(list[2].Cells[1])

        -- A modified click on an item: what it does on any item (shift links it in chat).
        item.OnMouseUp(item, "LeftButton")
        note.OnMouseUp(note, "LeftButton")
        number.OnMouseUp(number, "LeftButton")
        assert(#client.linked == 2 and client.linked[1]:find("|Hitem:2318:", 1, true) and client.linked[2]:find("|Hitem:2320:", 1, true))
        noErrors()
    """)


SORTING = visual("Sorting", table(
    [col("Item", "item"), col("Price", "money"), col("Note")],
    [[4253, 900, "b"], [2318, 12005, None], [2589, 95, "a"], [2320, None, "c"]],
))


def test_a_click_on_a_header_sorts_by_its_values(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        show([==[""" + SORTING + r"""]==])
        local h = holder("table")
        local function click(n, button) h.Headers[n].OnMouseUp(h.Headers[n], button or "LeftButton") end
        local function items() return column(1) end
        assert(items() == "Toughened Leather Gloves,Light Leather,Linen Cloth,Coarse Thread", "as the AI gave it")

        -- Money by copper (as text, "95c" would come before "9s"), highest first, empty last.
        click(2)
        assert(items() == "Light Leather,Toughened Leather Gloves,Linen Cloth,Coarse Thread", items())
        assert(h.Headers[2].Arrow.shown and not h.Headers[1].Arrow.shown)
        assert(h.Headers[2].Arrow.texCoord[3] == 0, "descending: the arrow as it is")
        -- Again: the other way round; empty still last.
        click(2)
        assert(items() == "Linen Cloth,Toughened Leather Gloves,Light Leather,Coarse Thread", items())
        assert(h.Headers[2].Arrow.texCoord[3] == 1, "ascending: flipped, as the auction house does")
        -- Items by name, A to Z first.
        click(1)
        assert(items() == "Coarse Thread,Light Leather,Linen Cloth,Toughened Leather Gloves", items())
        click(1)
        assert(items() == "Toughened Leather Gloves,Linen Cloth,Light Leather,Coarse Thread", items())
        -- Text, with the empty cell last.
        click(3)
        assert(column(3) == "a,b,c,", column(3))

        -- The header says how it's sorted, and what a click does.
        local head = h.Headers[3]
        head.OnEnter(head)
        assert(client.tooltip.lines[2] == "Sorted A to Z.", client.tooltip.lines[2])
        click(3)
        assert(client.tooltip.lines[2] == "Sorted Z to A.", "the tooltip follows")
        head.OnLeave(head)
        h.Headers[2].OnEnter(h.Headers[2])
        assert(client.tooltip.lines[2] == "Click: sort by this column, highest first.", client.tooltip.lines[2])
        h.Headers[2].OnLeave(h.Headers[2])

        -- A right-click: back to how it came. A sort is kept over a /reload.
        click(2, "RightButton")
        assert(items() == "Toughened Leather Gloves,Light Leather,Linen Cloth,Coarse Thread")
        assert(not h.Headers[2].Arrow.shown)
        click(2)
        Reload()
        mcp("visuals show")
        assert(column(1) == "Light Leather,Toughened Leather Gloves,Linen Cloth,Coarse Thread", column(1))
        noErrors()
    """)


def test_a_visual_can_come_sorted(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        show([==[""" + visual("Cheapest first", {**json.loads(SORTING)["blocks"][0], "sort": {"column": "Price", "order": "asc"}}) + r"""]==])
        local h = holder("table")
        assert(column(1) == "Linen Cloth,Toughened Leather Gloves,Light Leather,Coarse Thread", column(1))
        assert(h.Headers[2].Arrow.shown)
        -- A click: the other way round, as it's sorted by it already. A right-click: back to how it came, sorted.
        h.Headers[2].OnMouseUp(h.Headers[2], "LeftButton")
        assert(column(1) == "Light Leather,Toughened Leather Gloves,Linen Cloth,Coarse Thread")
        h.Headers[1].OnMouseUp(h.Headers[1], "RightButton")
        assert(column(1) == "Linen Cloth,Toughened Leather Gloves,Light Leather,Coarse Thread")
        noErrors()
    """)


FORTY = visual("Forty rows", table([col("N", "number"), col("Name")], [[n, f"Row {n}"] for n in range(1, 41)]))


def test_a_long_table_scrolls_its_rows(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        show([==[""" + FORTY + r"""]==])
        local f, h = win(), holder("table")
        assert(#rows() == 15 and column(1) == "1,2,3,4,5,6,7,8,9,10,11,12,13,14,15")
        assert(status() == "Rows 1-15 of 40" and h.Bar:IsShown())
        assert(h.Bar.height == 15 * 16 and h.Bar.Thumb.height == 90, h.Bar.Thumb.height)

        -- The wheel over the table scrolls its rows, three at a time; not past either end.
        local cell = rows()[1].Cells[2]
        cell.OnMouseWheel(cell, -1)
        assert(status() == "Rows 4-18 of 40" and column(1):sub(1, 2) == "4,", column(1))
        assert(at(h.Bar.Thumb)[5] == -18, at(h.Bar.Thumb)[5])
        for _ = 1, 20 do cell.OnMouseWheel(cell, -1) end
        assert(status() == "Rows 26-40 of 40" and at(h.Bar.Thumb)[5] == -150)
        for _ = 1, 20 do h.Bar.OnMouseWheel(h.Bar, 1) end
        assert(status() == "Rows 1-15 of 40")
        -- The stack fits the window: it doesn't scroll.
        assert(not f.Bar:IsShown() and f.Scroll.verticalScroll == 0)

        -- Dragging the bar: the rows where the cursor is. (The stub puts every frame's top at y = 500.)
        client.Mouse(0, 305, true)
        h.Bar.OnMouseDown(h.Bar, "LeftButton")
        assert(status() == "Rows 26-40 of 40", status())
        client.Mouse(0, 401, true)
        client.Tick()
        assert(status() == "Rows 10-24 of 40", status())
        client.Mouse(0, 500, false)
        client.Tick()
        assert(h.Bar.OnUpdate == nil and status() == "Rows 10-24 of 40", "let go: the drag is over")

        -- Sorting starts from the top again.
        h.Headers[1].OnMouseUp(h.Headers[1], "LeftButton")
        assert(status() == "Rows 1-15 of 40" and column(1):sub(1, 6) == "40,39,", column(1))
        noErrors()
    """)


def test_the_stack_scrolls_when_its_taller_than_the_window(game):
    items = [{"text": f"Step {n}", "value": n * 100} for n in range(1, 51)]
    game(r"""
        client.LoadAddon()
        client.Wait()
        show([==[""" + visual("Long", {"type": "list", "items": items, "value_type": "money", "heading": "Steps"},
                            {"type": "text", "text": "The end."}) + r"""]==])
        local f = win()
        -- 16 for the heading, 50 rows of 16, 10 between the blocks, the text's line: 838, of which 560 show.
        assert(f.Canvas.height == 838 and f.Scroll.height == 560, f.Canvas.height .. " " .. f.Scroll.height)
        assert(f.Bar:IsShown() and f.Scroll.verticalScroll == 0)
        assert(f.width == f.content + 16 + 8 + 4, "the bar has room of its own")

        -- The wheel anywhere on it scrolls it, three rows at a time, up to its end.
        f.OnMouseWheel(f, -1)
        assert(f.Scroll.verticalScroll == 48)
        local row = holder("list").Rows[1]
        row.OnMouseWheel(row, -1)
        assert(f.Scroll.verticalScroll == 96)
        for _ = 1, 20 do f.Scroll.OnMouseWheel(f.Scroll, -1) end
        assert(f.Scroll.verticalScroll == 278 and at(f.Bar.Thumb)[5] == -(560 - f.Bar.Thumb.height))
        -- Its bar: dragged to the top.
        client.Mouse(0, 600, true)
        f.Bar.OnMouseDown(f.Bar, "LeftButton")
        assert(f.Scroll.verticalScroll == 0)
        f.Bar.OnMouseUp(f.Bar, "LeftButton")
        noErrors()
    """)


def test_lists_and_texts(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        show([==[""" + visual(
            "Craft",
            {"type": "list", "heading": "Materials", "value_type": "money", "items": [
                {"text": "2x {item:2320}", "value": 40}, {"text": "1x Salt", "value": "1s 50c"},
                {"text": "Your {item:2318}", "highlight": "blue"}]},
            {"type": "list", "style": "numbered", "value_type": "delta", "unit": "percent", "decimals": 1, "items": [
                {"text": "Mongoose", "value": 4.26}, {"text": "Crusader", "value": -2}, {"text": "Fiery Weapon"},
                {"text": "A name so long that it can't fit in the room this list has, not even close, it's cut short"}]},
            {"type": "text", "text": "Buy the {item:2320} at the vendor.\nThe rest from the auction house."},
        ) + r"""]==])
        local f = win()
        local bullets, numbered, text = holder("list", 1), holder("list", 2), holder("text")
        -- Bullets, the items' text on the left and their values on the right.
        local one = bullets.Rows[1]
        assert(one.Bullet.shown and not one.Number.shown)
        assert(one.Text.text == "2x |T132889:0|t |cffffffffCoarse Thread|r", one.Text.text)
        assert(plain(one.Value.text) == "40" and one.Value.justify == "RIGHT" and one.Value.shown)
        assert(plain(bullets.Rows[2].Value.text) == "150", bullets.Rows[2].Value.text)
        assert(bullets.Rows[3].Value.text == "" and bullets.Rows[3].Mark.shown and bullets.Rows[3].Mark.color[3] == 1)
        assert(not one.Mark.shown)
        -- Numbers, and values as their type shows them.
        local first = numbered.Rows[1]
        assert(first.Number.shown and not first.Bullet.shown and first.Number.text == "1." and numbered.Rows[4].Number.text == "4.")
        assert(first.Value.text == "|cff33ff33+4.3%|r" and numbered.Rows[2].Value.text == "|cffff5050-2.0%|r", first.Value.text)
        -- A text: wrapped to the window, its items shown as items, its lines kept.
        assert(text.Text.text == "Buy the |T132889:0|t |cffffffffCoarse Thread|r at the vendor.\nThe rest from the auction house.", text.Text.text)
        assert(text.Text.width == f.content - 8 and text.Text.wordWrap)
        -- Headings: over the blocks that have one.
        assert(f.Headings[1]:IsShown() and f.Headings[1].text == "Materials" and not f.Headings[2]:IsShown())

        -- Hovering an item: its tooltip; a line cut short: all of it.
        one.OnEnter(one)
        assert(client.tooltip.item == 2320 and one.Hover.shown)
        one.OnLeave(one)
        local long = numbered.Rows[4]
        long.OnEnter(long)
        assert(client.tooltip.lines[1]:find("not even close, it's cut short", 1, true), client.tooltip.lines[1])
        long.OnLeave(long)
        one.OnMouseUp(one, "LeftButton")
        assert(client.linked[1]:find("|Hitem:2320:", 1, true))
        noErrors()
    """)


def test_blocks_stack_in_one_window(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        show([==[""" + visual(
            "Mixed",
            {"type": "text", "text": "First the text."},
            table([col("Item", "item"), col("Price", "money")], [[2318, 500]], heading="Prices"),
            {"type": "list", "items": [{"text": "One"}, {"text": "Two"}]},
            table([col("A")], [["second table"]]),
        ) + r"""]==])
        local f = win()
        local text, first, list, second = holder("text"), holder("table", 1), holder("list"), holder("table", 2)
        -- Top to bottom, in the order sent, each under its heading.
        local function y(frame) return -at(frame)[5] end
        assert(y(text) == 0 and y(first) == y(text) + text.height + 10 + 16, y(first))
        assert(y(f.Headings[2]) == y(text) + text.height + 10 and f.Headings[2].text == "Prices")
        assert(y(list) == y(first) + first.height + 10 and y(second) == y(list) + list.height + 10)
        assert(f.Canvas.height == y(second) + second.height and not f.Bar:IsShown())
        assert(plain(rows(second)[1].Cells[1].Text.text) == "second table" and plain(rows(first)[1].Cells[2].Text.text) == "5")
        -- All as wide as the window's content.
        for _, h in ipairs({ text, first, list, second }) do assert(h.width == f.content) end

        -- The next visual, with fewer blocks: the frames of the ones it doesn't have go.
        show([==[""" + visual("Just a list", {"type": "list", "items": [{"text": "Only"}]}) + r"""]==])
        assert(holder("list"):IsShown() and not holder("table", 1):IsShown() and not holder("text"):IsShown())
        assert(not f.Headings[2]:IsShown())
        noErrors()
    """)


def test_opens_for_a_new_visual_and_not_again_after_a_reload(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        show([==[""" + visual("First", table([col("A")], [["x"]])) + r"""]==])
        assert(open())
        -- Closed: an update for something else doesn't bring it back, nor does a /reload.
        win().Close.OnClick(win().Close)
        assert(not open() and last():find("Hidden. /mcp visuals show brings it back.", 1, true))
        show([[{"message": "Something else"}]])
        assert(not open())
        Reload()
        client.Wait()
        assert(not open())
        -- Open at the /reload: open again after it, on the same visual.
        mcp("visuals show")
        assert(open() and win().Title.text == "First")
        Reload()
        client.Wait()
        assert(open() and win().Title.text == "First")
        -- A new one opens it, after a /reload too.
        win().Close.OnClick(win().Close)
        show([==[""" + visual("Second", table([col("A")], [["y"]])) + r"""]==])
        assert(open() and win().Title.text == "Second")
        assert(not has("error"), chat())
    """)


def test_a_reset_server_starts_its_ids_over(game):
    game(r"""
        WowMCPDB = { visualSeen = 500, visualsCleared = 400 }
        client.LoadAddon()
        client.Wait()
        show([==[""" + visual("After a reset", table([col("A")], [["x"]])) + r"""]==])
        assert(open() and win().Title.text == "After a reset" and WowMCPDB.visualSeen == 1)
        noErrors()
    """)


def test_flipping_through_the_last_visuals(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        for _, name in ipairs({ "One", "Two", "Three" }) do
            show('{"title": "' .. name .. '", "blocks": [{"type": "text", "text": "' .. name .. '"}]}')
        end
        local f = win()
        assert(f.Title.text == "Three" and f.Count.text == "3/3" and f.Prev.shown ~= false and f.Next:IsShown())
        assert(f.Prev.enabled and not f.Next.enabled)
        f.Prev.OnEnter(f.Prev)
        assert(client.tooltip.lines[1] == "Previous" and client.tooltip.lines[2] == "Two")
        f.Prev.OnLeave(f.Prev)
        f.Prev.OnClick(f.Prev)
        assert(f.Title.text == "Two" and f.Count.text == "2/3" and f.Next.enabled)
        f.Prev.OnClick(f.Prev)
        assert(f.Title.text == "One" and not f.Prev.enabled)
        f.Prev.OnClick(f.Prev)
        assert(f.Title.text == "One")
        f.Next.OnClick(f.Next)
        assert(f.Title.text == "Two" and holder("text").Text.text == "Two")
        -- What's on show is kept over a /reload.
        Reload()
        client.Wait()
        assert(win().Title.text == "Two")
        mcp("visuals 3")
        assert(win().Title.text == "Three")

        -- The server keeps the last 5; with one visual there's nothing to flip to.
        for _, name in ipairs({ "Four", "Five", "Six" }) do
            show('{"title": "' .. name .. '", "blocks": [{"type": "text", "text": "' .. name .. '"}]}')
        end
        assert(win().Count.text == "5/5")
        mcp("visuals 1")
        assert(win().Title.text == "Two")
        show([[{"clear": null}]])
        show([==[""" + visual("Alone", {"type": "text", "text": "x"}) + r"""]==])
        assert(win().Title.text == "Alone" and not win().Count:IsShown() and not win().Prev:IsShown())
        noErrors()
    """)


def test_commands(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        mcp("visuals")
        assert(last():find("Nothing to show. Ask your AI for a table!", 1, true))
        show([==[""" + visual("Gear", table([col("A")], [["x"]])) + r"""]==])
        show([==[""" + visual("Profit", {"type": "list", "items": [{"text": "x"}]}) + r"""]==])
        mcp("visuals")
        assert(has("1. |cffffffffGear|r (table, sent ") and has("2. |cffffffffProfit|r (list, sent "), chat())
        assert(last():find(" - showing$"), last())
        mcp("visuals hide")
        assert(not open() and last():find("Hidden", 1, true))
        mcp("visuals hide")
        assert(last():find("The window isn't open", 1, true))
        mcp("visuals show")
        assert(open() and win().Title.text == "Profit")
        mcp("visuals 1")
        assert(win().Title.text == "Gear")
        mcp("visuals 7")
        assert(last():find("There's no 7: there are 2.", 1, true), last())
        mcp("visuals what")
        assert(last():find("/mcp visuals [show, hide, clear, <n>]", 1, true))
        -- /mcp table does the same; the list of commands only names /mcp visuals.
        mcp("table hide")
        assert(not open())
        mcp("table 2")
        assert(open() and win().Title.text == "Profit")
        mcp("")
        assert(has("/mcp visuals [show") and not has("/mcp table "), chat())

        -- Clear: gone, also from later updates; new ones still show.
        mcp("visuals clear")
        assert(not open() and last():find("Cleared.", 1, true))
        show([[{"message": "Something else"}]])
        mcp("visuals show")
        assert(not open() and last():find("Nothing to show", 1, true))
        show([==[""" + visual("New", {"type": "text", "text": "x"}) + r"""]==])
        assert(open() and win().Title.text == "New" and not win().Count:IsShown())
        noErrors()
    """)


def test_the_ai_clears_its_visuals(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        show([==[""" + visual("One", {"type": "text", "text": "1"}) + r"""]==])
        show([==[""" + visual("Two", {"type": "text", "text": "2"}) + r"""]==])
        local two = WowMCPDB.visualSeen
        -- One: the other stays, on show.
        show('{"clear": ' .. two .. '}')
        assert(open() and win().Title.text == "One" and not win().Count:IsShown())
        show([[{"clear": null}]])
        assert(not open() and last():find("Your AI cleared what it showed.", 1, true))
        noErrors()
    """)


def test_the_window_keeps_its_place_and_size(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        show([==[""" + FORTY + r"""]==])
        local f = win()
        assert(at(f)[1] == "TOP" and at(f)[5] == -140, "first: at the top of the screen")
        -- Dragged (by the window or by a row) and let go: kept.
        f.OnDragStart(f)
        assert(f.moving)
        f:ClearAllPoints()
        f:SetPoint("TOPLEFT", UIParent, "BOTTOMLEFT", 300, 700)
        f.OnDragStop(f)
        assert(not f.moving and WowMCPDB.visualWindow.point == "TOPLEFT" and WowMCPDB.visualWindow.x == 300)
        local cell = rows()[1].Cells[1]
        cell.OnDragStart(cell)
        assert(f.moving)
        cell.OnDragStop(cell)

        -- The corner: 60 px to the right is 60 wider, 64 px up four rows fewer.
        local width = f.content
        client.Mouse(500, 400, true)
        f.Grip.OnMouseDown(f.Grip, "RightButton")
        assert(f.Grip.OnUpdate == nil)
        f.Grip.OnMouseDown(f.Grip, "LeftButton")
        assert(at(f)[1] == "TOPLEFT" and at(f)[3] == "BOTTOMLEFT" and f.Grip.Corner.texture:find("Down"))
        client.Mouse(560, 464, true)
        client.Tick()
        assert(f.content == width + 60 and #rows() == 11, f.content .. " " .. #rows())
        client.Mouse(560, 464, false)
        client.Tick()
        assert(f.Grip.OnUpdate == nil and f.Grip.Corner.texture:find("Up"))
        assert(WowMCPDB.visualWindow.rows == 11 and WowMCPDB.visualViews[WowMCPDB.visualSeen].width == width + 60)

        -- Kept over a /reload.
        Reload()
        client.Wait()
        f = win()
        assert(at(f)[1] == "TOPLEFT" and at(f)[4] == 24 and at(f)[5] == 500, "where the corner drag left it")
        assert(f.content == width + 60 and #rows() == 11)
        -- Another visual fits its own content; tables in it show the rows the player chose.
        show([==[""" + visual("Other", table([col("N", "number")], [[n] for n in range(1, 31)])) + r"""]==])
        assert(win().content == 224 and #rows() == 11, win().content)
        -- Just a click on the corner changes nothing.
        f.Grip.OnMouseDown(f.Grip, "LeftButton")
        client.Tick()
        f.Grip.OnMouseUp(f.Grip, "LeftButton")
        assert(WowMCPDB.visualWindow.rows == 11 and WowMCPDB.visualViews[WowMCPDB.visualSeen] ~= nil
            and WowMCPDB.visualViews[WowMCPDB.visualSeen].width == nil)
        noErrors()
    """)


def test_a_block_this_addon_doesnt_know(game):
    raw = {"title": "From the future", "at": NOW, "blocks": [
        {"type": "chart", "points": [1, 2, 3]}, {"type": "text", "text": "Known"}, {"type": "list"}]}
    game(r"""
        client.LoadAddon()
        client.Wait()
        show([==[""" + lua_json({"raw": raw}) + r"""]==])
        assert(open() and holder("text", 1).Text.text == "(A chart the WowMCP addon can't show yet: run wow-mcp-install, then /reload.)")
        assert(holder("text", 2).Text.text == "Known", "a list without items isn't shown")
        assert(WowMCPVisualFrame.Holders.list == nil)
        noErrors()
    """)


def test_items_still_loading_show_when_they_arrive(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        client.Publish([==[""" + visual("Loading", table([col("Item", "item")], [[2319], [2318]], sort={"column": 1})) + r"""]==])
        for _ = 1, 6 do
            if open() then break end
            client.Tick()
        end
        -- Medium Leather takes half a second to load: by its number until then, and sorted by that.
        assert(column(1) == "item 2319,Light Leather", column(1))
        client.Wait(3)
        assert(column(1) == "Light Leather,Medium Leather", column(1))
        assert(rows()[2].Cells[1].Text.text:find("|cffffffffMedium Leather|r", 1, true))
        noErrors()
    """)


def test_visuals_live(live_game, tmp_path):
    """visualize end to end, with the player's two kinds of question: comparing gear, and auction house profits."""
    dump = tmp_path / "window.txt"
    setup = f"""
        local out = [[{dump}]]
        C_Timer.NewTicker(0.5, function()
            local f = WowMCPVisualFrame
            if not (f and f:IsShown()) then return end
            local lines = {{ f.Title.text }}
            for _, h in ipairs(f.Holders.table or {{}}) do
                if h:IsShown() then
                    for _, row in ipairs(h.Rows) do
                        if row:IsShown() then
                            local cells = {{}}
                            for _, c in ipairs(row.Cells) do if c:IsShown() then cells[#cells + 1] = c.Text.text end end
                            lines[#lines + 1] = table.concat(cells, " | ")
                        end
                    end
                end
            end
            local file = io.open(out, "w")
            file:write(table.concat(lines, "\\n"))
            file:close()
        end)
    """

    def wait_for(text):
        for _ in range(40):
            if dump.exists() and text in dump.read_text():
                return dump.read_text()
            time.sleep(0.1)
        pytest.fail(f"not shown: {text!r} in {dump.read_text() if dump.exists() else None!r}")

    with live_game(setup):
        gear = visuals.visualize(
            "Hands: what you have and what's better",
            [{"type": "table", "columns": [
                {"name": "Item", "type": "item"}, {"name": "Armor", "type": "number"}, {"name": "Agility", "type": "number"},
                {"name": "Score", "type": "delta", "decimals": 1}, {"name": "Price", "type": "money"}],
              "rows": [[4253, 49, 6, 8.5, 21500], [2302, 24, 0, 0, None]],
              "highlight": [{"row": 1, "label": "Best upgrade"}, {"row": 2, "color": "blue", "label": "Equipped"}],
              "sort": {"column": "Score"}}],
            subtitle="Auction house scan, 12 min ago",
        )
        assert gear.startswith("Visual #") and "a table (2 rows x 5 columns, sorted by Score (desc), 2 highlighted)" in gear
        assert "isn't running" not in gear
        shown = wait_for("Hands: what you have")
        assert "Toughened Leather Gloves" in shown and "|cff33ff33+8.5|r" in shown

        profit = visuals.visualize(
            "Leatherworking: profit per craft",
            [{"type": "table", "columns": [
                {"name": "Craft", "type": "item"}, {"name": "Materials", "type": "money"}, {"name": "Sells for", "type": "money"},
                {"name": "Profit", "type": "delta", "unit": "money"}],
              "rows": [[2302, 1890, 4500, 2610], [4243, 9600, 8000, -1600]],
              "totals": ["Both", 11490, 12500, 1010], "sort": {"column": "Profit"}},
             {"type": "text", "text": "Craft the {item:2302}; the {item:4243} sells at a loss."}],
            subtitle="Auction house scan, 20 min ago",
        )
        assert "a table (2 rows x 4 columns, sorted by Profit (desc), with totals) and a text" in profit
        shown = wait_for("Leatherworking: profit per craft")
        assert "Handstitched Leather Pants" in shown.splitlines()[1] and "|cffff5050-16" in shown.splitlines()[2]


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


def state(addons):
    return Transport(addons).load_state()


def test_visualize_payload(addons):
    result = visuals.visualize(
        "{item:4253} or the pants?",
        [
            {"type": "table", "heading": "Gear", "columns": [
                {"name": "Item", "type": "item"}, {"name": "Price", "type": "money"},
                {"name": "Change", "type": "delta", "unit": "money", "align": "center"}, {"name": "Drop", "type": "percent", "decimals": 1},
                {"name": "Note"}],
             "rows": [["{item:4253:Toughened Leather Gloves}", "1g 20s 5c", -2500.4, "12.5%", "Line\nbreak\there"],
                      [2302, 900.6, None, 0.004, 12.0]],
             "totals": ["Total", 12905, 0, None, None],
             "highlight": [{"row": 1, "label": "Best"}], "sort": {"column": "price"}},
            {"type": "list", "style": "numbered", "value_type": "money", "items": [
                {"text": "Light Leather x4", "value": 2400, "highlight": "green"}, {"text": "Thread"}]},
            {"type": "text", "text": "  Buy   the gloves.\n\nThen equip them. "},
        ],
        subtitle="Auction house scan, 12 min ago",
    )
    assert result.startswith("Visual #1 sent to the game: '{item:4253} or the pants?', with a table (2 rows x 5 columns, "
                             "sorted by Price (desc), 1 highlighted, with totals), a numbered list (2 items) and a text.")
    assert "flip back through the last 5" in result and "isn't running, so it will show when the player next logs in" in result
    sent = state(addons)["visuals"]
    assert sent["id"] == 1 and len(sent["list"]) == 1
    entry = sent["list"][0]
    assert entry["id"] == 1 and entry["title"] == "{item:4253} or the pants?" and abs(entry["at"] - time.time()) < 5
    assert entry["subtitle"] == "Auction house scan, 12 min ago"
    table_block, list_block, text_block = entry["blocks"]
    assert table_block["type"] == "table" and table_block["heading"] == "Gear"
    assert [c["align"] for c in table_block["columns"]] == ["left", "right", "center", "right", "left"]
    assert table_block["columns"][2]["unit"] == "money" and table_block["columns"][1]["unit"] is None
    assert table_block["columns"][3]["decimals"] == 1
    # Rows: one string each, cells separated by line breaks; items as id:name, money in whole copper.
    assert table_block["rows"] == ["4253:Toughened Leather Gloves\n12005\n-2500\n12.5\nLine break here", "2302\n901\n\n0.004\n12"]
    assert table_block["totals"] == "Total\n12905\n0\n\n"
    assert table_block["highlight"] == [{"row": 1, "label": "Best", "color": "gold"}]
    assert table_block["sort"] == {"column": 2, "dir": "desc"}
    assert list_block == {"type": "list", "heading": None, "style": "numbered",
                          "value": {"type": "money", "unit": None, "decimals": None},
                          "items": [{"text": "Light Leather x4", "value": "2400", "mark": "green"},
                                    {"text": "Thread", "value": None, "mark": None}]}
    assert text_block == {"type": "text", "heading": None, "text": "Buy the gloves.\n\nThen equip them."}


@pytest.mark.parametrize("blocks, error", [
    ([], "blocks: List should have at least 1 item"),
    ([{"type": "text", "text": "x"}] * 9, "blocks: List should have at most 8 items"),
    ([{"type": "chart"}], "does not match any of the expected tags: 'table', 'list', 'text'"),
    ([table([col("A"), col("B")], [["x", "y"], ["z"]])], "row 2 has 1 cells, but there are 2 columns (use null for an empty cell)"),
    ([table([col(f"C{n}") for n in range(11)], [["x"] * 11])], "columns: List should have at most 10 items"),
    ([table([col("A")], [["x"]] * 201)], "rows: List should have at most 200 items"),
    ([table([col("Price", "money")], [["a lot"]])], "row 1, column 'Price': 'a lot' isn't copper as a number, e.g. 12005 for 1g 20s 5c"),
    ([table([col("Profit", "delta", unit="money")], [["more"]])], "isn't copper as a number, e.g. -2500 for -25s"),
    ([table([col("Item", "item")], [[0]])], "row 1, column 'Item': 0 isn't an item: give an item ID, or {item:<id>:<name>}"),
    ([table([col("Item", "item")], [["Light Leather"]])], "'Light Leather' isn't an item"),
    ([table([col("N", "number")], [[True]])], "Input should be a valid"),
    ([table([col("When", "time")], [["yesterday"]])], "isn't a unix time in seconds"),
    ([table([col("A")], [["x"]], totals=["a", "b"])], "totals has 2 cells, but there are 1 columns"),
    ([table([col("A")], [["x"]], highlight=[{"row": 3}])], "highlight row 3 isn't there: there are 1 rows"),
    ([table([col("A")], [["x"]], highlight=[{"row": 1}, {"row": 1}])], "row 1 is highlighted twice"),
    ([table([col("A")], [["x"]], sort={"column": "B"})], "sort column 'B' isn't one of the columns ('A'; or 1-1)"),
    ([table([col("A", "colour")], [["x"]])], "Input should be 'text', 'number'"),
    ([{"type": "list", "items": [{"text": "x", "value": "lots"}], "value_type": "percent"}], "item 1: 'lots' isn't a number in percent"),
    ([{"type": "list", "items": []}], "items: List should have at least 1 item"),
    ([{"type": "text", "text": "x" * 1001}], "text: String should have at most 1000 characters"),
])
def test_what_a_visual_must_be(addons, blocks, error):
    with pytest.raises(ToolError) as caught:
        visuals.visualize("Checked", blocks)
    assert str(caught.value).startswith("Nothing was sent:") and error in str(caught.value), str(caught.value)
    assert "visuals" not in state(addons)


def test_many_problems_are_listed_together():
    with pytest.raises(ValidationError) as caught:
        visuals.Visual(title="x", blocks=[table([col("N", "number")], [["a"]] * 20)])
    message = str(caught.value)
    assert "row 1, column 'N'" in message and "row 12, column 'N'" in message and "(and 8 more)" in message


def test_values_written_the_way_people_write_them():
    block = visuals.TableBlock.model_validate(table(
        [col("Price", "money"), col("Change", "delta", unit="money"), col("Share", "percent"), col("Item", "item"),
         col("When", "time"), col("Text")],
        [["1g 20s 5c", "-25s", "12.5%", "{item:2318}", "2026-11-11T12:00:00+00:00", 3.0],
         ["15s", "+1g", " 7 ", "  2589 ", 1794398400.4, "{item:2318:Light Leather} x2"]]))
    assert block.content()["rows"] == [
        "12005\n-2500\n12.5\n2318\n1794398400\n3",
        "1500\n10000\n7\n2589\n1794398400\n{item:2318:Light Leather} x2",
    ]


def test_the_last_five_are_kept(addons):
    for n in range(1, 8):
        visuals.visualize(f"Visual {n}", [{"type": "text", "text": str(n)}])
    sent = state(addons)["visuals"]
    assert sent["id"] == 7 and [v["title"] for v in sent["list"]] == [f"Visual {n}" for n in range(3, 8)]
    assert [v["id"] for v in sent["list"]] == [3, 4, 5, 6, 7]


def test_size(addons, capsys):
    """A 200-row table with 10 columns fits one update; the kept ones are cut to a budget, oldest first."""
    big = table(
        [col("Item", "item"), col("Name"), col("Price", "money"), col("Profit", "delta", unit="money"),
         col("Drop", "percent"), col("Level", "number"), col("Zone"), col("Sold", "number"), col("Seen", "time"),
         col("Left", "duration")],
        [[2318 + n, f"Some item name {n}", 123456 + n, -45678 + n * 7, 12.5, 30, "Stranglethorn Vale", n * 13,
          NOW - n * 60, 3600 + n] for n in range(200)],
    )
    entry = visuals.Visual(title="Big", blocks=[big]).encode(NOW)
    taken = visuals.size(entry)
    with capsys.disabled():
        print(f"\n200 x 10 table: {taken} bytes in the payload ({len(json.dumps(entry))} as JSON)")
    assert 15_000 < taken < visuals.MAX_SIZE

    first = visuals.visualize("Big 1", [big])
    assert "dropped" not in first
    visuals.visualize("Big 2", [big])
    third = visuals.visualize("Big 3", [big])
    assert "Older visuals were dropped to keep the game's updates small: #1." in third
    sent = state(addons)["visuals"]
    assert [v["title"] for v in sent["list"]] == ["Big 2", "Big 3"]
    assert visuals.size(sent["list"]) <= visuals.MAX_KEPT
    # The whole payload, as written into every slot.
    inbox = (addons / "WowMCP" / "Inbox.lua").read_bytes()
    assert len(inbox) < visuals.MAX_KEPT + 2_000

    too_big = table([col(f"C{n}") for n in range(10)], [["x" * 200] * 10] * 30)
    with pytest.raises(ToolError, match=r"Nothing was sent: it takes \d+ KB, and at most 40 KB fit"):
        visuals.visualize("Too big", [too_big])


def test_clear_visuals(addons):
    assert visuals.clear_visuals() == "There are no visuals in the game."
    assert "visuals" not in state(addons), "nothing sent"
    for n in range(1, 4):
        visuals.visualize(f"Visual {n}", [{"type": "text", "text": str(n)}])
    with pytest.raises(ToolError, match=r"There's no visual #9. The game has: #1 'Visual 1', #2 'Visual 2', #3 'Visual 3'."):
        visuals.clear_visuals(9)
    assert visuals.clear_visuals(2).startswith("Removed visual #2 from the game.")
    assert [v["id"] for v in state(addons)["visuals"]["list"]] == [1, 3]
    assert visuals.clear_visuals().startswith("Cleared 2 visuals from the game.")
    assert state(addons)["visuals"] == {"id": 5, "list": []}


def test_the_block_registry():
    """Every block type is a model with its `type`, picked by it, and encodes itself for the game."""
    names = [b.model_fields["type"].annotation.__args__[0] for b in visuals.BLOCK_TYPES]
    assert names == ["table", "list", "text"]
    adapter = TypeAdapter(visuals.Block)
    for name, sample in zip(names, [table([col("A")], [["x"]]), {"type": "list", "items": [{"text": "x"}]},
                                    {"type": "text", "text": "x"}]):
        block = adapter.validate_python({**sample, "heading": "H"})
        assert type(block).__name__.lower() == f"{name}block"
        assert block.encode()["type"] == name and block.encode()["heading"] == "H" and block.describe()
    schema = adapter.json_schema()
    assert sorted(schema["discriminator"]["mapping"]) == ["list", "table", "text"]


def test_guidance_and_bridge():
    from wow_mcp import app, bridge

    text = next(t for order, t in app.GUIDANCE if "visualize" in t)
    assert "'table'" in text and "'list'" in text and "'text'" in text and "details are in the window" in text
    assert "visualize" in bridge.SYSTEM_PROMPT and bridge.tool_step("mcp__wow__visualize") == "making a table or list"


def test_visualize_tells_when_the_game_is_elsewhere(addons, monkeypatch):
    monkeypatch.setattr(wow_game, "running_folders", lambda proc=None: {"_forever_"})
    result = visuals.visualize("Shown", [{"type": "text", "text": "x"}])
    assert result.endswith("flip back through the last 5.")
