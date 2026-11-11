-- WowMCP messages: text the AI sends (payload key "messages", show_message tool) shown in chat,
-- in big letters in the middle of the screen, or both. Also ns.ShowMessage for other features.
-- The bridge's answers to /ai (marked "reply") show in the AI conversation (Chat.lua).

local _, ns = ...

local Messages = {}
ns.Messages = Messages

local SENDER_COLOR = "|cff33ff99"
local CENTER_COLOR = { 0.2, 1, 0.6 }
local CENTER_LINES = 3 -- messages on screen at once; the oldest makes room
local FADE_OUT = 3
local STALE = 300      -- older messages (sent while the game was closed) only go to chat
local SOUND = {        -- SOUNDKIT names, with their IDs in case the table is missing
	chat = { "TELL_MESSAGE", 3081 },
	center = { "RAID_WARNING", 8959 },
}

local recent = {} -- the payload's messages, for /mcp messages

local function PlayKit(kit)
	local id = SOUNDKIT and SOUNDKIT[kit[1]] or kit[2]
	pcall(PlaySound, id, "Master")
end

---------------------------------------------------------------------------
-- Center of the screen: our own frame, styled like a raid warning. Blizzard's
-- RaidWarningFrame also shows boss emotes, which can be secret in Midnight, so
-- writing into it from addon code would taint it.
---------------------------------------------------------------------------

local frame
local shown, free = {}, {} -- shown: { fs, text, age, hold }, oldest first

local function Layout()
	local above
	for _, line in ipairs(shown) do
		line.fs:ClearAllPoints()
		if above then
			line.fs:SetPoint("TOP", above, "BOTTOM", 0, -6)
		else
			line.fs:SetPoint("TOP", frame, "TOP")
		end
		above = line.fs
	end
end

local function Release(i)
	local line = table.remove(shown, i)
	line.fs:Hide()
	table.insert(free, line.fs)
end

local function OnUpdate(_, elapsed)
	local changed = false
	for i = #shown, 1, -1 do
		local line = shown[i]
		line.age = line.age + elapsed
		local alpha = line.age <= line.hold and 1 or 1 - (line.age - line.hold) / FADE_OUT
		if alpha <= 0 then
			Release(i)
			changed = true
		else
			line.fs:SetAlpha(alpha)
		end
	end
	if changed then Layout() end
	if #shown == 0 then frame:SetScript("OnUpdate", nil) end
end

local function ShowCenter(text)
	if not frame then
		frame = CreateFrame("Frame", nil, UIParent)
		frame:SetSize(800, 300)
		-- Just below where the game shows raid warnings, so both can be read.
		frame:SetPoint("TOP", UIParent, "TOP", 0, -290)
		frame:SetFrameStrata("HIGH")
	end
	if #shown >= CENTER_LINES then Release(1) end
	local fs = table.remove(free)
	if not fs then
		fs = frame:CreateFontString(nil, "ARTWORK", "GameFontNormalHuge")
		fs:SetWidth(800)
		fs:SetJustifyH("CENTER")
		fs:SetMaxLines(4)
		fs:SetTextColor(CENTER_COLOR[1], CENTER_COLOR[2], CENTER_COLOR[3])
	end
	fs:SetText(ns.RenderItems(text))
	fs:SetAlpha(1)
	fs:Show()
	-- Long enough to read: 10 s, up to 20 s for long texts.
	table.insert(shown, { fs = fs, text = text, age = 0, hold = math.min(20, math.max(10, #text / 15)) })
	Layout()
	frame:SetScript("OnUpdate", OnUpdate)
end

---------------------------------------------------------------------------
-- Showing a message
---------------------------------------------------------------------------

-- Shows text as it is, like ns.Print: escape text from the AI with ns.Escape first. Its item tokens
-- show as the items (links in chat).
-- style: "chat" (default), "center" or "both". sender: shown in chat (default "WowMCP").
-- sound: a SOUNDKIT ID to play instead of the usual one, or false for none.
-- frames: the chat windows for its chat lines (default: the main one).
function ns.ShowMessage(text, style, sender, sound, frames)
	text = tostring(text or "")
	if style ~= "center" and style ~= "both" then style = "chat" end
	if style ~= "center" then
		for _, frame in ipairs(frames or { DEFAULT_CHAT_FRAME }) do
			local prefix = SENDER_COLOR .. "[" .. ns.Escape(sender or "WowMCP") .. "]|r "
			for line in (text .. "\n"):gmatch("(.-)\r?\n") do
				if line:find("%S") then
					ns.Chat(prefix .. line, frame)
					prefix = "    "
				end
			end
		end
	end
	if style ~= "chat" then ShowCenter(text) end
	if sound then
		pcall(PlaySound, sound, "Master")
	elseif sound == nil then
		PlayKit(style == "chat" and SOUND.chat or SOUND.center)
	end
end

local function Valid(m)
	return type(m) == "table" and type(m.id) == "number" and type(m.text) == "string"
end

local function Sent(m)
	return type(m.at) == "number" and date("%H:%M", m.at) or nil
end

-- m.reply marks the bridge's answers to the player's questions: they belong to the AI conversation,
-- which has its own chat tab (Chat.lua). They also say which model the bridge uses by default, and
-- how hard it thinks, which question they answer (the post it came in), how long the answer took and
-- how many tokens the AI wrote for it.
local function Show(m, text, style)
	if m.reply == true and ns.AIChat then
		ns.AIChat.Answer(text, style, m.from, m.defaultModel, m.defaultEffort, m.session, m.post, m.took, m.tokens)
	else
		ns.ShowMessage(text, style, m.from)
	end
end

-- The payload lists the newest messages; show the ones with ids we haven't shown.
-- WowMCPDB.messageId (the newest shown) survives /reload, so nothing repeats.
-- m.status marks the bridge's notes about a question it's answering: they go to the AI tab's status
-- line (Chat.lua), never into chat or /mcp messages, and only while fresh.
function Messages.Receive(list)
	recent = {}
	local valid, newest = {}, 0
	for _, m in ipairs(list) do
		if Valid(m) then
			table.insert(valid, m)
			if m.status == nil then table.insert(recent, m) end
			newest = math.max(newest, m.id)
		end
	end
	local seen = WowMCPDB.messageId or 0
	if newest < seen then seen = 0 end -- the server's state was reset, so its ids started over
	WowMCPDB.messageId = newest
	local now = time()
	for _, m in ipairs(valid) do
		if m.id > seen and m.status ~= nil then
			if not (type(m.at) == "number" and now - m.at > STALE) and ns.AIChat then ns.AIChat.Status(m) end
		elseif m.id > seen then
			local text = ns.Escape(m.text)
			if type(m.at) == "number" and now - m.at > STALE then
				-- Sent while the game was closed: no flashing it on screen now.
				Show(m, text .. " |cffaaaaaa(" .. Sent(m) .. ")|r", "chat")
			else
				Show(m, text, m.style)
			end
		end
	end
end

function Messages.List()
	if #recent == 0 then
		ns.Print("No messages yet.")
		return
	end
	ns.Print("Recent messages:")
	for i = math.max(1, #recent - 4), #recent do
		local m = recent[i]
		local sent = Sent(m)
		ns.ShowMessage(ns.Escape(m.text) .. (sent and " |cffaaaaaa(" .. sent .. ")|r" or ""), "chat", m.from, false)
	end
end

-- Items that were still loading: their names in the lines on screen.
ns.OnItemsLoaded(function()
	for _, line in ipairs(shown) do line.fs:SetText(ns.RenderItems(line.text)) end
end)

ns.RegisterReceiver("messages", function(list)
	if type(list) == "table" then Messages.Receive(list) end
end)

ns.RegisterCommand("messages", "/mcp messages - repeat the last messages from your AI", Messages.List)
