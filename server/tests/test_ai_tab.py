"""The AI conversation's own chat tab (Chat.lua, Messages.lua): made once, when first needed; the
question, "Thinking…" and the answer there, per /mcp aitab; typing there asks the AI."""

import sys

import pytest

# How the Lua test scripts publish, through the real tools: answers to /ai are replies.
PUBLISH_SCRIPT = """
import json, sys
from wow_mcp.tools import messages
args = json.loads(sys.argv[1])
messages.publish_message(args["text"], args.get("style", "chat"), args.get("sender", "Claude"),
                         reply=args.get("reply", False))
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

-- The "AI:" label over the chat box's header.
local function Labelled(box)
    for _, fs in ipairs(client.fontStrings) do
        if fs.text == "|cff33ff99AI:|r" then
            return fs.parent:IsShown() and box.header:GetAlpha() == 0
        end
    end
    return false
end

local YOU, THINKING = "|cff8fc8ff[You]|r ", "|cffaaaaaaThinking…|r"
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
        assert(lines[2] == YOU .. "where is the bank?" and lines[3] == THINKING and #lines == 3, table.concat(lines, "\n"))
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
        assert(lines[#lines - 1] == "|cff33ff99[Claude]|r One." and lines[#lines] == THINKING, table.concat(lines, "\n"))
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

        -- No answer for a while: "Thinking…" gives way to a hint about the bridge.
        client.Type("/ai anyone?")
        client.Wait(182)
        lines = client.Lines(tab)
        assert(not Has(lines, "Thinking") and lines[#lines]:find("Still no answer", 1, true), lines[#lines])
    """)


def test_bridge_answers_are_replies(addons):
    from wow_mcp.app import transport
    from wow_mcp.tools import messages

    messages.publish_message("An answer.", reply=True)
    messages.publish_message("A heads-up.")
    sent = transport().load_state()["messages"]
    assert [(m["text"], m.get("reply")) for m in sent] == [("An answer.", True), ("A heads-up.", None)]


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
