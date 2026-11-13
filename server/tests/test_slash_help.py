"""The list of WowMCP's commands over the chat box while typing one (SlashHelp.lua), and /mcp help."""

import pytest

HELPERS = r"""
local function Has(text, part) return text ~= nil and text:find(part, 1, true) ~= nil end

-- Opens the chat box, types into it (as a paste: the whole text at once) and lets a frame pass.
local function Typing(text)
    local box = ACTIVE_CHAT_EDIT_BOX or ChatFrameUtil.OpenChat(nil)
    box:SetText(text)
    client.Tick()
    return box
end
"""


@pytest.mark.parametrize("style", ["classic", "im"])
def test_typing_a_slash_lists_the_commands(run_client, style):
    run_client(HELPERS + r"""
        client.chatStyle = "STYLE"
        local ns = client.LoadAddon()
        client.Wait()
        local Shown = ns.SlashHelp.Shown
        assert(not Shown())

        -- "/" (the key opens the box with it): every /ai and /mcp command, too many for what each does.
        local box = ChatFrameUtil.OpenChat("/")
        client.Tick()
        local list = Shown()
        assert(Has(list, "/ai <question>") and Has(list, "/ai model") and Has(list, "/mcp route") and not Has(list, "[show"), list)
        assert(Has(list, "/mcp status") and Has(list, "/mcp help") and Has(list, "Keep typing"), list)
        assert(not Has(list, "connection details"), list)

        -- Narrowing down: the commands that start with what's typed, with what they do once few.
        Typing("/a")
        list = Shown()
        assert(Has(list, "/ai new") and Has(list, "start a fresh conversation") and not Has(list, "/mcp"), list)
        Typing("/mcp r")
        list = Shown()
        assert(Has(list, "/mcp route") and Has(list, "/mcp remove <n>") and Has(list, "/mcp reminders"), list)
        assert(Has(list, "the route and its navigator") and not Has(list, "/mcp status"), list)
        Typing("/mcp route ")
        list = Shown()
        assert(Has(list, "/mcp route [show, next, prev, <n>, stop]") and not Has(list, "/mcp remove"), list)
        Typing("/mcp route sh")
        assert(Has(Shown(), "/mcp route ["))
        Typing("/mcp 3")
        assert(Has(Shown(), "/mcp <n>") and Has(Shown(), "navigate to marker n") and not Has(Shown(), "/mcp status"))
        -- The other names of the commands; a question after /ai.
        Typing("/wowmcp sta")
        assert(Has(Shown(), "/mcp status") and Has(Shown(), "connection details"))
        Typing("/ask where is the bank")
        assert(Has(Shown(), "/ai <question>") and not Has(Shown(), "/ai new"))
        Typing("/AI MOD")
        assert(Has(Shown(), "/ai model [name]"))

        -- Anything else hides it: other commands, /w (not /wowmcp), plain lines, an unknown /mcp command.
        for _, text in ipairs({ "/cast Fire", "/w", "/s", "hello", "", "/mcp nonsense", "/mcpx" }) do
            Typing(text)
            assert(not Shown(), text .. ": " .. tostring(Shown()))
        end

        -- Sending the line, or leaving the box, hides it.
        Typing("/mcp sta")
        assert(Shown())
        client.TypeLine("/mcp status")
        client.Tick()
        assert(not Shown() and Has(client.chat[#client.chat], "live updates:"))
        box = ChatFrameUtil.OpenChat("/")
        client.Tick()
        assert(Shown())
        box:ClearFocus()
        client.Tick()
        assert(not Shown())
        -- A chat type command typed: Blizzard's box empties itself, the list goes.
        box = ChatFrameUtil.OpenChat("/")
        client.Tick()
        box:SetText("/s ")
        client.Tick()
        assert(not Shown() and box:GetChatType() == "SAY")

        -- What the game hides isn't read.
        issecretvalue = function(value) return value == "<secret>" end
        box.text = "<secret>"
        client.Tick()
        issecretvalue = nil
        assert(not Shown())
    """.replace("STYLE", style))


def test_mcp_help(run_client):
    run_client(HELPERS + r"""
        local ns = client.LoadAddon()
        client.Wait()
        local before = #client.chat
        client.Type("/mcp help")
        local printed = table.concat(client.chat, "\n", before + 1)
        assert(Has(printed, "/ai <question>") and Has(printed, "ask the AI") and Has(printed, "/mcp status"), printed)
        assert(Has(printed, "/mcp aitab") and Has(printed, "connection details"), printed)
        -- An unknown /mcp command still lists the /mcp ones, help among them.
        before = #client.chat
        client.Type("/mcp nonsense")
        printed = table.concat(client.chat, "\n", before + 1)
        assert(Has(printed, "/mcp help - ") and not Has(printed, "/ai <question>"), printed)
        -- /ai alone, as before.
        before = #client.chat
        client.Type("/ai")
        printed = table.concat(client.chat, "\n", before + 1)
        assert(Has(printed, "/ai thinking [level]"), printed)
    """)


def test_other_addons_commands(run_client):
    """Other addons' commands that start with what's typed, by name and addon; not Blizzard's own."""
    run_client(HELPERS + r"""
        local ns = client.LoadAddon()
        client.Wait()
        -- Prat's (as AceConsole registers them), Questie's, and one of Blizzard's: secure.
        SlashCmdList.ACECONSOLE_CLEAR, SLASH_ACECONSOLE_CLEAR1, SLASH_ACECONSOLE_CLEAR2 = function() end, "/clear", "/CLS"
        SlashCmdList.QUESTIE, SLASH_QUESTIE1 = function() end, "/questie"
        SlashCmdList.RELOAD, SLASH_RELOAD1 = function() end, "/reload"
        issecurevariable = function(t, key)
            if t ~= SlashCmdList then return true end
            if key == "RELOAD" then return true end
            return false, key == "QUESTIE" and "Questie" or (key:find("^WOWMCP") and "WowMCP" or "Prat-3.0")
        end
        local Shown = ns.SlashHelp.Shown

        -- "/" alone: ours, and how many others there are.
        Typing("/")
        assert(Has(Shown(), "/mcp status") and Has(Shown(), "And 2 commands of other addons"), Shown())
        assert(not Has(Shown(), "/questie"), Shown())
        -- Typing on: theirs that match, with the addon; ours too when they match.
        Typing("/c")
        local list = Shown()
        assert(Has(list, "/clear, /cls") and Has(list, "(Prat-3.0)") and not Has(list, "/mcp") and not Has(list, "/questie"), list)
        Typing("/q")
        assert(Has(Shown(), "/questie") and Has(Shown(), "(Questie)"), Shown())
        -- Not Blizzard's own, and not ours twice.
        Typing("/re")
        assert(not Shown(), tostring(Shown()))
        Typing("/m")
        list = Shown()
        assert(Has(list, "/mcp status") and not Has(list, "Other addons") and not Has(list, "(WowMCP)"), list)
        -- Their arguments aren't known: past the name, theirs go.
        Typing("/clear ")
        assert(not Shown(), tostring(Shown()))
        issecurevariable = nil
    """)
