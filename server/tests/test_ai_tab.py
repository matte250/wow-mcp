"""The AI conversation's own chat tab (Chat.lua, Messages.lua): made once, when first needed; the
question, "Thinking…" and the answer there, per /mcp aitab; typing there asks the AI. The picker on
the tab, /ai model and /ai thinking pick which model answers and how hard it thinks."""

import sys

import pytest

# How the Lua test scripts publish, through the real tools: answers to /ai are replies, the bridge's notes
# about a question (ack, progress) statuses, which go over the bell.
PUBLISH_SCRIPT = """
import json, sys
from wow_mcp.tools import messages
args = json.loads(sys.argv[1])
question = (args["session"], args["post"]) if "post" in args else None
if "status" in args:
    messages.publish_status(args["status"], args.get("text", ""), question=question, quiet=args.get("quiet"),
                            tokens=args.get("tokens"))
else:
    messages.publish_message(args["text"], args.get("style", "chat"), args.get("sender", "Claude"),
                             reply=args.get("reply", False), default_model=args.get("defaultModel"),
                             default_effort=args.get("defaultEffort"), question=question, took=args.get("took"),
                             tokens=args.get("tokens"))
"""

HELPERS = """
-- /reload: Lua state, loaded slots and frames reset; saved variables and chat window settings don't.
local function Reload()
    for _, t in ipairs({ client.loaded, client.tickers, client.timers, client.frames, client.chat, client.sounds, client.fontStrings }) do
        wipe(t)
    end
    return client.LoadAddon()
end

-- Past the first seconds after login, when the tab may be made.
local function Settle() client.Wait(12) end

local function Has(lines, text)
    for _, line in ipairs(lines) do
        if line:find(text, 1, true) then return true end
    end
    return false
end

local function Count(lines, text)
    local n = 0
    for _, line in ipairs(lines) do
        if line:find(text, 1, true) then n = n + 1 end
    end
    return n
end

-- The open chat window named AI, and its number.
local function Tab()
    for i = 1, 10 do
        local name, _, _, _, _, _, shown = GetChatWindowInfo(i)
        if name == "AI" and (shown or _G["ChatFrame" .. i].isDocked) then return _G["ChatFrame" .. i], i end
    end
end

-- What the addon posts to the bridge.
local function Posts(ns)
    local posted = {}
    ns.Stream.Post = function(value) posted[#posted + 1] = value end
    return posted
end

local function Answer(text)
    client.Publish('{"text":"' .. text .. '","reply":true}')
    client.Wait()
end

-- An answer as the bridge sends it: signed with the model, and saying which one it uses by default (and
-- how hard it thinks, if given).
local function AnswerFrom(sender, text, defaultModel, defaultEffort)
    client.Publish('{"text":"' .. text .. '","reply":true,"sender":"' .. sender .. '","defaultModel":"' .. defaultModel .. '"'
        .. (defaultEffort and ',"defaultEffort":"' .. defaultEffort .. '"' or "") .. '}')
    client.Wait()
end

-- The texts of the picker's menu, with the picked ones marked "*", titles as "# Title" and dividers "--".
local function Menu(picker)
    local texts = {}
    for _, element in ipairs(client.OpenMenu(picker)) do
        texts[#texts + 1] = element.kind == "title" and "# " .. element.text or element.kind == "divider" and "--"
            or (element:IsSelected() and "*" or "") .. element.text
    end
    picker:CloseMenu()
    return texts
end

-- What a tooltip function says, line by line.
local function TooltipLines(tooltipFunc)
    local said = {}
    local tooltip = setmetatable({}, { __index = function(_, name)
        return function(_, text) if name == "SetText" or name == "AddLine" then said[#said + 1] = text end end
    end })
    tooltipFunc(tooltip)
    return said
end

-- The "AI:" label over the chat box's header.
local function Labelled(box)
    for _, fs in ipairs(client.fontStrings) do
        if fs.text == "|cff33ff99AI:|r" then
            return fs.parent:IsShown() and box.header:GetAlpha() == 0
        end
    end
    return false
end

local YOU, STATUS = "|cff8fc8ff[You]|r ", "|cffababab"
local DOWN, STOPPED = "No answer from wow-mcp-bridge. Is it running?", "The AI seems to have stopped responding."

-- Whether a line is the status line saying the AI is thinking.
local function Thinking(line)
    return line ~= nil and line:find(STATUS .. "Thinking…", 1, true) == 1
end

-- What the addon posts to the bridge, named as Stream.Post names them: post ids from 1, session 77.
local function Asks(ns)
    local posted = {}
    ns.Stream.Post = function(value)
        posted[#posted + 1] = value
        return #posted, 77
    end
    return posted
end

-- The game's clock on the server's (published messages have its time): a test can wait for long here.
local function Now() client.epoch = os.time() - client.now end

-- The bridge's note about the question in post `post`: "ack", or "progress" with the step it's on. `quiet`:
-- the most seconds it says it may go without news. `more`: other fields, as JSON ('"tokens":0').
local function Note(status, post, step, quiet, more)
    Now()
    client.Publish('{"status":"' .. status .. '","session":77,"post":' .. post .. ',"text":"' .. (step or "") .. '"'
        .. (quiet and ',"quiet":' .. quiet or "") .. (more and "," .. more or "") .. '}')
    client.Wait()
end

-- The bridge's answer to the question in post `post`; `more` as for Note.
local function AnswerTo(post, text, session, more)
    Now()
    client.Publish('{"text":"' .. text .. '","reply":true,"session":' .. (session or 77) .. ',"post":' .. post
        .. (more and "," .. more or "") .. '}')
    client.Wait()
end

-- The arrow before token counts; the counts as the bridge sends them (Claude Code's: an --agent's have none).
local ARROW = "|A:minimal-scrollbar-arrow-bottom-over:8:12|a"
local function Counted(tokens) return '"tokens":' .. tokens end
"""


@pytest.fixture
def game(run_client, tmp_path, monkeypatch):
    script = tmp_path / "publish_messages.py"
    script.write_text(PUBLISH_SCRIPT)
    monkeypatch.setenv("WOWMCP_PUBLISH", f"{sys.executable} {script}")
    return lambda lua: run_client(HELPERS + lua)


def test_tab_is_made_once_when_first_needed(game):
    game(r"""
        -- An answer that arrives while logging in (asked before a /reload) shows in the main window:
        -- the chat windows may not be set up yet, so no tab is made then.
        client.Publish('{"text":"Early answer","reply":true}')
        local ns = client.LoadAddon()
        local posted = Posts(ns)
        assert(Has(client.chat, "[Claude]|r Early answer") and client.windowsMade == 0)
        Settle()

        -- Nor for anything else: the AI's own messages, addon lines.
        client.Publish('{"text":"Hello from the agent"}')
        client.Wait()
        SlashCmdList.WOWMCP("status")
        assert(Has(client.chat, "[Claude]|r Hello from the agent") and client.windowsMade == 0 and not Tab())

        -- The first /ai makes it: docked, selected, with no chat groups or channels of its own.
        client.Type("/ai where is the bank?")
        local tab, id = Tab()
        assert(client.windowsMade == 1 and tab and tab.name == "AI" and id == 3)
        local w = client.chatWindows[id]
        assert(#w.messages == 0 and #w.channels == 0 and #tab.messageTypeList == 0)
        assert(select("#", GetChatWindowMessages(id)) == 0 and select("#", GetChatWindowChannels(id)) == 0)
        assert(w.docked == 3 and GENERAL_CHAT_DOCK.selected == tab and tab:IsShown() and not ChatFrame1:IsShown())
        local lines = client.Lines(tab)
        assert(lines[1]:find("Type here to ask the AI, no /ai needed", 1, true), lines[1])
        assert(lines[2] == YOU .. "where is the bank?" and lines[3] == STATUS .. "Thinking… 0:00|r" and #lines == 3,
            table.concat(lines, "\n"))
        assert(not Has(client.chat, "where is the bank") and posted[1].question == "where is the bank?")

        client.Type("/ai and the auction house?")
        assert(client.windowsMade == 1 and Tab() == tab)

        -- /reload: found again by name, not made again.
        ns = Reload()
        posted = Posts(ns)
        Settle()
        client.Type("/ai still there?")
        assert(client.windowsMade == 1 and Tab() == tab and Has(client.Lines(tab), YOU .. "still there?"))
        assert(not Has(client.chat, "still there"))
    """)


def test_conversation_and_where_it_shows(game):
    game(r"""
        local ns = client.LoadAddon()
        local posted = Posts(ns)
        Settle()
        client.Type("/ai who sells linen?")
        local tab = Tab()

        -- The answer below the question, items as links; "Thinking…" is gone.
        client.Publish('{"text":"Try {item:2589:Linen Cloth} at the vendor.\\nOr farm it.","reply":true}')
        client.Wait()
        local lines = client.Lines(tab)
        assert(lines[#lines - 2] == YOU .. "who sells linen?", table.concat(lines, "\n"))
        assert(lines[#lines - 1]:find("^|cff33ff99%[Claude%]|r Try .*|Hitem:2589:.-|h%[Linen Cloth%]|h.* at the vendor%.$"), lines[#lines - 1])
        assert(lines[#lines] == "    Or farm it." and not Has(lines, "Thinking"))
        assert(not Has(client.chat, "Linen") and client.sounds[#client.sounds] == 3081)

        -- Two questions out: "Thinking…" stays at the bottom until both are answered.
        client.Type("/ai first?")
        client.Type("/ai second?")
        assert(Count(client.Lines(tab), "Thinking") == 1)
        Answer("One.")
        lines = client.Lines(tab)
        assert(lines[#lines - 1] == "|cff33ff99[Claude]|r One." and Thinking(lines[#lines]), table.concat(lines, "\n"))
        Answer("Two.")
        assert(not Has(client.Lines(tab), "Thinking"))

        -- Notes about the conversation are part of it.
        client.Type("/ai new")
        assert(Has(client.Lines(tab), "Starting a fresh conversation.") and not Has(client.chat, "fresh conversation"))
        assert(posted[#posted].reset == true)

        -- Everything else stays in the main window: the AI's own messages, addon lines.
        client.Publish('{"text":"Heads up"}')
        client.Wait()
        SlashCmdList.WOWMCP("status")
        assert(Has(client.chat, "[Claude]|r Heads up") and Has(client.chat, "live updates:"))
        assert(not Has(client.Lines(tab), "Heads up") and not Has(client.Lines(tab), "live updates"))

        -- News while another tab is selected: the tab flashes until it's selected.
        client.ClickTab(ChatFrame1)
        assert(not tab:IsShown() and not client.TabFlashing(tab))
        Answer("Later.")
        assert(client.TabFlashing(tab) and not Has(client.chat, "Later."))
        client.ClickTab(tab)
        assert(tab:IsShown() and not client.TabFlashing(tab))
        Answer("Seen.")
        assert(not client.TabFlashing(tab))

        -- both: the tab, and the main window as before the tab.
        SlashCmdList.WOWMCP("aitab both")
        assert(WowMCPDB.aiTab.mode == "both" and Has(client.chat, "The AI talks in its chat tab and here."))
        client.Type("/ai both?")
        assert(Has(client.Lines(tab), YOU .. "both?") and Has(client.chat, "|cff33ff99WowMCP|r Asking… both?"))
        assert(not Has(client.chat, "Thinking"))
        Answer("Both ways.")
        assert(Has(client.Lines(tab), "[Claude]|r Both ways.") and Has(client.chat, "[Claude]|r Both ways."))

        -- off: only the main window, as before the tab.
        SlashCmdList.WOWMCP("aitab off")
        local before = #client.Lines(tab)
        client.Type("/ai off?")
        Answer("Off.")
        assert(#client.Lines(tab) == before and Has(client.chat, "Asking… off?") and Has(client.chat, "[Claude]|r Off."))

        -- on again (the default).
        SlashCmdList.WOWMCP("aitab on")
        client.Type("/ai on?")
        assert(Has(client.Lines(tab), YOU .. "on?") and not Has(client.chat, "Asking… on?"))
        SlashCmdList.WOWMCP("aitab loud")
        assert(Has(client.chat, "/mcp aitab [on, both, off]") and WowMCPDB.aiTab.mode == "on")

        -- Nothing at all from the bridge for a while: "Thinking…" gives way to a hint about it, in the tab only.
        client.Type("/ai anyone?")
        client.Wait(32)
        lines = client.Lines(tab)
        assert(not Has(lines, "Thinking") and lines[#lines] == STATUS .. DOWN .. "|r", lines[#lines])
        assert(Count(lines, DOWN) == 1 and not Has(client.chat, DOWN))
    """)


def test_bridge_answers_are_replies(addons):
    from wow_mcp.app import transport
    from wow_mcp.tools import messages

    messages.publish_message("An answer.", reply=True)
    messages.publish_message("A heads-up.")
    # The bridge's answers also say what model it answers with by default, and how hard it thinks.
    messages.publish_message("Signed.", sender="Claude Sonnet", reply=True, default_model="opus")
    messages.publish_message("Thought.", sender="Claude Sonnet · max", reply=True, default_model="", default_effort="high")
    sent = transport().load_state()["messages"]
    assert [(m["text"], m.get("reply"), m.get("defaultModel"), m.get("defaultEffort")) for m in sent] == [
        ("An answer.", True, None, None), ("A heads-up.", None, None, None), ("Signed.", True, "opus", None),
        ("Thought.", True, "", "high"),
    ]
    assert sent[2]["from"] == "Claude Sonnet" and sent[3]["from"] == "Claude Sonnet · max"


@pytest.mark.parametrize("style", ["classic", "im"])
def test_typing_in_the_tab(game, style):
    """In the AI tab a plain line is a question; slash commands and lines aimed elsewhere work as
    always. "classic" chat shares one chat box between the tabs, "im" gives each its own."""
    game(r"""
        client.chatStyle = "STYLE"
        local ns = client.LoadAddon()
        local posted = Posts(ns)
        Settle()
        SlashCmdList.WOWMCP("aitab") -- makes the tab
        local tab = Tab()
        assert(tab and GENERAL_CHAT_DOCK.selected == tab)
        client.ClickTab(tab)

        -- A plain line: asked, not sent to the chat. "AI:" in place of the chat type header meanwhile.
        local box = ChatFrameUtil.OpenChat(nil)
        assert(box == (client.chatStyle == "classic" and ChatFrame1.editBox or tab.editBox))
        assert(Labelled(box))
        client.TypeLine("where am I?")
        assert(#posted == 1 and posted[1].question == "where am I?" and #client.sent == 0)
        assert(Has(client.Lines(tab), YOU .. "where am I?"))
        assert(box.history[#box.history] == "/ai where am I?" and box:GetText() == "")
        assert(not Labelled(box) and box.header:GetAlpha() == 1)
        -- Shift-clicked links become text the agent can read.
        client.EnterLine("who buys |cff1eff00|Hitem:2589::::::::60:::::::::|h[Linen Cloth]|h|r?")
        assert(posted[2].question == "who buys [Linen Cloth] (item 2589)?")

        -- Slash commands run as usual: secure ones, the addon's, unknown ones.
        client.EnterLine("/cast Fireball")
        client.EnterLine("/mcp status")
        client.EnterLine("/ai and now?")
        client.EnterLine("/nonsense")
        assert(client.ran[1] == "/CAST Fireball" and Has(client.chat, "live updates:"))
        assert(#posted == 3 and posted[3].question == "and now?" and #client.sent == 0)

        -- A line aimed elsewhere goes there: a chat type command, even the box's current one.
        client.EnterLine("/s hello there")
        client.EnterLine("/w Grunta psst")
        assert(client.sent[1].text == "hello there" and client.sent[1].type == "SAY")
        assert(client.sent[2].text == "psst" and client.sent[2].type == "WHISPER" and client.sent[2].target == "Grunta")
        assert(#posted == 3)
        -- The box now opens as a whisper to Grunta (it's sticky); a plain line still asks the AI.
        box = ChatFrameUtil.OpenChat(nil)
        assert(box:GetChatType() == "WHISPER" and Labelled(box))
        client.TypeLine("is Grunta online?")
        assert(#posted == 4 and posted[4].question == "is Grunta online?" and #client.sent == 2)
        -- Unless the player starts typing a chat type command, then the label goes.
        box = ChatFrameUtil.OpenChat(nil)
        box:SetText("/p ")
        assert(not Labelled(box) and box.header:GetAlpha() == 1)
        client.TypeLine("party time")
        assert(client.sent[3].text == "party time" and client.sent[3].type == "PARTY" and #posted == 4)
        -- R replies, to the sticky whisper target too; a whisper to a friend from the friends list.
        client.lastTell = "Grunta"
        ChatFrameUtil.ReplyTell()
        assert(not Labelled(ACTIVE_CHAT_EDIT_BOX))
        client.TypeLine("yes?")
        client.EnterLine("/w Grunta again")
        ChatFrameUtil.ReplyTell()
        client.TypeLine("still here")
        ChatFrameUtil.SendBNetTell("Friend")
        client.TypeLine("hi friend")
        assert(client.sent[4].text == "yes?" and client.sent[4].target == "Grunta")
        assert(client.sent[6].text == "still here" and client.sent[6].target == "Grunta")
        assert(client.sent[7].text == "hi friend" and client.sent[7].type == "BN_WHISPER" and #posted == 4)

        -- What the game hides (secret) isn't readable: not asked, and not sent either.
        issecretvalue = function(value) return value == "<secret>" end
        box = ChatFrameUtil.OpenChat(nil)
        box.text = "<secret>"
        box:OnEnterPressed()
        issecretvalue = nil
        assert(#posted == 4 and #client.sent == 7 and Has(client.Lines(tab), "The game is hiding what you typed"))

        -- Another tab: as before.
        client.ClickTab(ChatFrame1)
        client.EnterLine("/s hello everyone")
        client.EnterLine("and again")
        assert(client.sent[9].text == "and again" and client.sent[9].type == "SAY" and #posted == 4)
        box = ChatFrameUtil.OpenChat(nil)
        assert(not Labelled(box) and box.header:GetAlpha() == 1)
        client.TypeLine("/cast Frostbolt")
        assert(client.ran[2] == "/CAST Frostbolt")

        -- /mcp aitab off: the tab is an ordinary tab (the line goes where its chat box is set to).
        SlashCmdList.WOWMCP("aitab off")
        client.ClickTab(tab)
        client.EnterLine("plain line")
        assert(client.sent[10].text == "plain line" and #posted == 4)
    """.replace("STYLE", style))


def test_closed_or_renamed_tab_is_respected(game):
    game(r"""
        local ns = client.LoadAddon()
        local posted = Posts(ns)
        Settle()
        client.Type("/ai first")
        local tab = Tab()

        -- Closed: the AI talks in the main window, says once how to get the tab back, and doesn't
        -- make it again on its own.
        FCF_Close(tab)
        assert(not Tab())
        client.Type("/ai second")
        Answer("Second answer.")
        client.Type("/ai third")
        assert(client.windowsMade == 1 and not Tab())
        assert(Has(client.chat, "Asking… second") and Has(client.chat, "[Claude]|r Second answer."))
        assert(Count(client.chat, "/mcp aitab brings the tab back") == 1)
        -- Typing: nothing to type into, so it's as before.
        client.EnterLine("hello")
        assert(client.sent[1].text == "hello" and #posted == 3)

        -- After a /reload too.
        ns = Reload()
        posted = Posts(ns)
        Settle()
        client.Type("/ai fourth")
        assert(client.windowsMade == 1 and not Tab() and Has(client.chat, "Asking… fourth"))

        -- /mcp aitab brings it back.
        SlashCmdList.WOWMCP("aitab")
        tab = Tab()
        assert(client.windowsMade == 2 and tab and Has(client.chat, "The AI talks in its chat tab."))
        client.Type("/ai fifth")
        assert(Has(client.Lines(tab), YOU .. "fifth"))

        -- Renamed: the player's tab is left as it is; the AI talks in the main window again.
        FCF_SetWindowName(tab, "Claude")
        client.Type("/ai sixth")
        assert(client.windowsMade == 2 and not Tab() and tab.name == "Claude")
        assert(Has(client.chat, "Asking… sixth") and not Has(client.Lines(tab), "sixth"))
        assert(Has(client.chat, "/mcp aitab brings the tab back"))
        -- And /mcp aitab makes a new AI tab next to it.
        SlashCmdList.WOWMCP("aitab")
        assert(client.windowsMade == 3 and Tab() ~= tab and tab.name == "Claude")
    """)


def test_no_room_for_a_tab(game):
    game(r"""
        for i = 3, 10 do client.chatWindows[i] = { name = "Tab " .. i, shown = true, docked = i, messages = {}, channels = {} } end
        local ns = client.LoadAddon()
        Posts(ns)
        Settle()
        client.Type("/ai where?")
        client.Type("/ai and?")
        assert(client.windowsMade == 0 and Has(client.chat, "Asking… where?") and Has(client.chat, "Asking… and?"))
        assert(Count(client.chat, "all 10 chat windows are in use") == 1)
    """)


def test_model_picker_shows_only_on_the_ai_tab(game):
    game(r"""
        local ns = client.LoadAddon()
        Posts(ns)
        Settle()
        -- Blizzard's dropdown, made at login, but there's no AI tab to show it on yet.
        local picker = WowMCPModelPicker
        assert(picker == client.dropdowns[#client.dropdowns] and picker.template == "WowStyle1DropdownTemplate")
        assert(not picker:IsShown() and picker.parent ~= ChatFrame1 and picker.parent.strata == "LOW")

        -- At the AI tab's top right while it's selected; not on the other tabs.
        SlashCmdList.WOWMCP("aitab")
        local tab = Tab()
        client.Tick()
        assert(picker:IsShown() and picker.Text.text == "Default")
        local point, to, relative = picker:GetPoint()
        assert(point == "BOTTOMRIGHT" and to == tab and relative == "TOPRIGHT")
        client.ClickTab(ChatFrame1)
        client.Tick()
        assert(not picker:IsShown())
        client.ClickTab(tab)
        client.Tick()
        assert(picker:IsShown())

        -- Faded like the AI tab's own chat tab, unless the mouse is on it or its menu is open.
        _G[tab:GetName() .. "Tab"]:SetAlpha(0.4)
        client.Tick()
        assert(picker:GetAlpha() == 0.4)
        picker.mouseOver = true
        client.Tick()
        assert(picker:GetAlpha() == 1)
        picker.mouseOver = false
        client.OpenMenu(picker)
        client.Tick()
        assert(picker:GetAlpha() == 1)
        picker:CloseMenu()
        client.Tick()
        assert(picker:GetAlpha() == 0.4)

        -- Not while the AI talks in the main window only, nor once the tab is closed or renamed.
        SlashCmdList.WOWMCP("aitab off")
        client.Tick()
        assert(not picker:IsShown())
        SlashCmdList.WOWMCP("aitab on")
        client.Tick()
        assert(picker:IsShown())
        FCF_Close(tab)
        client.Tick()
        assert(not picker:IsShown())
        SlashCmdList.WOWMCP("aitab")
        client.Tick()
        assert(Tab() == tab and picker:IsShown())
        FCF_SetWindowName(tab, "Claude")
        client.Tick()
        assert(not picker:IsShown())
        -- A new AI tab next to the renamed one: the picker moves there.
        SlashCmdList.WOWMCP("aitab")
        local again = Tab()
        client.Tick()
        assert(again ~= tab and picker:IsShown() and select(2, picker:GetPoint()) == again)
    """)


def test_picking_a_model(game):
    game(r"""
        local ns = client.LoadAddon()
        local posted = Posts(ns)
        Settle()
        client.Type("/ai first")
        local tab = Tab()
        client.Tick()
        local picker = WowMCPModelPicker
        assert(posted[1].model == nil and picker:IsShown())

        -- The menu: every model with what it's good for, then how hard it thinks; Default picked in each.
        local menu = table.concat(Menu(picker), "\n")
        assert(menu == table.concat({
            "# Model",
            "*Default  |cffaaaaaawhat the bridge was started with|r",
            "Fable  |cffaaaaaathe most capable, slowest|r",
            "Opus  |cffaaaaaavery capable|r",
            "Sonnet  |cffaaaaaafast and strong|r",
            "Haiku  |cffaaaaaafastest, for simple lookups|r",
            "--",
            "# Thinking",
            "*Default  |cffaaaaaawhat the bridge was started with, or Claude Code's own|r",
            "Low  |cffaaaaaafastest|r",
            "Medium  |cffaaaaaabalanced|r",
            "High  |cffaaaaaathorough|r",
            "Extra high  |cffaaaaaavery thorough, slower|r",
            "Max  |cffaaaaaaslowest, deepest|r",
        }, "\n"), menu)

        -- Picking one: kept for this character, said in the conversation, sent with the next questions.
        client.PickMenu(picker, "Sonnet")
        assert(WowMCPDB.aiTab.models["Thorgrim-Firemaw"] == "sonnet" and picker.Text.text == "Sonnet")
        local lines = client.Lines(tab)
        assert(lines[#lines] == "|cffaaaaaaAnswers now come from Sonnet.|r", lines[#lines])
        assert(#posted == 1 and not Has(client.chat, "Answers now"))
        client.Type("/ai where?")
        assert(posted[2].question == "where?" and posted[2].model == "sonnet")

        -- The answer names the model; the bridge says what it uses by default, so Default can say it too.
        AnswerFrom("Claude Sonnet", "Over there.", "opus")
        lines = client.Lines(tab)
        assert(lines[#lines - 1] == "|cff33ff99[Claude Sonnet]|r Over there.", lines[#lines - 1])
        assert(WowMCPDB.aiTab.bridgeModel == "opus")
        assert(Menu(picker)[2] == "Default (Opus)  |cffaaaaaawhat the bridge was started with|r")
        assert(Menu(picker)[5]:sub(1, 1) == "*")

        -- Kept over a /reload; the picker shows it again (General is selected after one).
        ns = Reload()
        posted = Posts(ns)
        Settle()
        picker = WowMCPModelPicker
        assert(not picker:IsShown())
        client.ClickTab(tab)
        client.Tick()
        assert(picker:IsShown() and picker.Text.text == "Sonnet")
        client.Type("/ai still sonnet?")
        assert(posted[1].model == "sonnet")

        -- Back to Default: no model sent, and the bridge's own is named.
        client.PickMenu(picker, "Default")
        lines = client.Lines(tab)
        assert(lines[#lines] == "|cffaaaaaaAnswers now come from the model the bridge was started with (Opus).|r", lines[#lines])
        assert(WowMCPDB.aiTab.models["Thorgrim-Firemaw"] == nil and picker.Text.text == "Default (Opus)")
        client.Type("/ai and now?")
        assert(posted[2].question == "and now?" and posted[2].model == nil)
        -- A bridge started without --model doesn't know: plain Default again.
        AnswerFrom("Claude", "Hm.", "")
        assert(WowMCPDB.aiTab.bridgeModel == nil and picker.Text.text == "Default")

        -- Per character: another one has its own pick.
        client.PickMenu(picker, "Haiku")
        UnitName = function() return "Grunta", nil end
        client.Type("/ai mine?")
        assert(posted[3].model == nil and WowMCPDB.aiTab.models["Thorgrim-Firemaw"] == "haiku")

        -- The tooltip says what it is.
        local said = TooltipLines(picker.tooltipFunc)
        assert(said[1] == "AI model: Default" and said[2] == "Thinking: Default")
        assert(said[3]:find("Which Claude model answers your /ai questions", 1, true))
    """)


def test_model_command(game):
    game(r"""
        local ns = client.LoadAddon()
        local posted = Posts(ns)
        Settle()
        SlashCmdList.WOWMCP("aitab")
        local tab = Tab()
        client.Tick()
        local function Said(text) return Has(client.Lines(tab), text) end
        client.Type("/ai")
        assert(Said("/ai model [name] - which Claude model answers (default, fable, opus, sonnet, haiku); the AI tab has a picker too"))

        -- "model": which one answers. With a name, in any case: picks it, and the picker follows.
        client.Type("/ai model")
        assert(Said("Answers come from Default. /ai model <name> picks another: default, fable, opus, sonnet, haiku."))
        client.Type("/ai model HAIKU")
        assert(WowMCPDB.aiTab.models["Thorgrim-Firemaw"] == "haiku" and Said("Answers now come from Haiku."))
        assert(WowMCPModelPicker.Text.text == "Haiku")
        client.Type("/ask  Model   fable ")
        assert(WowMCPDB.aiTab.models["Thorgrim-Firemaw"] == "fable" and WowMCPModelPicker.Text.text == "Fable")
        client.Type("/ai model")
        assert(Said("Answers come from Fable."))

        -- Another single word: the options, and nothing changes.
        client.Type("/ai model gpt-5")
        assert(Said("/ai model <name> - which Claude model answers you (also picked on the AI tab): default (what the "
            .. "bridge was started with); fable (Fable, the most capable, slowest); opus (Opus, very capable); "
            .. "sonnet (Sonnet, fast and strong); haiku (Haiku, fastest, for simple lookups)"))
        assert(WowMCPDB.aiTab.models["Thorgrim-Firemaw"] == "fable")

        client.Type("/ai model default")
        assert(WowMCPDB.aiTab.models["Thorgrim-Firemaw"] == nil and Said("Answers now come from the model the bridge was started with."))
        assert(WowMCPModelPicker.Text.text == "Default")
        -- None of that went to the AI.
        assert(#posted == 0 and not Said("[You]"))

        -- Questions that only start with "model" are questions.
        for _, question in ipairs({ "model of the Deeprun Tram?", "model?", "models", "Model sonnet please", "modeling clay vendor" }) do
            client.Type("/ai " .. question)
            assert(posted[#posted].question == question, question)
        end
        assert(#posted == 5 and WowMCPDB.aiTab.models["Thorgrim-Firemaw"] == nil)

        -- Typed in the AI tab, as /ai model.
        client.ClickTab(tab)
        client.EnterLine("model opus")
        assert(#posted == 5 and #client.sent == 0 and WowMCPDB.aiTab.models["Thorgrim-Firemaw"] == "opus")
        client.EnterLine("what's a good model for pets?")
        assert(posted[6].question == "what's a good model for pets?" and posted[6].model == "opus")
    """)


def test_model_picker_without_blizzard_dropdowns(game):
    """A client without Blizzard_Menu's dropdowns: a button that picks the next model on a click."""
    game(r"""
        client.noDropdowns = true
        local ns = client.LoadAddon()
        local posted = Posts(ns)
        Settle()
        SlashCmdList.WOWMCP("aitab")
        local tab = Tab()
        client.Tick()
        local button = WowMCPModelPicker
        assert(#client.dropdowns == 0 and button and button:IsShown() and button.text == "Default")
        assert(select(2, button:GetPoint()) == tab)

        -- The tooltip lists the models, the picked one marked.
        local lines = {}
        GameTooltip = setmetatable({}, { __index = function(_, name)
            return function(_, text) if name == "SetText" or name == "AddLine" then lines[#lines + 1] = text end end
        end })
        button.OnEnter(button)
        assert(lines[1] == "AI model: Default" and lines[2] == "Thinking: Default")
        assert(Has(lines, "> Default: what the bridge was started with") and Has(lines, "   Haiku: fastest, for simple lookups"))
        assert(Has(lines, "> Default: what the bridge was started with, or Claude Code's own") and Has(lines, "   Max: slowest, deepest"))
        assert(Has(lines, "Click: the next model.") and Has(lines, "With Shift: the thinking level."))

        -- A click: the next one; a right-click: the one before, round the list.
        button.OnClick(button, "LeftButton")
        assert(button.text == "Fable" and WowMCPDB.aiTab.models["Thorgrim-Firemaw"] == "fable")
        assert(Has(client.Lines(tab), "Answers now come from Fable."))
        button.OnClick(button, "RightButton")
        button.OnClick(button, "RightButton")
        assert(button.text == "Haiku" and WowMCPDB.aiTab.models["Thorgrim-Firemaw"] == "haiku")
        client.Type("/ai quick one")
        assert(posted[1].model == "haiku")
        client.Type("/ai model sonnet")
        assert(button.text == "Sonnet")

        -- With Shift: the thinking level, the same way; the button shows both.
        IsShiftKeyDown = function() return true end
        button.OnClick(button, "LeftButton")
        assert(button.text == "Sonnet · low" and WowMCPDB.aiTab.efforts["Thorgrim-Firemaw"] == "low")
        button.OnClick(button, "RightButton")
        button.OnClick(button, "RightButton")
        assert(button.text == "Sonnet · max" and Has(client.Lines(tab), "The AI now thinks: Max."))
        assert(WowMCPDB.aiTab.models["Thorgrim-Firemaw"] == "sonnet")
        IsShiftKeyDown = nil
        client.Type("/ai deep one")
        assert(posted[2].model == "sonnet" and posted[2].effort == "max")
        client.Type("/ai thinking medium")
        assert(button.text == "Sonnet · medium")

        client.ClickTab(ChatFrame1)
        client.Tick()
        assert(not button:IsShown())
    """)


def test_thinking_is_picked_on_the_model_picker(game):
    """One dropdown picks both, where the model picker always was and as wide: two side by side would run
    into the chat tabs. Nothing else of the addon's sits by the chat windows or their tabs."""
    game(r"""
        local ns = client.LoadAddon()
        Posts(ns)
        Settle()
        SlashCmdList.WOWMCP("aitab")
        local tab = Tab()
        client.Tick()
        local picker = WowMCPModelPicker
        assert(#client.dropdowns == 1 and picker == client.dropdowns[1] and picker:IsShown())
        local point, to, relative, x, y = picker:GetPoint()
        assert(point == "BOTTOMRIGHT" and to == tab and relative == "TOPRIGHT" and x == 0 and y == 4 and picker.width == 140)
        local chat = {}
        for i = 1, 10 do chat[_G["ChatFrame" .. i]], chat[_G["ChatFrame" .. i .. "Tab"]] = true, true end
        for _, frame in ipairs(client.frames) do
            for _, anchor in ipairs(frame.points) do
                assert(frame == picker or not chat[anchor[2]], "another frame by the chat tabs")
            end
        end

        -- Both picks on its button, in short; the menu has both groups, each under its title.
        local menu = Menu(picker)
        assert(menu[1] == "# Model" and menu[7] == "--" and menu[8] == "# Thinking" and #menu == 14, table.concat(menu, "\n"))
        client.PickMenu(picker, "Extra high", "Thinking")
        assert(picker.Text.text == "Default · xhigh")
        client.PickMenu(picker, "Haiku", "Model")
        assert(picker.Text.text == "Haiku · xhigh")
        -- Default in either group is that group's.
        client.PickMenu(picker, "Default", "Thinking")
        assert(picker.Text.text == "Haiku" and WowMCPDB.aiTab.models["Thorgrim-Firemaw"] == "haiku")
        client.PickMenu(picker, "Max", "Thinking")
        client.PickMenu(picker, "Default", "Model")
        assert(picker.Text.text == "Default · max" and WowMCPDB.aiTab.efforts["Thorgrim-Firemaw"] == "max")

        -- Shown and hidden with the AI tab, as before.
        client.ClickTab(ChatFrame1)
        client.Tick()
        assert(not picker:IsShown())
        client.ClickTab(tab)
        client.Tick()
        assert(picker:IsShown() and picker.Text.text == "Default · max")
        SlashCmdList.WOWMCP("aitab off")
        client.Tick()
        assert(not picker:IsShown())
    """)


def test_picking_a_thinking_level(game):
    game(r"""
        local ns = client.LoadAddon()
        local posted = Posts(ns)
        Settle()
        client.Type("/ai first")
        local tab = Tab()
        client.Tick()
        local picker = WowMCPModelPicker
        assert(posted[1].effort == nil and picker.Text.text == "Default")

        -- Picking one: kept for this character, said in the conversation, sent with the next questions.
        -- The model stays as it was.
        client.PickMenu(picker, "High", "Thinking")
        assert(WowMCPDB.aiTab.efforts["Thorgrim-Firemaw"] == "high" and picker.Text.text == "Default · high")
        local lines = client.Lines(tab)
        assert(lines[#lines] == "|cffaaaaaaThe AI now thinks: High.|r", lines[#lines])
        assert(not Has(client.chat, "now thinks") and (WowMCPDB.aiTab.models or {})["Thorgrim-Firemaw"] == nil)
        client.Type("/ai where?")
        assert(posted[2].question == "where?" and posted[2].effort == "high" and posted[2].model == nil)
        client.PickMenu(picker, "Sonnet", "Model")
        client.Type("/ai and?")
        assert(posted[3].model == "sonnet" and posted[3].effort == "high" and picker.Text.text == "Sonnet · high")

        -- The answer names both; the bridge says how hard it thinks by default, so Default can say it too.
        AnswerFrom("Claude Sonnet · high", "Over there.", "opus", "max")
        assert(Has(client.Lines(tab), "|cff33ff99[Claude Sonnet · high]|r Over there."))
        assert(WowMCPDB.aiTab.bridgeEffort == "max" and WowMCPDB.aiTab.bridgeModel == "opus")
        local menu = Menu(picker)
        assert(menu[9] == "Default (Max)  |cffaaaaaawhat the bridge was started with, or Claude Code's own|r", menu[9])
        assert(menu[12] == "*High  |cffaaaaaathorough|r" and menu[5]:sub(1, 1) == "*")
        local said = TooltipLines(picker.tooltipFunc)
        assert(said[1] == "AI model: Sonnet" and said[2] == "Thinking: High")

        -- Kept over a /reload; the picker shows it again (General is selected after one).
        ns = Reload()
        posted = Posts(ns)
        Settle()
        picker = WowMCPModelPicker
        client.ClickTab(tab)
        client.Tick()
        assert(picker:IsShown() and picker.Text.text == "Sonnet · high")
        client.Type("/ai still?")
        assert(posted[1].model == "sonnet" and posted[1].effort == "high")

        -- Back to Default: no level sent, and the bridge's own is named.
        client.PickMenu(picker, "Default", "Thinking")
        lines = client.Lines(tab)
        assert(lines[#lines] == "|cffaaaaaaThe AI now thinks at the level the bridge was started with (Max).|r", lines[#lines])
        assert(WowMCPDB.aiTab.efforts["Thorgrim-Firemaw"] == nil and picker.Text.text == "Sonnet")
        assert(WowMCPDB.aiTab.models["Thorgrim-Firemaw"] == "sonnet")
        assert(TooltipLines(picker.tooltipFunc)[2] == "Thinking: Default (Max)")
        client.Type("/ai and now?")
        assert(posted[2].effort == nil and posted[2].model == "sonnet")
        -- An answer that doesn't say leaves it; a bridge started without --effort doesn't know: plain Default.
        AnswerFrom("Claude Sonnet", "Hm.", "opus")
        assert(WowMCPDB.aiTab.bridgeEffort == "max")
        AnswerFrom("Claude Sonnet", "Hm.", "opus", "")
        assert(WowMCPDB.aiTab.bridgeEffort == nil and TooltipLines(picker.tooltipFunc)[2] == "Thinking: Default")

        -- Per character: another one has its own pick.
        client.PickMenu(picker, "Max", "Thinking")
        UnitName = function() return "Grunta", nil end
        client.Type("/ai mine?")
        assert(posted[3].effort == nil and posted[3].model == nil and WowMCPDB.aiTab.efforts["Thorgrim-Firemaw"] == "max")
    """)


def test_thinking_command(game):
    game(r"""
        local ns = client.LoadAddon()
        local posted = Posts(ns)
        Settle()
        SlashCmdList.WOWMCP("aitab")
        local tab = Tab()
        client.Tick()
        local function Shows(text) return Has(client.Lines(tab), text) end
        client.Type("/ai")
        assert(Shows("/ai thinking [level] - how hard the AI thinks (default, low, medium, high, xhigh, max); also on the AI tab's picker"))

        -- "thinking" (or "effort"): how hard it thinks. With a level, in any case: picks it, and the picker follows.
        client.Type("/ai thinking")
        assert(Shows("The AI thinks: Default. /ai thinking <level> picks another: default, low, medium, high, xhigh, max."))
        client.Type("/ai thinking HIGH")
        assert(WowMCPDB.aiTab.efforts["Thorgrim-Firemaw"] == "high" and Shows("The AI now thinks: High."))
        assert(WowMCPModelPicker.Text.text == "Default · high")
        client.Type("/ask  Effort   xHigh ")
        assert(WowMCPDB.aiTab.efforts["Thorgrim-Firemaw"] == "xhigh" and Shows("The AI now thinks: Extra high."))
        assert(WowMCPModelPicker.Text.text == "Default · xhigh")
        client.Type("/ai effort")
        assert(Shows("The AI thinks: Extra high. /ai thinking <level> picks another"))

        -- Another single word: the options, and nothing changes.
        client.Type("/ai thinking turbo")
        assert(Shows("/ai thinking <level> - how hard the AI thinks before answering (also picked on the AI tab): default "
            .. "(what the bridge was started with, or Claude Code's own); low (Low, fastest); medium (Medium, balanced); "
            .. "high (High, thorough); xhigh (Extra high, very thorough, slower); max (Max, slowest, deepest)"))
        assert(WowMCPDB.aiTab.efforts["Thorgrim-Firemaw"] == "xhigh")
        -- Default names the bridge's own level once the bridge has said it.
        AnswerFrom("Claude · xhigh", "Ok.", "", "medium")
        client.Type("/ai effort extra")
        assert(Shows("default (what the bridge was started with, or Claude Code's own, Medium); low (Low, fastest)"))

        client.Type("/ai thinking Default")
        assert(WowMCPDB.aiTab.efforts["Thorgrim-Firemaw"] == nil)
        assert(Shows("The AI now thinks at the level the bridge was started with (Medium)."))
        assert(WowMCPModelPicker.Text.text == "Default")
        -- None of that went to the AI.
        assert(#posted == 0)

        -- Questions that only start with "thinking" or "effort" are questions.
        for _, question in ipairs({ "thinking of rolling a druid, any tips?", "thinking?", "thinkings", "Thinking high please",
                                    "effort.", "efforts to farm silk?", "is it worth the effort" }) do
            client.Type("/ai " .. question)
            assert(posted[#posted].question == question, question)
        end
        assert(#posted == 7 and WowMCPDB.aiTab.efforts["Thorgrim-Firemaw"] == nil)

        -- Typed in the AI tab, as /ai thinking.
        client.ClickTab(tab)
        client.EnterLine("thinking max")
        assert(#posted == 7 and #client.sent == 0 and WowMCPDB.aiTab.efforts["Thorgrim-Firemaw"] == "max")
        client.EnterLine("thinking about professions")
        assert(posted[8].question == "thinking about professions" and posted[8].effort == "max")
    """)


def test_status_line(game):
    """While an answer is due, the status line says how long it's taking and the step the bridge last said the AI
    is on. The clock ticks in game, at no cost, in place; each step the bridge sends costs it a live-update slot."""
    game(r"""
        local ns = client.LoadAddon()
        local posted = Asks(ns)
        Settle()
        client.Type("/ai any good flips near me?")
        local tab = Tab()
        local function Status()
            local lines = client.Lines(tab)
            return lines[#lines]
        end
        assert(posted[1].question == "any good flips near me?" and Status() == STATUS .. "Thinking… 0:00|r", Status())

        -- The bridge says it has it, and the clock ticks on, every second: the line changes in place.
        Note("ack", 1, nil, 120)
        local count = #client.Lines(tab)
        client.Wait(20)
        assert(Status():match("^|cffababab" .. "Thinking… 0:1[01]|r$"), Status())
        assert(#client.Lines(tab) == count and Count(client.Lines(tab), STATUS) == 1)

        -- The steps the bridge says the AI is on.
        Note("progress", 1, "checking auction prices")
        assert(Status():match("^|cffababab" .. "Thinking… 0:1%d · checking auction prices|r$"), Status())

        -- Long past the bridge's old limit (180 s), while it keeps saying it's at work (at least every 120 s, as its
        -- ack said): no warning.
        for _ = 1, 3 do
            client.Wait(220)
            assert(Thinking(Status()), Status())
            Note("progress", 1, "working it out")
        end
        assert(Status():match("^|cffababab" .. "Thinking… 5:4%d · working it out|r$"), Status())

        -- Nothing for longer than that, and a margin: it seems to have stopped. In the tab only, where the
        -- conversation shows.
        client.Wait(340)
        assert(Thinking(Status()), Status())
        client.Wait(30)
        assert(Status() == STATUS .. STOPPED .. "|r" and not Has(client.chat, STOPPED), Status())
        -- It was only slow: carry on.
        Note("progress", 1, "writing")
        assert(Status():match("^|cffababab" .. "Thinking… 8:5%d · writing|r$"), Status())

        -- The answer: the status line goes, and stays gone.
        AnswerTo(1, "Nothing worth flipping here.")
        local lines = client.Lines(tab)
        assert(lines[#lines] == "|cff33ff99[Claude]|r Nothing worth flipping here." and Count(lines, STATUS) == 0)
        client.Wait(20)
        assert(Count(client.Lines(tab), STATUS) == 0)

        -- A bridge whose agent says nothing until it's done (--agent) says how long it may take.
        client.Type("/ai a slow one?")
        Note("ack", 2, nil, 1800)
        client.Wait(1200)
        assert(Thinking(Status()), Status())
        AnswerTo(2, "Done.")
        assert(Count(client.Lines(tab), STATUS) == 0)
    """)


def test_bridge_down(game):
    """No word from the bridge that it has a question: it may not be running. The status line says so, and goes
    back to thinking if word comes after all."""
    game(r"""
        local ns = client.LoadAddon()
        local posted = Asks(ns)
        Settle()
        client.Type("/ai anyone there?")
        local tab = Tab()
        local function Status()
            local lines = client.Lines(tab)
            return lines[#lines]
        end
        client.Wait(28)
        assert(Thinking(Status()), Status())
        client.Wait(4)
        assert(Status() == STATUS .. DOWN .. "|r", Status())
        assert(Count(client.Lines(tab), STATUS) == 1 and not Has(client.chat, "wow-mcp-bridge"))
        -- It was slow to say so: carry on.
        Note("ack", 1)
        assert(Status():match("^|cffababab" .. "Thinking… 0:1%d|r$"), Status())
        AnswerTo(1, "Yes.")
        assert(Count(client.Lines(tab), STATUS) == 0)

        -- In a fight: the bridge's notes come all the same (they need no slot); an answer waits for it to end, and
        -- that there is one is word enough.
        client.combat = true
        client.Type("/ai in a fight?")
        Note("ack", 2)
        assert(Thinking(Status()) and not ns.run.pendingLoad, Status())
        AnswerTo(2, "Won.")
        client.Wait(60)
        assert(Thinking(Status()) and ns.run.pendingLoad, Status())
        client.combat = false
        client.Fire("PLAYER_REGEN_ENABLED")
        assert(Count(client.Lines(tab), STATUS) == 0)

        -- The conversation in the main window too (/mcp aitab both): it's told there as well, once.
        SlashCmdList.WOWMCP("aitab both")
        client.Type("/ai both?")
        client.Wait(40)
        assert(Status() == STATUS .. DOWN .. "|r" and Count(client.chat, DOWN) == 1)
        client.Wait(40)
        assert(Count(client.chat, DOWN) == 1 and not Has(client.chat, "Thinking"))
    """)


def test_several_questions(game):
    """Questions asked while another is answered: one status line, for the oldest, below the latest answer. The
    bridge answers in turn, so a question it never had is given up once it has a later one."""
    game(r"""
        local ns = client.LoadAddon()
        local posted = Asks(ns)
        Settle()
        client.Type("/ai first?")
        local tab = Tab()
        local function Status()
            local lines = client.Lines(tab)
            return lines[#lines]
        end
        client.Wait(20)
        client.Type("/ai second?")
        Note("ack", 1)
        Note("ack", 2)
        Note("progress", 1, "checking auction prices")
        local lines = client.Lines(tab)
        assert(Count(lines, STATUS) == 1 and lines[#lines - 1] == YOU .. "second?", table.concat(lines, "\n"))
        assert(Status():match("^|cffababab" .. "Thinking… 0:1%d · checking auction prices|r$"), Status())

        -- The first answered: the line moves below it, for the second (waiting since it was asked), no step yet.
        AnswerTo(1, "One.")
        lines = client.Lines(tab)
        assert(lines[#lines - 1] == "|cff33ff99[Claude]|r One." and Count(lines, STATUS) == 1)
        assert(Status():match("^|cffababab" .. "Thinking… 0:0%d|r$"), Status())
        Note("progress", 2, "searching the web")
        assert(Status():find(" · searching the web|r", 1, true), Status())
        -- An answer to a question asked before a /reload shows, and changes nothing here.
        AnswerTo(9, "Old answer.", 12)
        assert(Has(client.Lines(tab), "Old answer.") and Status():find(" · searching the web|r", 1, true), Status())
        AnswerTo(2, "Two.")
        assert(Count(client.Lines(tab), STATUS) == 0)

        -- Asked while the bridge was down: no word. Asked again once it's up: the first never got there, so it goes.
        client.Type("/ai lost?")
        client.Wait(32)
        assert(Status() == STATUS .. DOWN .. "|r", Status())
        client.Type("/ai again?")
        Note("ack", 4)
        assert(Status():match("^|cffababab" .. "Thinking… 0:0%d|r$"), Status())
        AnswerTo(4, "Found it.")
        assert(Count(client.Lines(tab), STATUS) == 0)

        -- A bridge from before these notes doesn't say which question it answers: the oldest, then.
        client.Type("/ai old bridge?")
        client.Type("/ai and again?")
        Answer("Old one.")
        assert(Thinking(Status()), Status())
        Answer("Old two.")
        assert(Count(client.Lines(tab), STATUS) == 0)
    """)


def test_status_notes_are_not_messages(game):
    """The bridge's notes go to the status line only: never into chat, /mcp messages or a message's sound."""
    game(r"""
        local ns = client.LoadAddon()
        Asks(ns)
        Settle()
        client.Type("/ai where?")
        local tab = Tab()
        local sounds = #client.sounds
        Note("ack", 1, nil, 120)
        Note("progress", 1, "checking where you are")
        assert(#client.sounds == sounds and not Has(client.chat, "checking where"))
        assert(not Has(client.Lines(tab), "[Claude]"))
        SlashCmdList.WOWMCP("messages")
        assert(Has(client.chat, "No messages yet."))
        AnswerTo(1, "Here.")
        wipe(client.chat)
        SlashCmdList.WOWMCP("messages")
        assert(Has(client.chat, "[Claude]|r Here.") and not Has(client.chat, "checking where"))
    """)


def test_status_notes_go_over_the_bell(addons):
    """The bridge's notes are frames of the bell, not messages: no live-update slot, nothing in the payload. An ack
    names the question by its post id (mod 16) and says how long the bridge may stay quiet; progress names the step
    by its number and carries the tokens written so far, if they're counted."""
    from wow_mcp import bell, bridge
    from wow_mcp.app import transport
    from wow_mcp.tools import messages

    def frame():
        on = [int((transport().bell.dir / name).stat().st_size > 0)
              for name in (bell.SEAL, bell.LEAD, *bell.DATA_FILES)]
        assert on[0] == on[1]
        return bell.decode(sum(b << i for i, b in enumerate(on[2:])), on[0])

    messages.publish_status("ack", question=(77, 17), quiet=120)
    assert frame() == bell.ack(1, 120)
    messages.publish_status("progress", "checking   auction prices", question=(77, 1), tokens=3400)
    assert frame() == bell.progress(bridge.STEP_CODES.index("checking auction prices"), 3400)
    messages.publish_status("progress", "working it out")
    assert frame() == bell.progress(1, None)
    assert transport().load_state() == {"seq": 0}, "no slot, no payload"
    with pytest.raises(ValueError, match="status must be one of ack, progress"):
        messages.publish_status("done", question=(77, 1))
    with pytest.raises(ValueError, match="an ack names the question"):
        messages.publish_status("ack")


def test_token_counts_in_the_payload(addons):
    """Claude Code's answers say how long they took and the exact count. An --agent's carry no count, and other
    messages never do."""
    from wow_mcp.app import transport
    from wow_mcp.tools import messages

    messages.publish_message("One.", reply=True, question=(77, 1), took=250.4, tokens=12345)
    messages.publish_message("Two.", reply=True, question=(77, 2), took=42)
    messages.publish_message("Heads up.", took=3, tokens=5)
    sent = transport().load_state()["messages"]
    assert [(m["text"], m.get("took"), m.get("tokens")) for m in sent] == [
        ("One.", 250, 12345), ("Two.", 42, None), ("Heads up.", None, None)]


def test_token_count(game):
    """With Claude Code, the status line also says how many tokens the AI has written, after an arrow, like Claude
    Code's spinner. The bridge's notes come every second or two; between them the count climbs at the pace of the last
    two, for 5 s at most, and the next one's count takes its place."""
    game(r"""
        local ns = client.LoadAddon()
        Asks(ns)
        Settle()
        client.Type("/ai any good flips near me?")
        local tab = Tab()
        local function Status()
            local lines = client.Lines(tab)
            return lines[#lines]
        end
        local function Shows(text) return Status():find(text, 1, true) ~= nil end
        local function Written() return Status():match(ARROW:gsub("%p", "%%%0") .. " (%S+) tokens") end

        -- The ack: nothing written yet, so nothing to show.
        Note("ack", 1, nil, 120)
        client.Wait(20)
        assert(Status():match("^|cffababab" .. "Thinking… 0:1[0-3]|r$"), Status())

        -- A note: the count so far, between the clock and the step; as it is, with no pace to go on yet.
        Note("progress", 1, "working it out", nil, Counted(3400))
        assert(Status():match("^|cffababab" .. "Thinking… 0:1%d · |A:"), Status())
        assert(Shows(" · " .. ARROW .. " 3.4k tokens · working it out|r"), Status())
        client.Wait(8)
        assert(Written() == "3.4k", Status())
        -- The next: it climbs at the pace since the last one...
        Note("progress", 1, "working it out", nil, Counted(4400))
        assert(Written() == "4.4k", Status())
        client.Wait(4)
        local early = Written()
        assert(early ~= "4.4k", Status())
        -- ... for 5 s at most.
        client.Wait(8)
        local most = Written()
        client.Wait(20)
        assert(Written() == most and most ~= early, most)
        assert(Count(client.Lines(tab), STATUS) == 1)

        -- The next note's count takes its place, also when it's less than the estimate; it stays put then.
        Note("progress", 1, "checking auction prices", nil, Counted(4100))
        assert(Shows(" · " .. ARROW .. " 4.1k tokens · checking auction prices|r"), Status())
        client.Wait(10)
        assert(Shows(" · " .. ARROW .. " 4.1k tokens · checking auction prices|r"), Status())

        -- Too long without news: the warning is all the line says, as before; news brings the count back.
        client.Wait(400)
        assert(Status() == STATUS .. STOPPED .. "|r", Status())
        Note("progress", 1, "writing", nil, Counted(5200))
        assert(Shows(" · " .. ARROW .. " 5.2k tokens · writing|r"), Status())
        AnswerTo(1, "Nothing worth flipping here.", nil, '"took":420,"tokens":5300')
        assert(Count(client.Lines(tab), STATUS) == 0)

        -- An agent of the player's own (--agent) streams nothing to count: no count, no arrow.
        client.Type("/ai a slow one?")
        Note("ack", 2, nil, 1800)
        Note("progress", 2, "working it out")
        client.Wait(20)
        assert(Thinking(Status()) and not Shows("|A:") and not Shows("tokens"), Status())
    """)


def test_token_counts_are_cut_short(game):
    """As Claude Code says them. (The bell carries a count exactly as it's shown up to 99.9k, bell.tokens_code; here
    they go to the status line as they are, and with no pace, so they don't run on.)"""
    game(r"""
        local ns = client.LoadAddon()
        Asks(ns)
        Settle()
        client.Type("/ai how many?")
        local tab = Tab()
        for _, case in ipairs({ { 1, "1 token" }, { 850, "850 tokens" }, { 999, "999 tokens" }, { 1000, "1k tokens" },
                                { 3400, "3.4k tokens" }, { 3449, "3.4k tokens" }, { 3450, "3.5k tokens" },
                                { 12000, "12k tokens" }, { 12345, "12.3k tokens" }, { 99949, "99.9k tokens" },
                                { 999949, "999.9k tokens" }, { 999950, "1M tokens" }, { 1234567, "1.2M tokens" },
                                { 12500000, "12.5M tokens" } }) do
            ns.AIChat.Status({ status = "progress", step = 0, tokens = case[1], rate = 0 })
            client.Wait()
            local lines = client.Lines(tab)
            assert(lines[#lines]:find(" · " .. ARROW .. " " .. case[2] .. "|r", 1, true), case[2] .. ": " .. lines[#lines])
        end
    """)


def test_line_after_the_answer(game):
    """Under each answer, in the AI tab only, a grey line: how long it took and how many tokens the AI wrote, the
    bridge's exact count. An --agent's answer has no count, so just the time; other replies get no line."""
    game(r"""
        local ns = client.LoadAddon()
        Asks(ns)
        Settle()
        client.Type("/ai any good flips near me?")
        local tab = Tab()
        Note("ack", 1, nil, 120, Counted(0))
        Note("progress", 1, "working it out", nil, Counted(3400))
        AnswerTo(1, "Nothing worth flipping here.\\nTry again after the patch.", nil, '"took":250,"tokens":12345')
        local lines = client.Lines(tab)
        assert(lines[#lines - 2] == "|cff33ff99[Claude]|r Nothing worth flipping here.", table.concat(lines, "\n"))
        assert(lines[#lines - 1] == "    Try again after the patch.", lines[#lines - 1])
        assert(lines[#lines] == "|cffaaaaaatook 4:10 · " .. ARROW .. " 12.3k tokens|r", lines[#lines])
        assert(Count(lines, STATUS) == 0)
        client.Wait(20)
        assert(client.Lines(tab)[#lines] == lines[#lines] and Count(client.Lines(tab), STATUS) == 0)

        -- An --agent's answer: how long it took, no count.
        client.Type("/ai a slow one?")
        Note("ack", 2, nil, 1800)
        AnswerTo(2, "Done.", nil, '"took":42')
        lines = client.Lines(tab)
        assert(lines[#lines - 1] == "|cff33ff99[Claude]|r Done." and lines[#lines] == "|cffaaaaaatook 0:42|r", lines[#lines])

        -- Other replies (the bridge was busy), and answers from a bridge from before: no line.
        AnswerTo(9, "Still busy with your earlier questions.")
        Answer("From before.")
        lines = client.Lines(tab)
        assert(lines[#lines - 1] == "|cff33ff99[Claude]|r Still busy with your earlier questions.", lines[#lines - 1])
        assert(lines[#lines] == "|cff33ff99[Claude]|r From before.", lines[#lines])

        -- The conversation in the main window too (/mcp aitab both): the line is the tab's only; and none in the main
        -- window alone.
        SlashCmdList.WOWMCP("aitab both")
        client.Type("/ai both?")
        AnswerTo(3, "Both ways.", nil, '"took":5,"tokens":850')
        lines = client.Lines(tab)
        assert(lines[#lines] == "|cffaaaaaatook 0:05 · " .. ARROW .. " 850 tokens|r", lines[#lines])
        assert(Has(client.chat, "[Claude]|r Both ways.") and not Has(client.chat, "took "))
        SlashCmdList.WOWMCP("aitab off")
        client.Type("/ai off?")
        AnswerTo(4, "Off.", nil, '"took":5,"tokens":850')
        assert(Has(client.chat, "[Claude]|r Off.") and not Has(client.chat, "took ") and #client.Lines(tab) == #lines)
    """)


def test_token_counts_of_several_questions(game):
    """Each question has its own count: the status line has the oldest one's, and the next one's starts afresh."""
    game(r"""
        local ns = client.LoadAddon()
        Asks(ns)
        Settle()
        client.Type("/ai first?")
        local tab = Tab()
        local function Status()
            local lines = client.Lines(tab)
            return lines[#lines]
        end
        client.Type("/ai second?")
        Note("ack", 1, nil, nil, Counted(0))
        Note("progress", 1, "checking auction prices", nil, Counted(2000))
        Note("ack", 2, nil, nil, Counted(0)) -- arrives while the first is answered: it doesn't touch its count
        assert(Status():find(" · " .. ARROW .. " 2k tokens · checking auction prices|r", 1, true), Status())

        -- The first answered: its line, then the status line for the second, with nothing written for it yet.
        AnswerTo(1, "One.", nil, '"took":61,"tokens":2500')
        local lines = client.Lines(tab)
        assert(lines[#lines - 2] == "|cff33ff99[Claude]|r One.", table.concat(lines, "\n"))
        assert(lines[#lines - 1] == "|cffaaaaaatook 1:01 · " .. ARROW .. " 2.5k tokens|r", lines[#lines - 1])
        assert(Status():match("^|cffababab" .. "Thinking… 0:0%d|r$"), Status())
        Note("progress", 2, "searching the web", nil, Counted(900))
        assert(Status():find(" · " .. ARROW .. " 900 tokens · searching the web|r", 1, true), Status())
        AnswerTo(2, "Two.", nil, '"took":30,"tokens":1100')
        lines = client.Lines(tab)
        assert(lines[#lines] == "|cffaaaaaatook 0:30 · " .. ARROW .. " 1.1k tokens|r" and Count(lines, STATUS) == 0)
    """)
