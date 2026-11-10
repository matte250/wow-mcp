-- Typing in the chat box, the chat windows and their tabs (Chat.lua, Messages.lua, Stream.lua posts).
-- The chat box follows Blizzard's ChatFrameEditBox.lua and ChatFrameUtil.lua (Forever 1.60.1) where
-- the addon can tell the difference: when it shows, gets focus, parses its text, runs slash commands
-- and sends, and the EventRegistry events it triggers on the way.

local function SlashHandler(command)
	for key, handler in pairs(SlashCmdList) do
		local i = 1
		while _G["SLASH_" .. key .. i] do
			if _G["SLASH_" .. key .. i]:upper() == command:upper() then return handler end
			i = i + 1
		end
	end
end

-- Runs a line typed into the chat box: like the client, finds the slash command by the
-- SLASH_<key><n> strings of each SlashCmdList entry.
function client.Type(line)
	local command, rest = line:match("^(/%S+)%s*(.-)$")
	local handler = SlashHandler(command)
	if not handler then error("unknown command: " .. line) end
	return handler(rest)
end

---------------------------------------------------------------------------
-- Chat windows. The client keeps their settings per character and across reloads
-- (client.chatWindows, read with GetChatWindowInfo); ChatFrame1-10 always exist, and a /reload
-- sets them up again from those settings (client.onLoad), as Blizzard's code does at login.
---------------------------------------------------------------------------

local MAX_WINDOWS, RESERVED = 10, 2 -- 1 General, 2 Combat Log
local DEFAULT_GROUPS = { "SAY", "YELL", "GUILD", "WHISPER", "BN_WHISPER", "PARTY", "PARTY_LEADER", "CHANNEL" }

client.chatWindows = {
	{ name = "General", shown = true, docked = 1, messages = { "SAY", "YELL", "GUILD", "WHISPER", "CHANNEL" }, channels = { "General", "Trade" } },
	{ name = "Combat Log", shown = true, docked = 2, messages = { "COMBAT_MISC_INFO" }, channels = {} },
}
for i = RESERVED + 1, MAX_WINDOWS do client.chatWindows[i] = { name = "", shown = false, messages = {}, channels = {} } end
client.chatStyle = "classic" -- the chatStyle setting: one shared chat box ("classic") or one per window ("im")
client.windowsMade = 0       -- windows FCF_OpenNewWindow opened
client.sent = {}             -- chat messages sent: { text, type, target }
client.ran = {}              -- secure commands run: "/CAST Fireball"
client.lastTell = nil        -- who whispered last (R replies to them)
client.channels = { [1] = "General", [2] = "Trade" }

Constants = Constants or {}
Constants.ChatFrameConstants = { MaxChatWindows = MAX_WINDOWS }
ChatTypeInfo = { SAY = { sticky = 1 }, YELL = { sticky = 0 }, PARTY = { sticky = 1 }, GUILD = { sticky = 1 },
                 WHISPER = { sticky = 1 }, BN_WHISPER = { sticky = 1 }, EMOTE = { sticky = 0 }, CHANNEL = { sticky = 1 } }
hash_ChatTypeInfoList = { ["/S"] = "SAY", ["/SAY"] = "SAY", ["/Y"] = "YELL", ["/P"] = "PARTY", ["/G"] = "GUILD",
                          ["/W"] = "WHISPER", ["/WHISPER"] = "WHISPER", ["/R"] = "REPLY", ["/E"] = "EMOTE" }
local SECURE = { ["/CAST"] = true, ["/TARGET"] = true } -- run before anything else, by Blizzard's code only

function GetChatWindowInfo(i)
	local w = client.chatWindows[i]
	if not w then return nil end
	return w.name, 14, 0, 0, 0, 0.25, w.shown, false, w.docked, false
end
function GetChatWindowMessages(i) return table.unpack(client.chatWindows[i].messages) end
function GetChatWindowChannels(i) return table.unpack(client.chatWindows[i].channels) end

-- A window's lines, top to bottom (ChatFrame1's are client.chat).
function client.Lines(frame) return frame.lines end

local ChatWindow = {}
function ChatWindow:GetID() return self.id end
function ChatWindow:GetName() return "ChatFrame" .. self.id end
function ChatWindow:AddMessage(msg) table.insert(self.lines, msg) end
function ChatWindow:RemoveMessagesByPredicate(predicate)
	for i = #self.lines, 1, -1 do
		if predicate(self.lines[i]) then table.remove(self.lines, i) end
	end
end
function ChatWindow:Clear() wipe(self.lines) end
function ChatWindow:IsVisible() return self:IsShown() end

for i = 1, MAX_WINDOWS do
	local frame = CreateFrame("Frame", "ChatFrame" .. i)
	for name, fn in pairs(ChatWindow) do frame[name] = fn end
	frame.id, frame.lines = i, i == 1 and client.chat or {}
	-- Not font strings of the addon's (client.fontStrings), so it gets plain regions.
	local flash = client.NewRegion("Flash", frame)
	flash.FlashAnim = {
		IsPlaying = function(anim) return anim.playing == true end,
		Play = function(anim) anim.playing = true end,
		Stop = function(anim) anim.playing = false end,
	}
	_G["ChatFrame" .. i .. "TabFlash"] = flash
end
DEFAULT_CHAT_FRAME = ChatFrame1

-- Whether the window's tab is flashing (FCF_FlashTab) for news.
function client.TabFlashing(frame) return _G[frame:GetName() .. "TabFlash"].FlashAnim:IsPlaying() end

local function Dock() return GENERAL_CHAT_DOCK.DOCKED_CHAT_FRAMES end

-- Docked windows: only the selected one shows (FCFDock_UpdateTabs).
local function UpdateDock()
	for _, frame in ipairs(Dock()) do frame:SetShown(frame == GENERAL_CHAT_DOCK.selected) end
end

local function SaveDock()
	for index, frame in ipairs(Dock()) do client.chatWindows[frame.id].docked = index end
end

function FCF_IsChatWindowIndexActive(i)
	local frame = _G["ChatFrame" .. i]
	return client.chatWindows[i].shown or (frame and frame.isDocked) or false
end

function FCFDock_GetSelectedWindow(dock) return dock.selected end

-- The tab menu's "New Window": the first window that isn't open, docked and selected in the dock
-- (FCF_DockFrame selects it there, but leaves SELECTED_DOCK_FRAME as it was).
function FCF_OpenNewWindow(name, noDefaultChannels)
	for i = RESERVED + 1, MAX_WINDOWS do
		if not FCF_IsChatWindowIndexActive(i) then
			local frame, w = _G["ChatFrame" .. i], client.chatWindows[i]
			w.name, w.shown, w.channels = name, true, {}
			w.messages = noDefaultChannels and {} or { table.unpack(DEFAULT_GROUPS) }
			frame.name, frame.messageTypeList = name, { table.unpack(w.messages) }
			frame:Clear()
			frame.editBox.history = {}
			table.insert(Dock(), frame)
			frame.isDocked = true
			SaveDock()
			GENERAL_CHAT_DOCK.selected = frame
			UpdateDock()
			ChatFrameUtil.SetLastActiveWindow(frame.editBox)
			client.windowsMade = client.windowsMade + 1
			return frame, i
		end
	end
end

function FCF_SetWindowName(frame, name)
	frame.name = name
	client.chatWindows[frame.id].name = name
end

-- Closing a window: undocked, hidden, and it no longer gets any chat.
function FCF_Close(frame)
	if frame == DEFAULT_CHAT_FRAME then return end
	for i, docked in ipairs(Dock()) do
		if docked == frame then table.remove(Dock(), i) end
	end
	frame.isDocked = nil
	local w = client.chatWindows[frame.id]
	w.docked, w.shown, w.messages, w.channels = nil, false, {}, {}
	SaveDock()
	if GENERAL_CHAT_DOCK.selected == frame then GENERAL_CHAT_DOCK.selected = ChatFrame1 end
	if SELECTED_DOCK_FRAME == frame then SELECTED_DOCK_FRAME = ChatFrame1 end
	frame:Hide()
	frame.messageTypeList = {}
	UpdateDock()
end

function FCF_SelectDockFrame(frame)
	SELECTED_DOCK_FRAME = frame
	_G[frame:GetName() .. "TabFlash"].FlashAnim:Stop()
	GENERAL_CHAT_DOCK.selected = frame
	UpdateDock()
end

-- Blizzard's tab flash for news in a docked window that isn't selected.
function FCF_FlashTab(frame)
	local flash = _G[frame:GetName() .. "TabFlash"]
	if not frame.isDocked or frame == SELECTED_DOCK_FRAME or flash.FlashAnim:IsPlaying() then return end
	flash:SetAlpha(0)
	flash:Show()
	flash.FlashAnim:Play()
end

-- The player clicks a window's tab (FCF_Tab_OnClick).
function client.ClickTab(frame)
	SELECTED_CHAT_FRAME = frame
	if frame.isDocked and GENERAL_CHAT_DOCK.selected ~= frame then FCF_SelectDockFrame(frame) end
	if client.chatStyle ~= "classic" then ChatFrameUtil.SetLastActiveWindow(frame.editBox) end
end

---------------------------------------------------------------------------
-- The chat box (ChatFrameEditBox.lua). Each window has one; its methods are its own, as the XML
-- mixin copies them, so hooksecurefunc(box, ...) hooks one box.
---------------------------------------------------------------------------

local EditBox = {}

function EditBox:GetChatType() return self.attributes.chatType end
function EditBox:SetChatType(chatType) self.attributes.chatType = chatType end
function EditBox:GetStickyType() return self.attributes.stickyType end
function EditBox:SetStickyType(chatType) self.attributes.stickyType = chatType end
function EditBox:GetTellTarget() return self.attributes.tellTarget end
function EditBox:SetTellTarget(target) self.attributes.tellTarget = target end
function EditBox:GetChannelTarget() return self.attributes.channelTarget or 0 end
function EditBox:SetChannelTarget(target) self.attributes.channelTarget = target end

-- Widget methods. Setting the text runs the box's OnTextChanged, as in the client.
function EditBox:GetText() return self.text end
function EditBox:SetText(text)
	self.text = text
	self:OnTextChanged()
end
function EditBox:AddHistoryLine(line) table.insert(self.history, line) end
function EditBox:IsShown() return self.shown end
function EditBox:IsVisible() return self.shown end
function EditBox:HasFocus() return self.focus end
function EditBox:Show()
	if self.shown then return end
	self.shown = true
	self:OnShow()
end
function EditBox:Hide()
	if not self.shown then return end
	self.shown = false
	self:ClearFocus()
	self:OnHide()
end
function EditBox:SetFocus()
	if self.focus then return end
	if client.focus and client.focus ~= self then client.focus:ClearFocus() end
	self.focus, client.focus = true, self
	self:OnEditFocusGained()
end
function EditBox:ClearFocus()
	if not self.focus then return end
	self.focus = false
	if client.focus == self then client.focus = nil end
	self:OnEditFocusLost()
end

-- Scripts
function EditBox:OnShow()
	EventRegistry:TriggerEvent("ChatFrame.OnEditBoxShow", self)
	self:ResetChatType()
end
function EditBox:OnHide()
	EventRegistry:TriggerEvent("ChatFrame.OnEditBoxHide", self)
	if ACTIVE_CHAT_EDIT_BOX == self then ChatFrameUtil.DeactivateChat(self) end
end
function EditBox:OnEditFocusGained()
	EventRegistry:TriggerEvent("ChatFrame.OnEditBoxFocusGained", self)
	ChatFrameUtil.ActivateChat(self)
end
function EditBox:OnEditFocusLost()
	EventRegistry:TriggerEvent("ChatFrame.OnEditBoxFocusLost", self)
	if self:GetText() == "" then ChatFrameUtil.DeactivateChat(self) end
end
function EditBox:OnTextChanged() self:ParseText(0) end
function EditBox:OnEnterPressed() self:SendMessage() end
function EditBox:OnPreSendText() EventRegistry:TriggerEvent("ChatFrame.OnEditBoxPreSendText", self) end

function EditBox:UpdateHeader() self.header:SetText((self:GetChatType() or "") .. ":") end
function EditBox:ResetChatTypeToSticky() self:SetChatType(self:GetStickyType()) end
function EditBox:ResetChatType()
	if self:GetChatType() == "PARTY" and not client.inGroup then self:SetChatType("SAY") end
	self:UpdateHeader()
end

function EditBox:ProcessChatType(msg, index, send)
	if index == "WHISPER" then
		local target, rest = msg:match("^(%S+)%s(.*)$") -- not done until there's a space after the name
		if target then
			self:SetTellTarget(target)
			self:SetChatType("WHISPER")
			self:SetText(rest)
			self:UpdateHeader()
		elseif send == 1 then
			self:ClearChat()
		end
	elseif index == "REPLY" then
		if client.lastTell then
			self:SetChatType("WHISPER")
			self:SetTellTarget(client.lastTell)
			self:SetText(msg)
			self:UpdateHeader()
		elseif send == 1 then
			self:ClearChat()
		end
	else
		self:SetChatType(index)
		self:SetText(msg)
		self:UpdateHeader()
	end
	return true
end

function EditBox:HandleChatType(msg, command, send)
	local channel = command:match("^/(%d+)$")
	if channel then
		if client.channels[tonumber(channel)] then
			self:SetChannelTarget(tonumber(channel))
			self:SetChatType("CHANNEL")
			self:SetText(msg)
			self:UpdateHeader()
			return true
		end
	elseif hash_ChatTypeInfoList[command] then
		return self:ProcessChatType(msg, hash_ChatTypeInfoList[command], send)
	end
	return false
end

-- send 0: while typing (chat type commands switch the box); 1: on Enter (commands run).
function EditBox:ParseText(send)
	local text = self:GetText()
	if text == "" or text:sub(1, 1) ~= "/" then return end
	if send ~= 1 and not text:find("%s") then return end
	local command = text:match("^(/[^%s]+)") or ""
	local msg = ""
	if command ~= text then msg = text:sub(#command + 2):match("^%s*(.*)$") end
	command = command:upper()
	if send == 1 and SECURE[command] then
		table.insert(client.ran, command .. " " .. strtrim(msg))
		self:AddHistoryLine(text)
		self:ClearChat()
		return
	end
	if self:HandleChatType(msg, command, send) then return end
	if send == 0 then return end
	local handler = SlashHandler(command)
	if handler then
		handler(strtrim(msg), self)
		self:AddHistoryLine(text)
		self:ClearChat()
		return
	end
	self.chatFrame:AddMessage("Type '/help' for a listing of a few commands.")
	self:ClearChat()
end

function EditBox:SendText(addHistory)
	self:ParseText(1)
	self:OnPreSendText()
	local chatType, text = self:GetChatType(), self:GetText()
	if text:find("%s*[^%s]+") then
		local target
		if chatType == "WHISPER" or chatType == "BN_WHISPER" then
			target = self:GetTellTarget()
			client.lastTold = target
		elseif chatType == "CHANNEL" then
			target = self:GetChannelTarget()
		end
		table.insert(client.sent, { text = text, type = chatType, target = target })
		if addHistory then self:AddHistoryLine(text) end
	end
end

function EditBox:SendMessage()
	self:SendText(1)
	local chatType = self:GetChatType()
	if ChatTypeInfo[chatType] and ChatTypeInfo[chatType].sticky == 1 then self:SetStickyType(chatType) end
	self:ClearChat()
end

function EditBox:ClearChat()
	self:ResetChatTypeToSticky()
	if client.chatStyle ~= "im" then
		self:SetText("")
		self:Hide()
	else
		ChatFrameUtil.DeactivateChat(self)
	end
end

function EditBox:Deactivate()
	if client.chatStyle == "classic" then
		self:Hide()
	else
		self:SetText("")
		self.header:Hide()
		self:ClearFocus()
		self:ResetChatTypeToSticky()
		self:ResetChatType()
	end
end

---------------------------------------------------------------------------
-- ChatFrameUtil: which box a key opens, and the keys and menus that aim it.
---------------------------------------------------------------------------

local Util = {}

function Util.ChooseBoxForSend(preferred)
	local last = LAST_ACTIVE_CHAT_EDIT_BOX
	if client.chatStyle == "classic" then return DEFAULT_CHAT_FRAME.editBox end
	if preferred and preferred:IsShown() then return preferred.editBox end
	if last and last.chatFrame:IsShown() then return last end
	return FCFDock_GetSelectedWindow(GENERAL_CHAT_DOCK).editBox
end

function Util.GetActiveWindow() return ACTIVE_CHAT_EDIT_BOX end

function Util.ActivateChat(box)
	if ACTIVE_CHAT_EDIT_BOX and ACTIVE_CHAT_EDIT_BOX ~= box then Util.DeactivateChat(ACTIVE_CHAT_EDIT_BOX) end
	ACTIVE_CHAT_EDIT_BOX = box
	Util.SetLastActiveWindow(box)
	box:Show()
	box:SetFocus()
	box.header:Show()
	box:UpdateHeader()
end

function Util.DeactivateChat(box)
	if ACTIVE_CHAT_EDIT_BOX == box then ACTIVE_CHAT_EDIT_BOX = nil end
	box:Deactivate()
end

function Util.SetLastActiveWindow(box)
	local previous = LAST_ACTIVE_CHAT_EDIT_BOX
	if previous and previous ~= box and client.chatStyle == "im" then previous:Hide() end
	LAST_ACTIVE_CHAT_EDIT_BOX = box
	if box and client.chatStyle == "im" and ACTIVE_CHAT_EDIT_BOX ~= box then
		box:Show()
		box:Deactivate()
	end
end

-- The OPENCHAT binding (Enter) calls OpenChat(nil); "/" calls OpenChat("/").
function Util.OpenChat(text, chatFrame)
	local box = Util.ChooseBoxForSend(chatFrame)
	Util.ActivateChat(box)
	if text then box:SetText(text) end
	box:UpdateHeader()
	return box
end

-- The REPLY binding (R).
function Util.ReplyTell(chatFrame)
	local box = Util.ChooseBoxForSend(chatFrame)
	if client.lastTell then
		box:SetChatType("WHISPER")
		box:SetTellTarget(client.lastTell)
		box:UpdateHeader()
		if box ~= ACTIVE_CHAT_EDIT_BOX then Util.OpenChat("", chatFrame) end
	end
end

function Util.ReplyTell2(chatFrame)
	local box = Util.ChooseBoxForSend(chatFrame)
	if client.lastTold then
		box:SetChatType("WHISPER")
		box:SetTellTarget(client.lastTold)
		box:UpdateHeader()
		if box ~= ACTIVE_CHAT_EDIT_BOX then Util.OpenChat("", chatFrame) end
	end
end

-- Whispering a friend from the friends list.
function Util.SendBNetTell(name)
	local box = Util.ChooseBoxForSend()
	box:SetTellTarget(name)
	box:SetChatType("BN_WHISPER")
	if box ~= ACTIVE_CHAT_EDIT_BOX then Util.OpenChat("") else box:UpdateHeader() end
end

-- The player presses Enter, types a line (all at once, like a paste) and presses Enter again.
function client.EnterLine(line)
	ChatFrameUtil.OpenChat(nil)
	client.TypeLine(line)
end

-- Types a line into the open chat box and presses Enter.
function client.TypeLine(line)
	local box = assert(ACTIVE_CHAT_EDIT_BOX, "no chat box open")
	box:SetText(line)
	box:OnEnterPressed()
end

-- Blizzard's chat UI as a /reload makes it: the windows from their settings, a chat box each, the
-- dock with General selected, and ChatFrameUtil's functions without any hooks.
table.insert(client.onLoad, function()
	ChatFrameUtil = ChatFrameUtil or {}
	for name, fn in pairs(Util) do ChatFrameUtil[name] = fn end
	GENERAL_CHAT_DOCK = { DOCKED_CHAT_FRAMES = {} }
	ACTIVE_CHAT_EDIT_BOX, LAST_ACTIVE_CHAT_EDIT_BOX, client.focus = nil, nil, nil
	local docked = {}
	for i = 1, MAX_WINDOWS do
		local frame, w = _G["ChatFrame" .. i], client.chatWindows[i]
		frame.name, frame.messageTypeList = w.name, { table.unpack(w.messages) }
		frame.isDocked = w.docked and true or nil
		frame:SetShown(w.shown and not w.docked)
		if w.docked then table.insert(docked, frame) end
		local box = CreateFromMixins(EditBox)
		box.chatFrame, box.attributes, box.text, box.history, box.shown, box.focus = frame, {}, "", {}, false, false
		box.header, box.headerSuffix = client.NewRegion("Header", box), client.NewRegion("Header", box)
		box:SetChatType("SAY")
		box:SetStickyType("SAY")
		frame.editBox = box
		_G["ChatFrame" .. i .. "EditBox"] = box
		_G["ChatFrame" .. i .. "TabFlash"].FlashAnim:Stop()
	end
	table.sort(docked, function(a, b) return client.chatWindows[a.id].docked < client.chatWindows[b.id].docked end)
	GENERAL_CHAT_DOCK.DOCKED_CHAT_FRAMES = docked
	GENERAL_CHAT_DOCK.selected, SELECTED_CHAT_FRAME, SELECTED_DOCK_FRAME = ChatFrame1, ChatFrame1, ChatFrame1
	UpdateDock()
	Util.SetLastActiveWindow(ChatFrame1.editBox)
end)
