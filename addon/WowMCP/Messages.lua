-- WowMCP messages: text the AI sends (payload key "messages", show_message tool) shown in chat,
-- in big letters in the middle of the screen, or both. Also ns.ShowMessage for other features.
-- The bridge's answers to /ai (marked "reply") show in the AI conversation (Chat.lua).
-- Visuals the AI sends (payload key "visuals", visualize tool: tables, lists, texts) show in a window of
-- their own, at the end.

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

---------------------------------------------------------------------------
-- Visuals: what the AI shows in a window of our own (payload key "visuals", visualize tool): a title
-- and a stack of blocks, top to bottom. The block types are a registry (BLOCKS below): a table, which
-- sorts by a click on a header (by the values: money by copper, items by name); a list of things with
-- values; a text. The stack scrolls as a whole when it's taller than the window can be, and a long
-- table scrolls its own rows, so its header and totals stay in view. The last few visuals are kept to
-- flip back through. A new visual opens the window; a /reload doesn't open it for one already seen.
---------------------------------------------------------------------------

do
	local Visuals = {}
	ns.Visuals = Visuals

	local PAD, GAP, ROW, HEADER, LINE, TITLE = 8, 8, 16, 18, 12, 16 -- GAP: between columns, half on each side
	local HEADING, BLOCK_GAP = 16, 10 -- a block's heading, and the room between blocks
	local ARROW = 10                -- room for the sort arrow in a table's header
	local MIN_COLUMN, MAX_COLUMN = 30, 240
	local MIN_WIDTH, MAX_WIDTH = 240, 1000
	local TEXT_WIDTH, LIST_WIDTH, BULLET = 360, 520, 14
	local BUTTONS, CLOSE = 92, 24   -- room the top right takes: previous, count, next and close; close alone
	local BAR, BAR_GAP, MIN_THUMB = 8, 4, 12
	-- Table rows shown at once; the player can drag the window's corner for more or fewer.
	local ROWS, MIN_ROWS, MOST_ROWS = 15, 3, 30
	local SCROLL_STEP = 3           -- rows per turn of the mouse wheel
	local STACK = 560               -- the most of the stack shown at once, if the screen's height is unknown
	local MAX_BLOCKS, MAX_COLUMNS, MAX_ROWS, MAX_ITEMS = 8, 10, 200, 50
	local TYPES = { text = true, number = true, percent = true, money = true, delta = true, item = true, time = true,
		duration = true }
	local NUMERIC = { number = true, percent = true, money = true, delta = true, time = true, duration = true }
	local MARKS = { gold = { 1, 0.82, 0 }, green = { 0.3, 1, 0.3 }, red = { 1, 0.35, 0.35 }, blue = { 0.35, 0.65, 1 },
		grey = { 0.6, 0.6, 0.6 } }
	local GREEN, RED, GREY, HINT = "|cff33ff33", "|cffff5050", "|cff808080", { 0.4, 0.8, 1 }
	local SORT_ARROW = "auctionhouse-ui-sortarrow" -- the auction house's
	local FONTS = { title = "GameFontNormal", heading = "GameFontHighlight", header = "GameFontNormalSmall",
		cell = "GameFontHighlightSmall" }

	local visuals = {} -- the kept visuals, oldest first: { id, title, subtitle, at, blocks = { block, ... } }
	local current      -- the one the window shows
	local window
	local hovered      -- the row or cell under the mouse
	local tipOwner     -- what the tooltip shows, while it's ours
	local bars = {}    -- the scroll bars: their drags end when the window goes
	local Layout

	-- WowMCPDB.visualWindow: { point, relativePoint, x, y, rows (a table shows at once), open, showing (an id) }.
	local function Saved()
		WowMCPDB.visualWindow = WowMCPDB.visualWindow or {}
		return WowMCPDB.visualWindow
	end

	local function MaxRows()
		local rows = tonumber(Saved().rows)
		return rows and math.max(MIN_ROWS, math.min(MOST_ROWS, math.floor(rows + 0.5))) or ROWS
	end

	-- How the player has a visual: { width (of its stack), scroll (of its stack), blocks = { [n] = what block n
	-- keeps, e.g. a table's sort and its row on top } }, in WowMCPDB.visualViews by its id, so a /reload keeps it.
	local function View(v)
		WowMCPDB.visualViews = WowMCPDB.visualViews or {}
		local view = WowMCPDB.visualViews[v.id]
		if not view then
			view = {}
			WowMCPDB.visualViews[v.id] = view
		end
		view.blocks = view.blocks or {}
		return view
	end

	local function Index(v)
		for i, kept in ipairs(visuals) do
			if kept == v then return i end
		end
	end

	local function Find(id)
		for _, v in ipairs(visuals) do
			if v.id == id then return v end
		end
	end

	-- A text's width in one of FONTS, measured on the window's own font strings.
	local function Measure(font, text)
		if text == "" then return 0 end
		local fs = window.Measures[font]
		fs:SetText(text)
		return fs:GetUnboundedStringWidth() or 0
	end

	---------------------------------------------------------------------------
	-- Values, as table cells and list items have them
	---------------------------------------------------------------------------

	-- 1234567.5 as "1,234,567.5": up to 2 decimals (or as many as asked), and never 0 for what isn't.
	local function Number(value, decimals)
		local text = string.format("%." .. (decimals or 2) .. "f", value)
		if not decimals then
			text = text:gsub("(%.%d-)0+$", "%1"):gsub("%.$", "")
			if tonumber(text) == 0 and value ~= 0 then text = string.format("%.2g", value) end -- a drop rate of 0.004%
		end
		local sign, digits, rest = text:match("^(-?)(%d+)(.*)$")
		if not digits then return text end
		local separator, n = (LARGE_NUMBER_SEPERATOR or ","):gsub("%%", "%%%%"), 1
		while n > 0 do digits, n = digits:gsub("^(%d+)(%d%d%d)", "%1" .. separator .. "%2") end
		return sign .. digits .. rest
	end

	-- Copper as the game shows money: with coin icons (or as it shows it in colourblind mode).
	local function Coins(copper)
		copper = math.floor(copper + 0.5)
		if GetMoneyString then
			local ok, text = pcall(GetMoneyString, copper, true)
			if ok and type(text) == "string" then return text end
		end
		local g, s, c = math.floor(copper / 10000), math.floor(copper / 100) % 100, copper % 100
		local parts = {}
		if g > 0 then parts[#parts + 1] = Number(g, 0) .. "|cffffd700g|r" end
		if s > 0 then parts[#parts + 1] = s .. "|cffc7c7cfs|r" end
		if c > 0 or #parts == 0 then parts[#parts + 1] = c .. "|cffeda55fc|r" end
		return table.concat(parts, " ")
	end

	-- What a delta changes, without its sign.
	local function Amount(format, value)
		if format.unit == "money" then return Coins(value) end
		return Number(value, format.decimals) .. (format.unit == "percent" and "%" or "")
	end

	local function Delta(format, value)
		if value == 0 then return GREY .. Amount(format, 0) .. "|r" end
		return (value > 0 and GREEN .. "+" or RED .. "-") .. Amount(format, math.abs(value)) .. "|r"
	end

	-- Seconds as "2d 4h", "1h 05m", "3m 20s" or "45s".
	local function Duration(seconds)
		local sign = seconds < 0 and "-" or ""
		seconds = math.floor(math.abs(seconds) + 0.5)
		local days, hours = math.floor(seconds / 86400), math.floor(seconds / 3600) % 24
		local minutes, rest = math.floor(seconds / 60) % 60, seconds % 60
		if days > 0 then return string.format("%s%dd %dh", sign, days, hours) end
		if hours > 0 then return string.format("%s%dh %02dm", sign, hours, minutes) end
		if minutes > 0 then return string.format("%s%dm %02ds", sign, minutes, rest) end
		return string.format("%s%ds", sign, rest)
	end

	-- A unix time in local time: today just the time, else the date too.
	local function When(at)
		at = math.floor(at)
		local now = time()
		if date("%Y%m%d", at) == date("%Y%m%d", now) then return date("%H:%M", at) end
		if date("%Y", at) == date("%Y", now) then return date("%b %d %H:%M", at) end
		return date("%Y-%m-%d", at)
	end

	-- What a value shows, for a font string; format: its type and how to show it ({ type, unit, decimals }).
	-- Text, also text where a number goes (a table's totals can have it), and items go through ns.ItemText:
	-- escaped, with item icons and names.
	local function Shown(format, value)
		if value == nil then return "" end
		if type(value) == "table" then
			return ns.ItemText("{item:" .. value.id .. (value.name and (":" .. value.name) or "") .. "}")
		end
		if type(value) ~= "number" then return ns.ItemText(value) end
		local kind = format.type
		if kind == "money" then return value < 0 and "-" .. Coins(-value) or Coins(value) end
		if kind == "percent" then return Number(value, format.decimals) .. "%" end
		if kind == "delta" then return Delta(format, value) end
		if kind == "time" then return When(value) end
		if kind == "duration" then return Duration(value) end
		return Number(value, format.decimals)
	end

	-- The text a value shows without colours, icons and links, in lower case: what text and items sort by.
	local function SortText(text)
		text = text:gsub("|c%x%x%x%x%x%x%x%x", ""):gsub("|r", ""):gsub("|T.-|t", ""):gsub("|H.-|h", ""):gsub("|h", "")
		return strtrim((text:gsub("||", "|"))):lower()
	end

	-- How a value is shown: { name, type, align, width, decimals, unit } (a table's column, a list's values).
	local function Format(raw)
		local kind = TYPES[raw.type] and raw.type or "text"
		local align = raw.align
		if align ~= "left" and align ~= "center" and align ~= "right" then align = NUMERIC[kind] and "right" or "left" end
		local decimals = tonumber(raw.decimals)
		return {
			name = tostring(raw.name or ""), type = kind, align = align, width = tonumber(raw.width),
			decimals = decimals and math.max(0, math.min(4, math.floor(decimals))) or nil,
			unit = (raw.unit == "money" or raw.unit == "percent") and raw.unit or nil,
		}
	end

	-- A value as the payload has it (a string): a number for number types, { id, name } for items, else
	-- text; nil when empty.
	local function Value(format, raw)
		if raw == nil or raw == "" then return nil end
		raw = tostring(raw)
		if format.type == "item" then
			local id, name = raw:match("^(%d+)$"), nil
			if not id then id, name = raw:match("^(%d+):(.+)$") end
			id = tonumber(id)
			if id and id > 0 then return { id = id, name = name } end
		elseif NUMERIC[format.type] then
			return tonumber(raw) or raw
		end
		return raw
	end

	-- Whether a value shows items the client is still loading (ns.ItemText asked for them).
	local function Waiting(value)
		local ids = {}
		if type(value) == "table" then
			ids[1] = value.id
		elseif type(value) == "string" then
			for id in value:gmatch("{item:(%d+)") do ids[#ids + 1] = tonumber(id) end
		end
		for _, id in ipairs(ids) do
			local ok, name = pcall(C_Item.GetItemNameByID, id)
			if not (ok and name) then return true end
		end
		return false
	end

	-- The first item a text names, for its tooltip.
	local function FirstItem(text)
		return type(text) == "string" and tonumber(text:match("{item:(%d+)")) or nil
	end

	---------------------------------------------------------------------------
	-- What every block uses: tooltips, scroll bars, moving the window
	---------------------------------------------------------------------------

	local function Tip(owner, anchor)
		GameTooltip:SetOwner(owner, anchor or "ANCHOR_TOP")
		tipOwner = owner
	end

	-- Hides the tooltip if it's showing for owner (for any of ours without one).
	local function HideTip(owner)
		if tipOwner ~= nil and (owner == nil or tipOwner == owner) then
			tipOwner = nil
			GameTooltip:Hide()
		end
	end

	-- The item's own tooltip; false if the client can't show it.
	local function ItemTip(id)
		return (pcall(GameTooltip.SetItemByID, GameTooltip, id))
	end

	-- A modified click on an item does what it does on any item: shift links it in chat, ctrl tries it on.
	local function ItemClick(id)
		if not (id and HandleModifiedItemClick) then return end
		local ok, _, link = pcall(C_Item.GetItemInfo, id)
		if ok and type(link) == "string" then pcall(HandleModifiedItemClick, link) end
	end

	local function StartMoving() window:StartMoving() end

	local function StopMoving()
		window:StopMovingOrSizing()
		local point, _, relativePoint, x, y = window:GetPoint(1)
		local saved = Saved()
		saved.point, saved.relativePoint, saved.x, saved.y = point, relativePoint, x, y
	end

	-- A frame in a block that shows a tooltip and moves the window when dragged; enter and leave are its
	-- mouse handlers, enter again when the window is laid out anew under the mouse.
	local function Hoverable(frame, enter, leave, wheel)
		frame:EnableMouse(true)
		frame:EnableMouseWheel(true)
		frame:RegisterForDrag("LeftButton")
		frame.enter, frame.leave = enter, leave
		frame:SetScript("OnEnter", enter)
		frame:SetScript("OnLeave", leave)
		frame:SetScript("OnMouseWheel", wheel)
		frame:SetScript("OnDragStart", StartMoving)
		frame:SetScript("OnDragStop", StopMoving)
	end

	-- A row's backgrounds: every other one shaded, a highlighted one tinted in its colour with a bar of it on
	-- the left, the one under the mouse lighter.
	local function Backgrounds(row)
		row.Back = row:CreateTexture(nil, "BACKGROUND")
		row.Back:SetAllPoints()
		row.Tint = row:CreateTexture(nil, "BORDER")
		row.Tint:SetAllPoints()
		row.Hover = row:CreateTexture(nil, "ARTWORK")
		row.Hover:SetAllPoints()
		row.Hover:SetColorTexture(1, 1, 1, 0.08)
		row.Hover:Hide()
		row.Mark = row:CreateTexture(nil, "OVERLAY")
		row.Mark:SetPoint("TOPLEFT", row, "TOPLEFT", 0, 0)
		row.Mark:SetSize(2, ROW)
	end

	local function MarkRow(row, mark)
		row.Tint:SetShown(mark ~= nil)
		row.Mark:SetShown(mark ~= nil)
		if mark then
			local r, g, b = mark.color[1], mark.color[2], mark.color[3]
			row.Tint:SetColorTexture(r, g, b, 0.15)
			row.Mark:SetColorTexture(r, g, b, 0.9)
		end
	end

	-- A scroll bar: a track with a thumb. Clicking or dragging it calls jump(fraction), 0 the top and 1 the
	-- end; the mouse wheel over it calls wheel(delta).
	local function NewBar(parent, jump, wheel)
		local bar = CreateFrame("Frame", nil, parent)
		bar:SetWidth(BAR)
		bar:EnableMouse(true)
		bar:EnableMouseWheel(true)
		bar.Back = bar:CreateTexture(nil, "BACKGROUND")
		bar.Back:SetAllPoints()
		bar.Back:SetColorTexture(1, 1, 1, 0.06)
		bar.Thumb = bar:CreateTexture(nil, "ARTWORK")
		bar.Thumb:SetColorTexture(1, 1, 1, 0.3)
		local function Follow()
			if not (bar.span and GetCursorPosition and bar.GetEffectiveScale) then return end
			local top, scale = bar:GetTop(), bar:GetEffectiveScale()
			if not (top and scale and scale > 0) then return end
			local _, y = GetCursorPosition()
			jump(math.max(0, math.min(1, (top - y / scale - bar.thumb / 2) / math.max(1, bar.span))))
		end
		function bar.Stop() bar:SetScript("OnUpdate", nil) end
		bar:SetScript("OnMouseDown", function(self, button)
			if button and button ~= "LeftButton" then return end
			Follow()
			self:SetScript("OnUpdate", function()
				-- Let go where the bar couldn't tell: the drag is over.
				if IsMouseButtonDown and not IsMouseButtonDown("LeftButton") then return bar.Stop() end
				Follow()
			end)
		end)
		bar:SetScript("OnMouseUp", bar.Stop)
		bar:SetScript("OnHide", bar.Stop)
		bar:SetScript("OnMouseWheel", function(_, delta) wheel(delta) end)
		bars[#bars + 1] = bar
		return bar
	end

	-- The thumb: seen is the part in view (0-1), at is how far down it is (0-1).
	local function SetBar(bar, height, seen, at)
		bar:SetHeight(height)
		bar.thumb = math.max(MIN_THUMB, math.floor(height * seen))
		bar.span = height - bar.thumb
		bar.Thumb:ClearAllPoints()
		bar.Thumb:SetPoint("TOPLEFT", bar, "TOPLEFT", 0, -math.floor(at * bar.span + 0.5))
		bar.Thumb:SetSize(BAR, bar.thumb)
	end

	---------------------------------------------------------------------------
	-- Block types. BLOCKS[type] = {
	--   parse(raw) -> the block, or nil if there's nothing to show;
	--   prepare(block, measure): works out what it shows, once, and again after items it shows have loaded
	--     if it set block.waiting; measure(font, text) gives a text's width in one of FONTS;
	--   width(block) -> how wide it would be; least(block), if it has one -> the narrowest it can be;
	--   draw(block, holder, width, view) -> its height: lays it out in holder, a frame of its own on the
	--     window's stack (reused for blocks of its type), width wide; view keeps what the player did with
	--     it (e.g. a table's sort) in WowMCPDB.
	-- }
	-- The window draws a block's heading above it. A new type is one entry here and one model in
	-- tools/visuals.py.
	---------------------------------------------------------------------------

	local BLOCKS = {}

	-- Tables: columns of a type each, rows of values. ------------------------

	local function Split(line)
		local cells = {}
		for cell in (line .. "\n"):gmatch("(.-)\n") do cells[#cells + 1] = cell end
		return cells
	end

	-- A row comes as one string, its cells separated by line breaks (tools/visuals.py).
	local function TableRow(columns, line)
		local raw, cells = Split(line), {}
		for c, column in ipairs(columns) do cells[c] = Value(column, raw[c]) end
		return { cells = cells }
	end

	local function TableParse(raw)
		local block = { columns = {}, rows = {} }
		for _, c in ipairs(type(raw.columns) == "table" and raw.columns or {}) do
			if type(c) == "table" and #block.columns < MAX_COLUMNS then block.columns[#block.columns + 1] = Format(c) end
		end
		if #block.columns == 0 then return nil end
		for _, line in ipairs(type(raw.rows) == "table" and raw.rows or {}) do
			if type(line) == "string" and #block.rows < MAX_ROWS then block.rows[#block.rows + 1] = TableRow(block.columns, line) end
		end
		if type(raw.totals) == "string" then block.totals = TableRow(block.columns, raw.totals) end
		for _, h in ipairs(type(raw.highlight) == "table" and raw.highlight or {}) do
			local row = type(h) == "table" and block.rows[tonumber(h.row) or 0]
			if row then
				row.mark = { color = MARKS[h.color] or MARKS.gold, label = type(h.label) == "string" and h.label ~= "" and h.label or nil }
			end
		end
		local sort = type(raw.sort) == "table" and tonumber(raw.sort.column)
		if sort and block.columns[sort] then block.sort = { column = sort, dir = raw.sort.dir == "asc" and "asc" or "desc" } end
		return block
	end

	-- What a row's cells show, what they sort by and how wide they are.
	local function TableFill(block, row, measure)
		row.shown, row.keys, row.widths = {}, {}, {}
		for c, column in ipairs(block.columns) do
			local value = row.cells[c]
			local text = Shown(column, value)
			row.shown[c] = text
			row.keys[c] = type(value) == "number" and value or (value ~= nil and SortText(text) or nil)
			row.widths[c] = measure("cell", text)
			block.widest[c] = math.max(block.widest[c], row.widths[c])
			if Waiting(value) then block.waiting = true end
		end
	end

	local function TablePrepare(block, measure)
		block.widest = {}
		for c, column in ipairs(block.columns) do block.widest[c] = measure("header", ns.ItemText(column.name)) + ARROW end
		for _, row in ipairs(block.rows) do TableFill(block, row, measure) end
		if block.totals then TableFill(block, block.totals, measure) end
		block.order = nil
	end

	local function RowsShown(block)
		return math.min(#block.rows, MaxRows())
	end

	-- The room its own scroll bar takes, when its rows are more than it shows.
	local function TableBar(block)
		return #block.rows > MaxRows() and BAR + BAR_GAP or 0
	end

	-- The columns as wide as their widest cell, within limits, or as wide as the AI said.
	local function Natural(block)
		local natural, total = {}, 0
		for c, column in ipairs(block.columns) do
			natural[c] = math.max(MIN_COLUMN, column.width or math.min(MAX_COLUMN, math.ceil(block.widest[c]) + GAP))
			total = total + natural[c]
		end
		return natural, total
	end

	-- The columns' widths in room: what's above the narrowest a column can be is shared in proportion to
	-- what they need. With room to spare, a column the AI gave a width keeps it.
	local function Fit(block, room)
		local natural, total = Natural(block)
		local grow = room >= total
		local widths, used, flexible, wanted = {}, 0, 0, 0
		for c, column in ipairs(block.columns) do
			if grow and column.width then
				widths[c] = natural[c]
			else
				widths[c], flexible, wanted = MIN_COLUMN, flexible + 1, wanted + natural[c] - MIN_COLUMN
			end
			used = used + widths[c]
		end
		local spare = room - used
		for c, column in ipairs(block.columns) do
			if not (grow and column.width) then
				local share = wanted > 0 and (natural[c] - MIN_COLUMN) * spare / wanted or spare / flexible
				widths[c] = MIN_COLUMN + math.max(0, math.floor(share))
			end
		end
		used = 0
		for c = 1, #widths do used = used + widths[c] end
		widths[#widths] = widths[#widths] + room - used -- what rounding left
		return widths
	end

	-- Which way a first click sorts: text and items A to Z, numbers highest first.
	local function FirstWay(column)
		return NUMERIC[column.type] and "desc" or "asc"
	end

	-- Which way a column is sorted, in words.
	local function Way(column, dir)
		if column.type == "time" then return dir == "asc" and "oldest first" or "newest first" end
		if NUMERIC[column.type] then return dir == "asc" and "lowest first" or "highest first" end
		return dir == "asc" and "A to Z" or "Z to A"
	end

	-- The rows in the order shown (block.order: places to rows): by the view's column, empty cells last
	-- either way and ties as the AI gave them; else as the AI gave them.
	local function TableSort(block, view)
		local order, keys = {}, {}
		for i = 1, #block.rows do order[i] = i end
		local c = view.sort
		if c and block.columns[c] then
			local desc = view.dir == "desc"
			for i, row in ipairs(block.rows) do keys[i] = row.keys[c] end
			table.sort(order, function(a, b)
				local x, y = keys[a], keys[b]
				if x == nil or y == nil then
					if x == nil and y == nil then return a < b end
					return y == nil
				end
				if type(x) ~= type(y) then return type(x) == "number" end -- text in a number column after the numbers
				if x == y then return a < b end
				if desc then return x > y end
				return x < y
			end)
		end
		block.order, block.sortedBy = order, (c or 0) .. (view.dir or "")
	end

	-- The table's rows from first on (the view's row on top).
	local function ScrollRows(holder, first)
		local view = holder.view
		first = math.max(1, math.min(math.floor(first), #holder.block.rows - RowsShown(holder.block) + 1))
		if first == view.first then return end
		view.first = first
		Layout()
	end

	-- The wheel over a table scrolls its rows if they're more than it shows, else the whole stack.
	local function TableWheel(holder, delta)
		if #holder.block.rows > MaxRows() then return ScrollRows(holder, holder.view.first - delta * SCROLL_STEP) end
		Visuals.Scroll(delta)
	end

	-- A cell's tooltip: its item's own, the whole text of one that doesn't fit, why its row is highlighted.
	local function CellEnter(cell)
		hovered = cell
		cell.Row.Hover:Show()
		HideTip(cell)
		local data, block = cell.Row.data, cell.holder.block
		if not (data and block and block.columns[cell.c]) then return end
		local c = cell.c
		local value, mark = data.cells[c], data.mark
		local cut = (data.widths[c] or 0) > (cell.room or 0) + 0.5
		local item = type(value) == "table" and value.id or FirstItem(value)
		local label = mark and mark.label
		if not (item or cut or label) then return end
		Tip(cell, "ANCHOR_RIGHT")
		local told = item and ItemTip(item)
		if told and cut and type(value) ~= "table" then GameTooltip:AddLine(data.shown[c], 1, 1, 1, true) end
		if not told and (item or cut) then
			GameTooltip:SetText(ns.ItemText(block.columns[c].name), 1, 0.82, 0)
			GameTooltip:AddLine(data.shown[c], 1, 1, 1, true)
			told = true
		end
		if label and told then
			GameTooltip:AddLine(ns.ItemText(label), mark.color[1], mark.color[2], mark.color[3], true)
		elseif label then
			GameTooltip:SetText(ns.ItemText(label), mark.color[1], mark.color[2], mark.color[3], 1, true)
		end
		GameTooltip:Show()
	end

	local function CellLeave(cell)
		cell.Row.Hover:Hide()
		if hovered == cell then hovered = nil end
		HideTip(cell)
	end

	local function TableCell(row, c)
		if row.Cells[c] then return row.Cells[c] end
		local cell = CreateFrame("Frame", nil, row)
		cell.Row, cell.holder, cell.c = row, row.holder, c
		cell:SetHeight(ROW)
		Hoverable(cell, CellEnter, CellLeave, function(self, delta) TableWheel(self.holder, delta) end)
		cell:SetScript("OnMouseUp", function(self)
			local value = self.Row.data and self.Row.data.cells[self.c]
			ItemClick(type(value) == "table" and value.id or FirstItem(value))
		end)
		cell.Text = cell:CreateFontString(nil, "ARTWORK", row.totals and "GameFontNormalSmall" or "GameFontHighlightSmall")
		cell.Text:SetPoint("LEFT", cell, "LEFT", GAP / 2, 0)
		cell.Text:SetWordWrap(false)
		row.Cells[c] = cell
		return cell
	end

	local function TableRowFrame(holder, totals)
		local row = CreateFrame("Frame", nil, holder)
		row:SetHeight(ROW)
		row.holder, row.totals, row.Cells = holder, totals, {}
		Backgrounds(row)
		if totals then
			-- Set apart: a gold line above, a lighter background, gold text.
			row.Back:SetColorTexture(1, 1, 1, 0.1)
			row.Line = row:CreateTexture(nil, "OVERLAY")
			row.Line:SetPoint("BOTTOMLEFT", row, "TOPLEFT", 0, 1)
			row.Line:SetPoint("BOTTOMRIGHT", row, "TOPRIGHT", 0, 1)
			row.Line:SetHeight(1)
			row.Line:SetColorTexture(1, 0.82, 0, 0.5)
		end
		return row
	end

	-- place: where the row is in the order shown, for the shading of every other one.
	local function TableFillRow(row, block, data, widths, place)
		row.data = data
		if not row.totals then row.Back:SetColorTexture(1, 1, 1, place % 2 == 0 and 0.05 or 0) end
		MarkRow(row, data and data.mark)
		local x = 0
		for c, column in ipairs(block.columns) do
			local cell = TableCell(row, c)
			cell:ClearAllPoints()
			cell:SetPoint("TOPLEFT", row, "TOPLEFT", x, 0)
			cell:SetWidth(widths[c])
			cell.room = widths[c] - GAP
			cell.Text:SetWidth(cell.room)
			cell.Text:SetJustifyH(column.align == "right" and "RIGHT" or column.align == "center" and "CENTER" or "LEFT")
			cell.Text:SetText(data and data.shown[c] or "")
			cell:Show()
			x = x + widths[c]
		end
		for c = #block.columns + 1, #row.Cells do row.Cells[c]:Hide() end
		row:SetWidth(x)
	end

	local function HeaderEnter(h)
		h.Hover:Show()
		local block, view = h.holder.block, h.holder.view
		local column = block and block.columns[h.c]
		if not column then return end
		Tip(h)
		GameTooltip:SetText(ns.ItemText(column.name), 1, 0.82, 0)
		if view.sort == h.c then
			GameTooltip:AddLine("Sorted " .. Way(column, view.dir) .. ".", 1, 1, 1)
			GameTooltip:AddLine("Click: the other way round. Right-click: back to how it came.", HINT[1], HINT[2], HINT[3], true)
		else
			GameTooltip:AddLine("Click: sort by this column, " .. Way(column, FirstWay(column)) .. ".", HINT[1], HINT[2], HINT[3], true)
		end
		GameTooltip:Show()
	end

	-- A click on a header sorts by its column, or the other way round if it's sorted by it already; a
	-- right-click goes back to how the table came.
	local function SortBy(holder, c, button)
		local block, view = holder.block, holder.view
		local column = block and block.columns[c]
		if not column then return end
		if button == "RightButton" then
			view.sort, view.dir = block.sort and block.sort.column, block.sort and block.sort.dir
		elseif view.sort == c then
			view.dir = view.dir == "asc" and "desc" or "asc"
		else
			view.sort, view.dir = c, FirstWay(column)
		end
		view.first = 1
		Layout()
		if tipOwner == holder.Headers[c] then HeaderEnter(holder.Headers[c]) end
	end

	local function TableHeader(holder, c)
		if holder.Headers[c] then return holder.Headers[c] end
		local h = CreateFrame("Frame", nil, holder)
		h.holder, h.c = holder, c
		h:SetHeight(HEADER)
		h:EnableMouse(true)
		h:EnableMouseWheel(true)
		h:SetScript("OnMouseWheel", function(self, delta) TableWheel(self.holder, delta) end)
		h.Hover = h:CreateTexture(nil, "BACKGROUND")
		h.Hover:SetAllPoints()
		h.Hover:SetColorTexture(1, 1, 1, 0.1)
		h.Hover:Hide()
		h.Text = h:CreateFontString(nil, "ARTWORK", "GameFontNormalSmall")
		h.Text:SetWordWrap(false)
		h.Arrow = h:CreateTexture(nil, "ARTWORK")
		pcall(h.Arrow.SetAtlas, h.Arrow, SORT_ARROW, true)
		h:SetScript("OnMouseUp", function(self, button) SortBy(self.holder, self.c, button) end)
		h:SetScript("OnEnter", HeaderEnter)
		h:SetScript("OnLeave", function(self)
			self.Hover:Hide()
			HideTip(self)
		end)
		holder.Headers[c] = h
		return h
	end

	-- The sort arrow sits on the side away from the name: right of a left-aligned one, left of a right-aligned one.
	local function FillHeader(h, column, width, sorted, dir)
		local right = column.align == "right"
		h:SetWidth(width)
		h.Text:ClearAllPoints()
		h.Text:SetWidth(width - GAP - ARROW)
		h.Text:SetJustifyH(right and "RIGHT" or column.align == "center" and "CENTER" or "LEFT")
		h.Text:SetText(ns.ItemText(column.name))
		h.Arrow:ClearAllPoints()
		if right then
			h.Text:SetPoint("RIGHT", h, "RIGHT", -GAP / 2, 0)
			h.Arrow:SetPoint("LEFT", h, "LEFT", GAP / 2, 0)
		else
			h.Text:SetPoint("LEFT", h, "LEFT", GAP / 2, 0)
			h.Arrow:SetPoint("RIGHT", h, "RIGHT", -GAP / 2, 0)
		end
		h.Arrow:SetShown(sorted)
		-- As the auction house shows it: flipped for ascending.
		if dir == "asc" then h.Arrow:SetTexCoord(0, 1, 1, 0) else h.Arrow:SetTexCoord(0, 1, 0, 1) end
	end

	local function TableDraw(block, holder, width, view)
		holder.block, holder.view = block, view
		holder.Headers, holder.Rows = holder.Headers or {}, holder.Rows or {}
		if not view.ready then
			view.ready, view.first = true, 1
			if block.sort then view.sort, view.dir = block.sort.column, block.sort.dir end
		end
		if not block.order or block.sortedBy ~= (view.sort or 0) .. (view.dir or "") then TableSort(block, view) end
		local count, shown = #block.rows, RowsShown(block)
		view.first = math.max(1, math.min(math.floor(tonumber(view.first) or 1), count - shown + 1))
		local scrolls = count > shown
		local room = width - TableBar(block)
		local widths = Fit(block, room)

		local x = 0
		for c, column in ipairs(block.columns) do
			local h = TableHeader(holder, c)
			h:ClearAllPoints()
			h:SetPoint("TOPLEFT", holder, "TOPLEFT", x, 0)
			FillHeader(h, column, widths[c], view.sort == c, view.dir)
			h:Show()
			x = x + widths[c]
		end
		for c = #block.columns + 1, #holder.Headers do holder.Headers[c]:Hide() end
		local y = HEADER
		for i = 1, shown do
			local row = holder.Rows[i] or TableRowFrame(holder)
			holder.Rows[i] = row
			row:ClearAllPoints()
			row:SetPoint("TOPLEFT", holder, "TOPLEFT", 0, -y)
			TableFillRow(row, block, block.rows[block.order[view.first + i - 1]], widths, view.first + i - 1)
			row:Show()
			y = y + ROW
		end
		for i = shown + 1, #holder.Rows do holder.Rows[i]:Hide() end

		holder.Bar = holder.Bar or NewBar(holder,
			function(at) ScrollRows(holder, 1 + at * (#holder.block.rows - RowsShown(holder.block)) + 0.5) end,
			function(delta) TableWheel(holder, delta) end)
		holder.Bar:SetShown(scrolls)
		if scrolls then
			holder.Bar:ClearAllPoints()
			holder.Bar:SetPoint("TOPLEFT", holder, "TOPLEFT", room + BAR_GAP, -HEADER)
			SetBar(holder.Bar, shown * ROW, shown / count, (view.first - 1) / (count - shown))
		end

		holder.Totals = holder.Totals or TableRowFrame(holder, true)
		holder.Totals:SetShown(block.totals ~= nil)
		if block.totals then
			y = y + 3
			holder.Totals:ClearAllPoints()
			holder.Totals:SetPoint("TOPLEFT", holder, "TOPLEFT", 0, -y)
			TableFillRow(holder.Totals, block, block.totals, widths, 0)
			y = y + ROW
		end
		holder.Status = holder.Status or holder:CreateFontString(nil, "ARTWORK", "GameFontDisableSmall")
		holder.Status:SetShown(scrolls)
		if scrolls then
			holder.Status:ClearAllPoints()
			holder.Status:SetPoint("TOPLEFT", holder, "TOPLEFT", GAP / 2, -(y + 2))
			holder.Status:SetText(string.format("Rows %d-%d of %d", view.first, view.first + shown - 1, count))
			y = y + 2 + LINE
		end
		return y
	end

	BLOCKS.table = {
		parse = TableParse,
		prepare = TablePrepare,
		width = function(block)
			local _, total = Natural(block)
			return total + TableBar(block)
		end,
		least = function(block) return #block.columns * MIN_COLUMN + TableBar(block) end,
		draw = TableDraw,
	}

	-- Lists: bullet or numbered items, each with a value on the right if it has one. -------------

	local function ListEnter(row)
		hovered = row
		row.Hover:Show()
		HideTip(row)
		local item = row.item
		if not item then return end
		local cut = item.width > (row.room or 0) + 0.5
		local id = FirstItem(item.text)
		if not (cut or id) then return end
		Tip(row, "ANCHOR_RIGHT")
		if id and ItemTip(id) then
			if cut then GameTooltip:AddLine(item.shown, 1, 1, 1, true) end
		else
			GameTooltip:SetText(item.shown, 1, 1, 1, 1, true)
		end
		GameTooltip:Show()
	end

	local function ListLeave(row)
		row.Hover:Hide()
		if hovered == row then hovered = nil end
		HideTip(row)
	end

	local function ListRow(holder)
		local row = CreateFrame("Frame", nil, holder)
		row.holder = holder
		row:SetHeight(ROW)
		Backgrounds(row)
		Hoverable(row, ListEnter, ListLeave, function(_, delta) Visuals.Scroll(delta) end)
		row:SetScript("OnMouseUp", function(self) ItemClick(self.item and FirstItem(self.item.text)) end)
		row.Bullet = row:CreateTexture(nil, "ARTWORK")
		row.Bullet:SetSize(4, 4)
		row.Bullet:SetPoint("LEFT", row, "LEFT", 5, 0)
		row.Bullet:SetColorTexture(1, 0.82, 0, 1)
		row.Number = row:CreateFontString(nil, "ARTWORK", "GameFontNormalSmall")
		row.Number:SetPoint("LEFT", row, "LEFT", 0, 0)
		row.Number:SetJustifyH("RIGHT")
		row.Text = row:CreateFontString(nil, "ARTWORK", "GameFontHighlightSmall")
		row.Text:SetJustifyH("LEFT")
		row.Text:SetWordWrap(false)
		row.Value = row:CreateFontString(nil, "ARTWORK", "GameFontHighlightSmall")
		row.Value:SetPoint("RIGHT", row, "RIGHT", -GAP / 2, 0)
		row.Value:SetJustifyH("RIGHT")
		row.Value:SetWordWrap(false)
		return row
	end

	local function ListParse(raw)
		local block = { style = raw.style == "numbered" and "numbered" or "bullet", items = {} }
		block.value = Format(type(raw.value) == "table" and raw.value or {})
		for _, item in ipairs(type(raw.items) == "table" and raw.items or {}) do
			if type(item) == "table" and #block.items < MAX_ITEMS then
				block.items[#block.items + 1] = {
					text = tostring(item.text or ""), value = Value(block.value, item.value),
					mark = MARKS[item.mark] and { color = MARKS[item.mark] } or nil,
				}
			end
		end
		return #block.items > 0 and block or nil
	end

	local function ListPrepare(block, measure)
		block.marker, block.textWidth, block.valueWidth = BULLET, 0, 0
		if block.style == "numbered" then block.marker = math.ceil(measure("cell", #block.items .. ".")) + 4 end
		for _, item in ipairs(block.items) do
			item.shown, item.shownValue = ns.ItemText(item.text), Shown(block.value, item.value)
			item.width = measure("cell", item.shown)
			block.textWidth = math.max(block.textWidth, item.width)
			block.valueWidth = math.max(block.valueWidth, measure("cell", item.shownValue))
			if Waiting(item.text) or Waiting(item.value) then block.waiting = true end
		end
	end

	local function ListDraw(block, holder, width)
		holder.Rows = holder.Rows or {}
		-- Values get their room first, up to half of it; the text what's left.
		local values = block.valueWidth > 0 and math.min(math.ceil(block.valueWidth), math.floor(width / 2)) or 0
		for i, item in ipairs(block.items) do
			local row = holder.Rows[i] or ListRow(holder)
			holder.Rows[i] = row
			row.item = item
			row:ClearAllPoints()
			row:SetPoint("TOPLEFT", holder, "TOPLEFT", 0, -(i - 1) * ROW)
			row:SetWidth(width)
			row.Back:SetColorTexture(0, 0, 0, 0)
			MarkRow(row, item.mark)
			local numbered = block.style == "numbered"
			row.Bullet:SetShown(not numbered)
			row.Number:SetShown(numbered)
			row.Number:SetWidth(block.marker - 4)
			row.Number:SetText(numbered and i .. "." or "")
			row.room = width - block.marker - (values > 0 and values + GAP or 0) - GAP / 2
			row.Text:ClearAllPoints()
			row.Text:SetPoint("LEFT", row, "LEFT", block.marker, 0)
			row.Text:SetWidth(row.room)
			row.Text:SetText(item.shown)
			row.Value:SetShown(values > 0)
			row.Value:SetWidth(values)
			row.Value:SetText(item.shownValue)
			row:Show()
		end
		for i = #block.items + 1, #holder.Rows do holder.Rows[i]:Hide() end
		return #block.items * ROW
	end

	BLOCKS.list = {
		parse = ListParse,
		prepare = ListPrepare,
		width = function(block)
			local values = block.valueWidth > 0 and math.ceil(block.valueWidth) + GAP or 0
			return math.min(LIST_WIDTH, block.marker + math.ceil(block.textWidth) + values + GAP / 2)
		end,
		draw = ListDraw,
	}

	-- Texts: a short paragraph. ------------------------------------------------

	BLOCKS.text = {
		parse = function(raw)
			return type(raw.text) == "string" and raw.text ~= "" and { text = raw.text } or nil
		end,
		prepare = function(block, measure)
			block.shown = ns.ItemText(block.text)
			block.natural = measure("cell", block.shown)
			block.waiting = Waiting(block.text) or nil
		end,
		-- Long texts wrap rather than make the window wide.
		width = function(block) return math.min(TEXT_WIDTH, math.ceil(block.natural) + GAP) end,
		draw = function(block, holder, width)
			if not holder.Text then
				holder.Text = holder:CreateFontString(nil, "ARTWORK", "GameFontHighlightSmall")
				holder.Text:SetPoint("TOPLEFT", holder, "TOPLEFT", GAP / 2, 0)
				holder.Text:SetJustifyH("LEFT")
				holder.Text:SetWordWrap(true)
			end
			holder.Text:SetWidth(width - GAP)
			holder.Text:SetText(block.shown)
			return holder.Text:GetStringHeight()
		end,
	}

	---------------------------------------------------------------------------
	-- The window
	---------------------------------------------------------------------------

	-- The previous and next arrows, like the merchant's pages; step: which way they go through the visuals.
	local function PageButton(f, page, step, tip)
		local b = CreateFrame("Button", nil, f)
		b:SetSize(22, 22)
		b:SetNormalTexture("Interface\\Buttons\\UI-SpellbookIcon-" .. page .. "-Up")
		b:SetPushedTexture("Interface\\Buttons\\UI-SpellbookIcon-" .. page .. "-Down")
		b:SetDisabledTexture("Interface\\Buttons\\UI-SpellbookIcon-" .. page .. "-Disabled")
		b:SetHighlightTexture("Interface\\Buttons\\UI-Common-MouseHilight", "ADD")
		b:SetScript("OnClick", function() Visuals.Step(step) end)
		b:SetScript("OnEnter", function(self)
			local i = Index(current)
			local other = i and visuals[i + step]
			if not other then return end
			Tip(self)
			GameTooltip:SetText(tip, 1, 1, 1)
			GameTooltip:AddLine(ns.ItemText(other.title), 1, 0.82, 0, true)
			GameTooltip:Show()
		end)
		b:SetScript("OnLeave", function(self) HideTip(self) end)
		return b
	end

	-- The most of the stack shown at once: most of the screen's height.
	local function MostStack()
		local ok, height = pcall(UIParent.GetHeight, UIParent)
		if ok and type(height) == "number" and height > 400 then return math.floor(height * 0.7) end
		return STACK
	end

	-- The frame a block draws in: one per block of a type on show.
	local function Holder(f, kind, n)
		f.Holders[kind] = f.Holders[kind] or {}
		local holder = f.Holders[kind][n]
		if not holder then
			holder = CreateFrame("Frame", nil, f.Canvas)
			f.Holders[kind][n] = holder
		end
		return holder
	end

	local function CreateWindow()
		local f = CreateFrame("Frame", "WowMCPVisualFrame", UIParent)
		f:SetSize(MIN_WIDTH, 100)
		f:SetFrameStrata("MEDIUM")
		if f.SetToplevel then f:SetToplevel(true) end -- a click brings it in front of the plan window
		f:SetClampedToScreen(true)
		f:SetMovable(true)
		f:SetDontSavePosition(true) -- we keep the position ourselves, in WowMCPDB
		f:EnableMouse(true)
		f:EnableMouseWheel(true)
		f:RegisterForDrag("LeftButton")
		local saved = Saved()
		if saved.point then
			f:SetPoint(saved.point, UIParent, saved.relativePoint, saved.x, saved.y)
		else
			f:SetPoint("TOP", UIParent, "TOP", 0, -140)
		end
		f.Back = f:CreateTexture(nil, "BACKGROUND")
		f.Back:SetAllPoints()
		f.Back:SetColorTexture(0, 0, 0, 0.75)

		-- The game's red close button, and arrows to flip through the visuals.
		local ok, close = pcall(CreateFrame, "Button", nil, f, "UIPanelCloseButtonNoScripts")
		if not ok then close = CreateFrame("Button", nil, f) end
		f.Close = close
		close:SetSize(20, 20)
		close:SetPoint("TOPRIGHT", f, "TOPRIGHT", -2, -2)
		close:SetScript("OnClick", function() Visuals.Hide() end)
		close:SetScript("OnEnter", function(self)
			Tip(self)
			GameTooltip:SetText("Hide the window (/mcp visuals show brings it back)", 1, 1, 1)
			GameTooltip:Show()
		end)
		close:SetScript("OnLeave", function(self) HideTip(self) end)
		f.Next = PageButton(f, "NextPage", 1, "Next")
		f.Next:SetPoint("RIGHT", close, "LEFT", 0, 0)
		f.Count = f:CreateFontString(nil, "ARTWORK", "GameFontHighlightSmall")
		f.Count:SetPoint("RIGHT", f.Next, "LEFT", -1, 0)
		f.Prev = PageButton(f, "PrevPage", -1, "Previous")
		f.Prev:SetPoint("RIGHT", f.Count, "LEFT", -1, 0)

		f.Title = f:CreateFontString(nil, "ARTWORK", FONTS.title)
		f.Title:SetPoint("TOPLEFT", f, "TOPLEFT", PAD, -PAD)
		f.Title:SetJustifyH("LEFT")
		f.Title:SetWordWrap(false)
		f.Subtitle = f:CreateFontString(nil, "ARTWORK", "GameFontDisableSmall")
		f.Subtitle:SetJustifyH("LEFT")
		f.Subtitle:SetWordWrap(true)
		f.Subtitle:SetMaxLines(2)
		f.Footer = f:CreateFontString(nil, "ARTWORK", "GameFontDisableSmall")
		f.Footer:SetJustifyH("LEFT")
		-- For measuring texts: laid out like the ones on show, never seen.
		f.Measures = {}
		for font, template in pairs(FONTS) do
			local fs = f:CreateFontString(nil, "ARTWORK", template)
			fs:SetPoint("TOPLEFT", f, "TOPLEFT", 0, 0)
			fs:SetAlpha(0)
			f.Measures[font] = fs
		end

		-- The stack: our own scroll frame, so it scrolls as a whole when it's taller than the window can be.
		f.Scroll = CreateFrame("ScrollFrame", nil, f)
		f.Canvas = CreateFrame("Frame", nil, f.Scroll)
		f.Canvas:SetSize(MIN_WIDTH, 10)
		f.Scroll:SetScrollChild(f.Canvas)
		f.Scroll:EnableMouseWheel(true)
		f.Scroll:SetScript("OnMouseWheel", function(_, delta) Visuals.Scroll(delta) end)
		f.Holders, f.Headings = {}, {}
		f.Bar = NewBar(f, function(at)
			local view = View(current)
			view.scroll = at * (f.range or 0)
			Layout()
		end, function(delta) Visuals.Scroll(delta) end)

		-- The corner to drag, as on the plan window: sideways for the width, up and down for a table's rows.
		local grip = CreateFrame("Frame", nil, f)
		f.Grip = grip
		grip:SetSize(16, 16)
		grip:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", 0, 0)
		grip:EnableMouse(true)
		grip.Corner = grip:CreateTexture(nil, "OVERLAY")
		grip.Corner:SetAllPoints()
		grip.Corner:SetTexture("Interface\\ChatFrame\\UI-ChatIM-SizeGrabber-Up")
		local function StopResizing()
			grip:SetScript("OnUpdate", nil)
			grip.Corner:SetTexture("Interface\\ChatFrame\\UI-ChatIM-SizeGrabber-Up")
		end
		grip:SetScript("OnMouseDown", function(self, button)
			if button and button ~= "LeftButton" then return end
			if not (GetCursorPosition and f.GetEffectiveScale and current) then return end
			-- Held by its top left corner, so it grows right and down from where it is.
			local left, top = f:GetLeft(), f:GetTop()
			if left and top then
				f:ClearAllPoints()
				f:SetPoint("TOPLEFT", UIParent, "BOTTOMLEFT", left, top)
				local saved = Saved()
				saved.point, saved.relativePoint, saved.x, saved.y = "TOPLEFT", "BOTTOMLEFT", left, top
			end
			local scale = f:GetEffectiveScale()
			local x0, y0 = GetCursorPosition()
			local width, rows = f.content, f.rows
			self.Corner:SetTexture("Interface\\ChatFrame\\UI-ChatIM-SizeGrabber-Down")
			self:SetScript("OnUpdate", function()
				if IsMouseButtonDown and not IsMouseButtonDown("LeftButton") then return StopResizing() end
				local x, y = GetCursorPosition()
				Visuals.Resize(width + (x - x0) / scale, rows + (y0 - y) / scale / ROW)
			end)
		end)
		grip:SetScript("OnMouseUp", StopResizing)
		grip:SetScript("OnHide", StopResizing)
		grip:SetScript("OnEnter", function(self)
			Tip(self, "ANCHOR_RIGHT")
			GameTooltip:SetText("Drag to resize", 1, 1, 1)
			GameTooltip:AddLine("Sideways: wider or narrower. Up and down: fewer or more table rows at once.", HINT[1], HINT[2], HINT[3], true)
			GameTooltip:Show()
		end)
		grip:SetScript("OnLeave", function(self) HideTip(self) end)

		f:SetScript("OnDragStart", StartMoving)
		f:SetScript("OnDragStop", StopMoving)
		f:SetScript("OnMouseWheel", function(_, delta) Visuals.Scroll(delta) end)
		-- Drags end when the window goes (a hidden frame isn't told the button was let go).
		f:SetScript("OnHide", function()
			StopResizing()
			for _, bar in ipairs(bars) do bar.Stop() end
			HideTip()
			hovered = nil
		end)
		return f
	end

	-- A block's texts and sizes, worked out once (and again after its items have loaded).
	local function Prepare(block)
		if block.prepared then return end
		block.waiting = nil
		BLOCKS[block.type].prepare(block, Measure)
		block.prepared = true
	end

	-- Shows the visual on show as the player has it: each block in turn, the stack scrolled, the size theirs.
	function Layout()
		local saved, v = Saved(), current
		if not (v and saved.open) then
			if window then window:Hide() end
			return
		end
		window = window or CreateWindow()
		local f = window
		f:Show()
		saved.showing = v.id
		local view = View(v)

		-- As wide as the widest block wants (or as the player made it), but no narrower than any can be.
		local many, index = #visuals > 1, Index(v) or #visuals
		local want = Measure("title", ns.ItemText(v.title)) + (many and BUTTONS or CLOSE)
		local least = MIN_WIDTH - 2 * PAD
		for _, block in ipairs(v.blocks) do
			local kind = BLOCKS[block.type]
			Prepare(block)
			want = math.max(want, kind.width(block), block.heading and Measure("heading", ns.ItemText(block.heading)) + GAP or 0)
			least = math.max(least, kind.least and kind.least(block) or 0)
		end
		local width = math.max(least, math.min(MAX_WIDTH - 2 * PAD, math.floor((tonumber(view.width) or want) + 0.5)))
		f.content = width

		-- The title, the subtitle under it, the arrows to the other visuals.
		f.Prev:SetShown(many)
		f.Next:SetShown(many)
		f.Count:SetShown(many)
		if many then
			f.Count:SetText(index .. "/" .. #visuals)
			f.Prev:SetEnabled(index > 1)
			f.Next:SetEnabled(index < #visuals)
		end
		f.Title:SetWidth(width - (many and BUTTONS or CLOSE))
		f.Title:SetText(ns.ItemText(v.title))
		local top = PAD + TITLE
		f.Subtitle:SetShown(v.subtitle ~= nil)
		if v.subtitle then
			f.Subtitle:ClearAllPoints()
			f.Subtitle:SetPoint("TOPLEFT", f, "TOPLEFT", PAD, -top)
			f.Subtitle:SetWidth(width)
			f.Subtitle:SetText(ns.ItemText(v.subtitle))
			top = top + f.Subtitle:GetStringHeight() + 2
		end
		top = top + 6

		-- The blocks, top to bottom, each under its heading.
		local canvas, used, y, rows = f.Canvas, {}, 0, 0
		for i, block in ipairs(v.blocks) do
			local heading = f.Headings[i]
			if not heading then
				heading = canvas:CreateFontString(nil, "ARTWORK", FONTS.heading)
				heading:SetJustifyH("LEFT")
				heading:SetWordWrap(false)
				f.Headings[i] = heading
			end
			heading:SetShown(block.heading ~= nil)
			if block.heading then
				heading:ClearAllPoints()
				heading:SetPoint("TOPLEFT", canvas, "TOPLEFT", GAP / 2, -y)
				heading:SetWidth(width - GAP)
				heading:SetText(ns.ItemText(block.heading))
				y = y + HEADING
			end
			used[block.type] = (used[block.type] or 0) + 1
			local holder = Holder(f, block.type, used[block.type])
			holder:ClearAllPoints()
			holder:SetPoint("TOPLEFT", canvas, "TOPLEFT", 0, -y)
			view.blocks[i] = view.blocks[i] or {}
			local height = BLOCKS[block.type].draw(block, holder, width, view.blocks[i])
			holder:SetSize(width, height)
			holder:Show()
			y = y + height + BLOCK_GAP
			if block.type == "table" then rows = math.max(rows, RowsShown(block)) end
		end
		f.rows = rows
		for i = #v.blocks + 1, #f.Headings do f.Headings[i]:Hide() end
		for kind, holders in pairs(f.Holders) do
			for n = (used[kind] or 0) + 1, #holders do holders[n]:Hide() end
		end

		-- The stack scrolls when it's taller than the window can be.
		local height = math.max(1, y - BLOCK_GAP)
		local shown = math.min(height, MostStack())
		local scrolls = height > shown
		f.range = height - shown
		canvas:SetSize(width, height)
		f.Scroll:ClearAllPoints()
		f.Scroll:SetPoint("TOPLEFT", f, "TOPLEFT", PAD, -top)
		f.Scroll:SetSize(width, shown)
		view.scroll = math.max(0, math.min(tonumber(view.scroll) or 0, f.range))
		if f.Scroll.UpdateScrollChildRect then f.Scroll:UpdateScrollChildRect() end -- its range, for the canvas's new size
		f.Scroll:SetVerticalScroll(view.scroll)
		f.Bar:SetShown(scrolls)
		if scrolls then
			f.Bar:ClearAllPoints()
			f.Bar:SetPoint("TOPLEFT", f, "TOPLEFT", PAD + width + BAR_GAP, -top)
			SetBar(f.Bar, shown, shown / height, view.scroll / f.range)
		end

		f.Footer:ClearAllPoints()
		f.Footer:SetPoint("TOPLEFT", f, "TOPLEFT", PAD, -(top + shown + 4))
		f.Footer:SetText(v.at and "Sent " .. When(v.at) or "")
		f:SetSize(width + 2 * PAD + (scrolls and BAR + BAR_GAP or 0), top + shown + 4 + LINE + PAD)

		-- Scrolling or sorting moved another row under the mouse: its tooltip.
		if hovered then
			local still = hovered:IsShown() and hovered.holder:IsShown() and (not hovered.Row or hovered.Row:IsShown())
			if still then hovered.enter(hovered) else hovered.leave(hovered) end
		end
	end

	---------------------------------------------------------------------------
	-- What the player does
	---------------------------------------------------------------------------

	-- Shows the window: visual n (1 = the oldest kept), or the one it showed last.
	function Visuals.Show(n)
		if #visuals == 0 then return ns.Print("Nothing to show. Ask your AI for a table!") end
		if n ~= nil then
			if not visuals[n] then
				return ns.Print(string.format("There's no %s: there %s %d. /mcp visuals lists them.", tostring(n),
					#visuals == 1 and "is" or "are", #visuals))
			end
			current = visuals[n]
		end
		current = current or visuals[#visuals]
		Saved().open = true
		Layout()
	end

	function Visuals.Hide()
		if not (window and window:IsShown()) then return ns.Print("The window isn't open. /mcp visuals show opens it.") end
		Saved().open = nil
		Layout()
		ns.Print("Hidden. /mcp visuals show brings it back.")
	end

	-- Removes every visual there is; new ones from the AI show as usual.
	function Visuals.Clear()
		if #visuals == 0 then return ns.Print("Nothing to clear.") end
		WowMCPDB.visualsCleared = visuals[#visuals].id
		WowMCPDB.visualViews = nil
		visuals, current = {}, nil
		local saved = Saved()
		saved.open, saved.showing = nil, nil
		Layout()
		ns.Print("Cleared. New tables and lists from your AI still show here.")
	end

	-- The previous (-1) or next (1) visual.
	function Visuals.Step(step)
		local i = Index(current)
		local other = i and visuals[i + step]
		if not other then return end
		current = other
		HideTip()
		Layout()
	end

	-- The mouse wheel over the stack.
	function Visuals.Scroll(delta)
		if not (current and window and window:IsShown()) then return end
		local view = View(current)
		view.scroll = (tonumber(view.scroll) or 0) - (tonumber(delta) or 0) * SCROLL_STEP * ROW
		Layout()
	end

	-- The window's size: the width of the visual on show, and how many rows tables show at once. Only what
	-- changes is kept, so a visual keeps fitting its content until the player makes it wider or narrower.
	function Visuals.Resize(width, rows)
		if not (current and window) then return end
		width, rows = tonumber(width), tonumber(rows)
		if width and math.abs(width - (window.content or 0)) >= 1 then
			View(current).width = math.max(MIN_WIDTH - 2 * PAD, math.min(MAX_WIDTH - 2 * PAD, math.floor(width + 0.5)))
		end
		if rows and (window.rows or 0) > 0 and math.floor(rows + 0.5) ~= window.rows then
			Saved().rows = math.max(MIN_ROWS, math.min(MOST_ROWS, math.floor(rows + 0.5)))
		end
		Layout()
	end

	function Visuals.List()
		if #visuals == 0 then return ns.Print("Nothing to show. Ask your AI for a table!") end
		ns.Print("What your AI showed, newest last (/mcp visuals <n> shows one):")
		local open = Saved().open
		for i, v in ipairs(visuals) do
			local kinds = {}
			for _, block in ipairs(v.blocks) do kinds[#kinds + 1] = block.type end
			ns.Print(string.format("%d. |cffffffff%s|r (%s%s)%s", i, ns.Escape(v.title), table.concat(kinds, ", "),
				v.at and ", sent " .. When(v.at) or "", open and v == current and " - showing" or ""))
		end
	end

	-- A visual from the payload, with the blocks this addon can show; one it can't says so.
	local function Parse(raw)
		if type(raw) ~= "table" or type(raw.id) ~= "number" then return nil end
		local v = {
			id = raw.id, title = tostring(raw.title or "Visual"), at = tonumber(raw.at), blocks = {},
			subtitle = type(raw.subtitle) == "string" and raw.subtitle ~= "" and raw.subtitle or nil,
		}
		for _, b in ipairs(type(raw.blocks) == "table" and raw.blocks or {}) do
			if type(b) == "table" and #v.blocks < MAX_BLOCKS then
				local kind, block = BLOCKS[b.type], nil
				if kind then
					local ok, parsed = pcall(kind.parse, b)
					block = ok and parsed or nil
				else
					block = { type = "text", text = "(A " .. tostring(b.type) .. " the WowMCP addon can't show yet: run wow-mcp-install, then /reload.)" }
				end
				if block then
					block.type = block.type or b.type
					block.heading = type(b.heading) == "string" and b.heading ~= "" and b.heading or nil
					v.blocks[#v.blocks + 1] = block
				end
			end
		end
		return #v.blocks > 0 and v or nil
	end

	-- The payload has the kept visuals, { id, list = { visual, ... } }, id being the update that last changed
	-- them. One we haven't seen opens the window; the ones we have keep how the player left them.
	function Visuals.Receive(payload)
		local id = tonumber(payload.id) or 0
		local seen = WowMCPDB.visualSeen or 0
		if id < seen then -- the server's state was reset, so its ids started over
			seen, visuals = 0, {}
			WowMCPDB.visualsCleared, WowMCPDB.visualViews = nil, nil
		end
		WowMCPDB.visualSeen = id
		local cleared = WowMCPDB.visualsCleared or 0
		local had, list, newest = {}, {}, nil
		for _, v in ipairs(visuals) do had[v.id] = v end -- read once: the payload comes with every update
		for _, raw in ipairs(type(payload.list) == "table" and payload.list or {}) do
			local v = type(raw) == "table" and had[raw.id] or Parse(raw)
			if v and v.id > cleared then
				list[#list + 1] = v
				if v.id > seen and (not newest or v.id > newest.id) then newest = v end
			end
		end
		table.sort(list, function(a, b) return a.id < b.id end)
		visuals = list
		for viewed in pairs(WowMCPDB.visualViews or {}) do
			if not Find(viewed) then WowMCPDB.visualViews[viewed] = nil end
		end
		local saved = Saved()
		local was = window and window:IsShown()
		if newest then
			current = newest
			saved.open = true
		else
			current = Find(current and current.id or saved.showing) or list[#list]
		end
		if not current then saved.open, saved.showing = nil, nil end
		Layout()
		if was and not current then ns.Print("Your AI cleared what it showed.") end
	end

	local USAGE = "/mcp visuals [show, hide, clear, <n>] - the tables and lists from your AI: list them, show or hide "
		.. "their window, remove them, show number n. Click a table's header to sort by it; drag the corner to resize"

	function Visuals.Command(rest)
		local word = strtrim(tostring(rest or "")):lower()
		if word == "" or word == "list" then
			Visuals.List()
		elseif word == "show" then
			Visuals.Show()
		elseif word == "hide" then
			Visuals.Hide()
		elseif word == "clear" then
			Visuals.Clear()
		elseif tonumber(word) then
			Visuals.Show(math.floor(tonumber(word)))
		else
			ns.Print(USAGE)
		end
	end

	Visuals.BLOCKS = BLOCKS -- the block types, by name

	ns.RegisterReceiver("visuals", function(payload)
		if type(payload) ~= "table" then return end
		local ok, err = pcall(Visuals.Receive, payload)
		if not ok then ns.Print("|cffff6060Visuals error:|r " .. ns.Escape(tostring(err))) end
	end)
	ns.RegisterCommand("visuals", USAGE, Visuals.Command)
	ns.RegisterCommand("table", nil, Visuals.Command) -- the same; without a usage line of its own
	-- Items that were still loading: their names, and the order by them.
	ns.OnItemsLoaded(function()
		local redraw = false
		for _, v in ipairs(visuals) do
			for _, block in ipairs(v.blocks) do
				if block.waiting then
					block.prepared = nil
					redraw = redraw or v == current
				end
			end
		end
		if redraw and window and window:IsShown() then Layout() end
	end)
end
