-- WowMCP chat: asking the AI without leaving the game. /ai <question> (or /ask), or a line typed in
-- the AI chat tab, goes to the wow-mcp-bridge service as a stream post (Stream.lua); the bridge asks
-- an agent that has the wow tools, and the answer comes back as a message marked as a reply
-- (Messages.lua). The bridge remembers the recent questions and answers, so follow-ups work;
-- /ai new starts afresh.
--
-- The conversation (your question, "Thinking…", the answer, notes about it) has its own chat tab,
-- "AI", so answers don't drown in a city's chat. It's made the first time it's needed, and the game
-- keeps it like any chat window. /mcp aitab picks the tab, the tab and the main chat window, or only
-- the main window.

local _, ns = ...

local AIChat = {}
ns.AIChat = AIChat

local MAX_QUESTION = 500 -- bytes; the chat box allows 255 anyway
local HINT_AFTER = 90    -- seconds without any update before saying the bridge may be down
local TAB_NAME = "AI"
local TAB_DELAY = 5      -- seconds after login before the tab may be made: the chat windows are set up by then
local YOU = "|cff8fc8ff[You]|r "
local NOTE = "|cffaaaaaa"
local THINKING = "|cffaaaaaaThinking…|r"
local LABEL = "|cff33ff99AI:|r"

-- Links shift-clicked into the question become text the agent can read:
-- "[Linen Cloth] (item 2589)".
local function PlainText(text)
	text = text:gsub("|H(%a+):(%d+)[^|]*|h(.-)|h", "%3 (%1 %2)"):gsub("|H.-|h(.-)|h", "%1")
	text = text:gsub("|c%x%x%x%x%x%x%x%x", ""):gsub("|cn[^:]*:", ""):gsub("|r", ""):gsub("|T.-|t", "")
	return (text:gsub("||", "|"))
end

---------------------------------------------------------------------------
-- The AI tab
---------------------------------------------------------------------------

-- WowMCPDB.aiTab: { mode = "on" (default) | "both" | "off", made = { ["Name-Realm"] = true } }. The
-- game keeps chat windows per character, so whether we made the tab is kept per character too.
local function Store()
	WowMCPDB.aiTab = WowMCPDB.aiTab or {}
	return WowMCPDB.aiTab
end

local function Mode()
	return Store().mode or "on"
end

local function Character()
	return tostring(UnitName("player")) .. "-" .. tostring(GetRealmName())
end

local settled = false -- TAB_DELAY has passed since login
local told = {}       -- said once per session: the tab is gone, or can't be made
local waiting = 0     -- questions without an answer yet

-- The open chat window named TAB_NAME. Looked up every time, by name: the player can close, rename
-- or undock it, and the game restores chat windows at login.
local function FindTab()
	local count = Constants and Constants.ChatFrameConstants and Constants.ChatFrameConstants.MaxChatWindows or 10
	for i = 1, count do
		local name, _, _, _, _, _, shown = GetChatWindowInfo(i)
		local frame = _G["ChatFrame" .. i]
		if name == TAB_NAME and frame and (shown or frame.isDocked) then return frame end
	end
end

-- Made like the tab menu's "New Window", without the default chat groups and channels, so only the
-- conversation shows there. Blizzard's code runs on the addon's behalf here, so the chat windows'
-- state carries the addon's taint until the next /reload; after that the game restores the window
-- itself. Once per character: a tab the player closed or renamed stays that way until they ask for
-- it again (force, from /mcp aitab).
local function MakeTab(force)
	local store = Store()
	store.made = store.made or {}
	if not force and (not settled or store.made[Character()]) then return nil end
	local ok, frame = pcall(FCF_OpenNewWindow, TAB_NAME, true)
	if not (ok and frame) then
		if not told.full then
			told.full = true
			ns.Print("Couldn't open a chat tab for the AI" .. (ok and " (all 10 chat windows are in use)" or "")
				.. ", so it talks here.")
		end
		return nil
	end
	store.made[Character()] = true
	ns.Chat(NOTE .. "Type here to ask the AI, no /ai needed. Lines starting with / work as usual; "
		.. "/mcp aitab sets where the AI talks.|r", frame)
	return frame
end

-- Where the conversation shows, per /mcp aitab: the tab (or nil) and whether the main chat window
-- too. The tab is made the first time it's needed.
local function Where()
	if Mode() == "off" then return nil, true end
	local tab = FindTab() or MakeTab()
	if not tab then
		if settled and (Store().made or {})[Character()] and not told.gone then
			told.gone = true
			ns.Print("The AI chat tab is closed or renamed, so the AI talks here. /mcp aitab brings the tab back; "
				.. "/mcp aitab off keeps the AI here.")
		end
		return nil, true
	end
	return tab, Mode() == "both"
end

-- News in the tab while it isn't showing: it flashes like a chat tab with new messages, until it's
-- selected. FCF_FlashTab only plays the tab's flash animation; it changes no state.
local function Flash(tab)
	if not tab:IsVisible() and FCF_FlashTab then pcall(FCF_FlashTab, tab) end
end

-- A line in the tab, already escaped.
local function TabLine(tab, text)
	ns.Chat(text, tab)
	Flash(tab)
end

-- "Thinking…" at the bottom of the tab while an answer is due, one at a time. Chat windows let
-- addons remove lines (ScrollingMessageFrameSecureMixin), so that doesn't taint them. Found
-- within the line, as chat addons may add timestamps; pcall: another addon's line there could be
-- secret.
local function Unthink(tab)
	if tab.RemoveMessagesByPredicate then
		pcall(tab.RemoveMessagesByPredicate, tab, function(message)
			return type(message) == "string" and message:find(THINKING, 1, true) ~= nil
		end)
	end
end

local function Think(tab)
	Unthink(tab)
	ns.Chat(THINKING, tab)
end

-- A note about the conversation, already escaped: in the tab, and as an addon line in the main window.
local function Say(text)
	local tab, main = Where()
	if tab then TabLine(tab, NOTE .. text .. "|r") end
	if main then ns.Print(text) end
end

-- An answer from the bridge (Messages.lua). "Thinking…" moves below it while more are due.
function AIChat.Answer(text, style, sender)
	local tab, main = Where()
	local frames = {}
	if tab then frames[1] = tab end
	if main then frames[#frames + 1] = DEFAULT_CHAT_FRAME end
	ns.ShowMessage(text, style, sender, nil, frames)
	waiting = math.max(0, waiting - 1)
	if tab then
		Unthink(tab)
		if waiting > 0 then Think(tab) end
		Flash(tab)
	end
end

---------------------------------------------------------------------------
-- Asking
---------------------------------------------------------------------------

local function Ask(input)
	local typed = strtrim(input or "")
	if typed == "" then
		Say("/ai <question> - ask the AI (or type in its chat tab), e.g. /ai where can I learn leatherworking?")
		Say("/ai new - start a fresh conversation (it remembers your recent questions for follow-ups)")
		return
	end
	if typed:lower() == "new" then
		-- Nothing comes back for this, so it costs no live-update slot.
		ns.Stream.Post({ reset = true, player = UnitName("player") })
		Say("Starting a fresh conversation.")
		return
	end
	local question = PlainText(typed)
	if #question > MAX_QUESTION then -- drop the (UTF-8) character that crosses the limit
		question = question:sub(1, MAX_QUESTION + 1):gsub("[\1-\127\192-\255][\128-\191]*$", "")
	end
	ns.Stream.Post({ question = question, player = UnitName("player") })
	local tab, main = Where()
	if tab then TabLine(tab, YOU .. typed) end
	if main then ns.Print("Asking… " .. typed) end
	if ns.run.liveProblem then
		Say("Live updates are off (" .. ns.run.liveProblem .. "), so the answer will only show after /reload.")
		return
	end
	waiting = waiting + 1
	if tab then Think(tab) end
	local seq = ns.run.seq
	C_Timer.After(HINT_AFTER, function()
		if ns.run.seq == seq then
			waiting = 0
			local current = FindTab()
			if current then Unthink(current) end
			Say("Still no answer. If none comes, check that the wow-mcp-bridge service is running.")
		end
	end)
end

-- Neither is a command of the default UI.
SLASH_WOWMCPAI1 = "/ai"
SLASH_WOWMCPAI2 = "/ask"
SlashCmdList.WOWMCPAI = Ask

---------------------------------------------------------------------------
-- Typing in the AI tab: a line typed there is a question, no /ai needed.
--
-- The chat box is Blizzard's, and it also runs secure commands (/cast, /target, ...), so the addon
-- doesn't write its fields or replace its code. It listens instead:
-- * EventRegistry's chat box events. Each callback runs through securecallfunction, so nothing done
--   in it can taint the box's own code that runs after it.
-- * hooksecurefunc post-hooks, which leave the hooked code as secure as it was.
-- Just before sending a line, the box tells addons (ChatFrame.OnEditBoxPreSendText, there for them
-- to edit the text). A line typed in the AI tab is taken then and the box emptied (SetText), so
-- nothing is sent. Any slash command on the line has already run by then. A line the player aimed
-- somewhere (/s, /w Name, /p, /1, R to reply, a whisper or channel from a menu) goes there as usual.
---------------------------------------------------------------------------

local open        -- the chat box opened to type into the AI tab: { box, aimed = sent elsewhere }
local hooked = {} -- chat boxes whose chat type commands we watch
local label       -- "AI:" over the chat type header ("Say:", "To Thorgrim:") while a line goes to the AI

-- The box's text, or nil while the game keeps it secret.
local function Text(box)
	local text = box:GetText()
	if issecretvalue and issecretvalue(text) then return nil end
	return text
end

-- Whether a chat box opened now types into the AI tab: the tab's own box ("IM style" chat gives each
-- window one), or the shared one while the tab is the selected one ("classic" style).
local function InTab(box)
	local tab = Mode() ~= "off" and FindTab()
	if not tab then return false end
	if box.chatFrame == tab then return true end
	return tab.isDocked and box.chatFrame and box.chatFrame.isDocked and GENERAL_CHAT_DOCK and FCFDock_GetSelectedWindow
		and FCFDock_GetSelectedWindow(GENERAL_CHAT_DOCK) == tab or false
end

-- "AI:" in place of the chat type header while the line being typed goes to the AI. Our own text on
-- top; the header is only made see-through meanwhile.
local function UpdateLabel()
	local box = open and not open.aimed and open.box
	if box and not (box:HasFocus() and box:IsVisible() and box.header) then box = nil end
	if label and label.box and label.box ~= box then
		label.box.header:SetAlpha(1)
		if label.box.headerSuffix then label.box.headerSuffix:SetAlpha(1) end
		label.box = nil
		label:Hide()
	end
	if not box then return end
	if not label then
		label = CreateFrame("Frame", nil, UIParent)
		label:SetFrameStrata("FULLSCREEN") -- above the chat box (DIALOG)
		label:SetSize(1, 1)
		label:SetPoint("CENTER")
		label.text = label:CreateFontString(nil, "OVERLAY", "ChatFontNormal")
		label.text:SetText(LABEL)
	end
	label.text:ClearAllPoints()
	label.text:SetPoint("LEFT", box.header, "LEFT")
	box.header:SetAlpha(0)
	if box.headerSuffix then box.headerSuffix:SetAlpha(0) end
	label.box = box
	label:Show()
end

local function Aim()
	if open then
		open.aimed = true
		UpdateLabel()
	end
end

-- A chat type command the box understood (/s, /w Name, /1, ...): the line goes there.
local function OnChatType(box, _, command)
	if not (open and open.box == box) then return end
	if issecretvalue and issecretvalue(command) then return Aim() end
	if type(command) == "string" and (command:match("^/%d+$") or hash_ChatTypeInfoList and hash_ChatTypeInfoList[command]) then
		Aim()
	end
end

-- A new line starts when the box gets focus while empty; focus back on a half-typed line keeps it.
-- Never without the chat type hook: then /s and the like couldn't be told apart.
local function OnFocus(_, box)
	if Text(box) == "" then
		open = InTab(box) and { box = box } or nil
		if open and not hooked[box] then
			hooked[box] = pcall(hooksecurefunc, box, "HandleChatType", OnChatType)
			if not hooked[box] then open = nil end
		end
	end
	UpdateLabel()
end

local function OnSend(_, box)
	if not (open and open.box == box and not open.aimed) then return end
	local text = Text(box)
	if text and not text:find("%S") then return end
	box:SetText("") -- first, so nothing below can let the line through to the chat
	if not text then
		Say("The game is hiding what you typed just now, so it wasn't sent anywhere.")
		return
	end
	box:AddHistoryLine("/ai " .. text) -- the up arrow brings it back, as a question from any tab
	Ask(text)
end

if EventRegistry then
	EventRegistry:RegisterCallback("ChatFrame.OnEditBoxFocusGained", OnFocus, AIChat)
	EventRegistry:RegisterCallback("ChatFrame.OnEditBoxFocusLost", UpdateLabel, AIChat)
	EventRegistry:RegisterCallback("ChatFrame.OnEditBoxHide", UpdateLabel, AIChat)
	EventRegistry:RegisterCallback("ChatFrame.OnEditBoxPreSendText", OnSend, AIChat)
end

-- Aiming a line without a chat type command: the reply keys, whispering a friend from the friends
-- list, a chat type picked in the chat menu.
for _, name in ipairs({ "ReplyTell", "ReplyTell2", "SendBNetTell" }) do
	if ChatFrameUtil and ChatFrameUtil[name] then hooksecurefunc(ChatFrameUtil, name, Aim) end
end
if ChatFrameMenuButtonMixin and ChatFrameMenuButtonMixin.SetChatTypeAttribute then
	hooksecurefunc(ChatFrameMenuButtonMixin, "SetChatTypeAttribute", Aim)
end

---------------------------------------------------------------------------
-- /mcp aitab
---------------------------------------------------------------------------

local USAGE = "/mcp aitab [on, both, off] - where the AI conversation shows: its own chat tab (on, the default), "
	.. "there and in the main chat window (both), or only in the main window (off); brings a closed tab back"

function AIChat.Command(rest)
	local mode = strtrim(tostring(rest or "")):lower()
	if mode ~= "" and mode ~= "on" and mode ~= "both" and mode ~= "off" then
		ns.Print(USAGE)
		return
	end
	if mode ~= "" then Store().mode = mode end
	if Mode() == "off" then
		ns.Print("The AI talks in the main chat window. /mcp aitab on gives it its own chat tab again.")
		return
	end
	told.full = nil -- asked for, so say again if it can't be made
	if not (FindTab() or MakeTab(true)) then return end
	told.gone = nil
	if Mode() == "both" then
		ns.Print("The AI talks in its chat tab and here. /mcp aitab on: only in its tab; off: only here.")
	else
		ns.Print("The AI talks in its chat tab. /mcp aitab both: also here; off: only here.")
	end
end

ns.RegisterCommand("aitab", USAGE, AIChat.Command)

ns.OnLogin(function()
	C_Timer.After(TAB_DELAY, function() settled = true end)
end)
