-- WowMCP core: receives data from the wow-mcp server while the game is running.
--
-- Addons can't read files or sockets at runtime, but the client does read two kinds
-- of files on demand (technique from github.com/chelinho139/wow-ai):
--   * PlaySoundFile() re-reads a sound file until it has once been valid. The server
--     turns sig/NNNN.wav from an empty file into a 2 s silent wav to say "new data".
--     We play the next one twice a second; on the Forever client PlaySoundFile returns
--     a handle even for an empty file, so we check C_Sound.IsPlaying() on the next poll.
--   * C_AddOns.LoadAddOn() reads a load-on-demand addon's Lua from disk when called.
--     The server writes the latest payload into every WowMCP_Sxxx slot addon; each
--     slot can be loaded once per UI session.
-- Inbox.lua (read on login and /reload) carries the same payload, so nothing is lost
-- if the live channel is unavailable.
--
-- A second set of doorbells, req/NNNN.wav, asks for game data; Requests.lua collects it and
-- Stream.lua sends it back.
--
-- Features plug in through the registries below (payload receivers, slash commands, login
-- hooks) and Requests.lua (request sections), so each one lives in its own file.

local ADDON, ns = ...

local SLOT_COUNT = 300
ns.SLOT_COUNT = SLOT_COUNT
local SIGNAL_COUNT = 1000  -- sig/: new data in the slots
local REQUEST_COUNT = 500  -- req/: requests for game data
local REQUEST_KINDS = 8    -- request n asks for kind n % 8 (see Requests.lua)
local POLL_INTERVAL = 0.5
local ROOT = "Interface\\AddOns\\" .. ADDON .. "\\"

local PREFIX = "|cff33ff99WowMCP|r "

local run = {
	seq = 0,            -- newest payload seen
	req = 0,            -- newest state request seen
	payload = nil,
	loggedIn = false,
	live = false,       -- signal self-test passed
	liveProblem = nil,
	pendingLoad = false,
	nextSlot = 1,
	slotProblem = nil,
}
ns.run = run

-- Item tokens in msg show as item links (see ns.Chat).
function ns.Print(msg)
	ns.Chat(PREFIX .. msg)
end

-- Text from the AI must not be able to inject UI escape sequences.
function ns.Escape(text)
	return (tostring(text or ""):gsub("|", "||"))
end

---------------------------------------------------------------------------
-- Items in text from the AI: {item:2318}, or {item:2318:Light Leather} with a name to show until
-- the client has loaded the item, shows as the item's icon and the game's name for it in its
-- quality colour; in chat, as an item link. The text is escaped first (ns.Escape) and a token
-- can't hold a "|", so the escape sequences made here are the only ones it gets.
---------------------------------------------------------------------------

local TOKEN = "({item:(%d+)([^{}|\n]*)})"
local MAX_ITEM_ID = 2147483647
local CHAT_WAIT = 1 -- seconds a chat line waits for items the client is still loading
local loading = {}  -- item ID -> true: asked the client for it; what shows it redraws when it's there
local missing = {}  -- item ID -> true: the client says there's no such item
local itemListeners = {}
local held = {}     -- chat lines waiting for their items, oldest first: { text, frame, late }
local redrawing = false

-- fn runs when items shown without their names have loaded, so their text can be shown again.
function ns.OnItemsLoaded(fn)
	table.insert(itemListeners, fn)
end

local function Request(id)
	if loading[id] or missing[id] then return end
	loading[id] = true
	if C_Item.RequestLoadItemDataByID then pcall(C_Item.RequestLoadItemDataByID, id) end
end

-- The icon is in the client's own data; the name, link and quality may have to be loaded first.
local function Icon(id)
	local ok, _, _, _, _, icon = pcall(C_Item.GetItemInfoInstant, id)
	if not (ok and icon) and C_Item.GetItemIconByID then ok, icon = pcall(C_Item.GetItemIconByID, id) end
	return ok and icon or nil
end

local function ItemInfo(id)
	local ok, name, link, quality = pcall(C_Item.GetItemInfo, id)
	if not ok then name, link, quality = nil, nil, nil end
	if not name and C_Item.GetItemNameByID then
		ok, name = pcall(C_Item.GetItemNameByID, id)
		name = ok and name or nil
	end
	if type(name) ~= "string" or name == "" then
		Request(id)
		return nil
	end
	return name, type(link) == "string" and link or nil, quality
end

-- ITEM_QUALITY_COLORS follows the player's colour overrides (e.g. colourblind mode).
local function QualityColor(quality)
	if type(quality) ~= "number" then return nil end
	local color = ITEM_QUALITY_COLORS and ITEM_QUALITY_COLORS[quality]
	if color and color.hex then return color.hex end
	local ok, r, g, b = pcall(C_Item.GetItemQualityColor, quality)
	if ok and r then
		return string.format("|cff%02x%02x%02x", math.floor(r * 255 + 0.5), math.floor(g * 255 + 0.5), math.floor(b * 255 + 0.5))
	end
end

-- One token. Unknown items stay as written: the given name, else the token itself. asLink: an
-- item link, for chat.
local function Item(token, id, rest, asLink)
	local given = rest:match("^:%s*(.-)%s*$")
	if rest ~= "" and not given then return token end -- e.g. {item:12abc}: not a token
	given = given ~= "" and given or nil
	id = tonumber(id)
	if not id or id < 1 or id > MAX_ITEM_ID then return given or token end
	local icon = Icon(id)
	local name, link, quality = ItemInfo(id)
	if not (icon or name) then return given or token end
	local color = name and QualityColor(quality)
	name = name and ns.Escape(name) or given or ("item " .. id)
	-- Height 0: as tall as the line, like the chat's raid target icons.
	local shown = icon and ("|T" .. icon .. ":0|t ") or ""
	if asLink and link then return shown .. link end
	if asLink then name = string.format("|Hitem:%d::::::::::::::::|h[%s]|h", id, name) end
	return shown .. (color and (color .. name .. "|r") or name)
end

-- The tokens in escaped text. Walks the escape sequences, so an escaped "||" is never read as one,
-- and a token inside one of the addon's own chat links shows without a link (links can't nest).
local function Render(text, links)
	if not text:find("{item:", 1, true) then return text end
	local parts, inLink, i = {}, nil, 1
	while i <= #text do
		local bar = text:find("|", i, true) or #text + 1
		local run = text:sub(i, bar - 1)
		if inLink ~= "data" then
			local asLink = links and not inLink
			run = run:gsub(TOKEN, function(token, id, rest) return Item(token, id, rest, asLink) end)
		end
		parts[#parts + 1] = run
		local code = text:sub(bar + 1, bar + 1)
		if code == "H" then
			inLink = "data"
		elseif code == "h" then
			inLink = inLink == "data" and "text" or nil
		end
		parts[#parts + 1] = text:sub(bar, bar + 1)
		i = bar + 2
	end
	return table.concat(parts)
end

-- Text from the AI as it shows outside chat: escaped, with its items' icons and names. (In chat,
-- ns.Print and ns.ShowMessage show them as links themselves.)
function ns.ItemText(text)
	return ns.RenderItems(ns.Escape(text))
end

-- The same for text that's already escaped, e.g. AI text inside a line of the addon's own.
function ns.RenderItems(text)
	local ok, shown = pcall(Render, text, false)
	return ok and shown or text
end

-- An item's icon followed by a space, to put before its name; "" if unknown.
function ns.ItemIcon(itemID)
	local id = tonumber(itemID)
	local icon = id and Icon(id)
	return icon and ("|T" .. icon .. ":0|t ") or ""
end

-- Whether the items a chat line names have loaded, or never will. Asks for all of them at once.
local function Ready(text)
	local ready = true
	for digits in text:gmatch("{item:(%d+)[^{}|\n]*}") do
		local id = tonumber(digits)
		if id and id >= 1 and id <= MAX_ITEM_ID and not missing[id] and not ItemInfo(id) then ready = false end
	end
	return ready
end

local function FlushChat()
	while held[1] do
		local line = held[1]
		local ok, ready = pcall(Ready, line.text)
		if ok and not ready and not line.late then return end
		table.remove(held, 1)
		local rendered, text = pcall(Render, line.text, true)
		local frame = line.frame or DEFAULT_CHAT_FRAME
		frame:AddMessage(rendered and text or line.text)
	end
end

-- Adds a line to a chat window as it is, with item tokens as item links (ns.Print adds the WowMCP
-- prefix). frame: the chat window (default: the main one). A line naming items the client is still
-- loading waits for them, up to CHAT_WAIT, so it shows the game's names and links; the lines after it
-- wait their turn, in every window, so the order stays.
function ns.Chat(text, frame)
	local line = { text = tostring(text or ""), frame = frame }
	table.insert(held, line)
	FlushChat()
	if held[#held] == line then
		C_Timer.After(CHAT_WAIT, function()
			line.late = true
			FlushChat()
		end)
	end
end

local function ItemArrived(id, success)
	if not loading[id] then return end
	loading[id] = nil
	if success == false then missing[id] = true end
	if redrawing then return end
	redrawing = true
	-- Once for all the items that arrive together.
	C_Timer.After(0, function()
		redrawing = false
		FlushChat()
		for _, fn in ipairs(itemListeners) do pcall(fn) end
	end)
end

---------------------------------------------------------------------------
-- Registries
---------------------------------------------------------------------------

local receivers = {}  -- payload key -> function(value), called for every new payload carrying it
local loginHooks = {}
local commands = {}   -- /mcp <name> -> { usage, run }; "" = no arguments, "#" = a number

function ns.RegisterReceiver(key, receive)
	receivers[key] = receive
end

-- Runs at PLAYER_LOGIN, in registration order, before the stored payload is applied.
function ns.OnLogin(hook)
	table.insert(loginHooks, hook)
end

function ns.RegisterCommand(name, usage, run)
	commands[name] = { usage = usage, run = run }
end

local function Apply(payload)
	for key, receive in pairs(receivers) do
		if payload[key] ~= nil then receive(payload[key]) end
	end
end

-- Called by Inbox.lua and by every slot addon's Inbox.lua.
function WowMCP_Receive(payload)
	if type(payload) ~= "table" then return end
	if type(payload.req) == "number" and payload.req > run.req then run.req = payload.req end
	if type(payload.seq) ~= "number" or payload.seq <= run.seq then return end
	run.seq = payload.seq
	run.payload = payload
	if run.loggedIn then Apply(payload) end
end

---------------------------------------------------------------------------
-- Live channel
---------------------------------------------------------------------------

-- Starts playing a file; IsPlaying(handle) a moment later says whether it had content.
local function StartSound(path)
	local ok, willPlay, handle = pcall(PlaySoundFile, path, "Master")
	return ok and willPlay and handle or nil
end

local function IsPlaying(handle)
	local playing = C_Sound.IsPlaying(handle)
	StopSound(handle)
	return playing
end

local function SoundProblem()
	if type(PlaySoundFile) ~= "function" or not (C_Sound and C_Sound.IsPlaying) then
		return "this client lacks the sound functions it needs"
	end
	if C_CVar and C_CVar.GetCVarBool and not C_CVar.GetCVarBool("Sound_EnableAllSound") then
		return "game sound is turned off"
	end
end

local function SlotName(i)
	return string.format("%s_S%03d", ADDON, i)
end

function ns.SlotsLeft()
	local left = 0
	for i = run.nextSlot, SLOT_COUNT do
		if not C_AddOns.IsAddOnLoaded(SlotName(i)) then left = left + 1 end
	end
	return left
end

local function LoadSlot()
	if InCombatLockdown() then return end  -- picked up again on PLAYER_REGEN_ENABLED
	run.pendingLoad = false
	for i = run.nextSlot, SLOT_COUNT do
		local name = SlotName(i)
		run.nextSlot = i + 1
		if not C_AddOns.IsAddOnLoaded(name) then
			local loaded, reason = C_AddOns.LoadAddOn(name)
			if loaded then return true end
			if not run.slotProblem then
				run.slotProblem = reason or "unknown"
				ns.Print("Couldn't load " .. name .. " (" .. tostring(reason) .. "). Are the WowMCP slot addons installed and enabled? New data will show after /reload.")
			end
			return false
		end
	end
	if run.slotProblem ~= "EXHAUSTED" then
		run.slotProblem = "EXHAUSTED"
		ns.Print("Used all live-update slots for this session. /reload to reset them; new data shows after the reload.")
	end
	return false
end

-- A doorbell is a numbered pool of sound files; the server raises number n by making
-- file n % count valid. We check the next unused numbers on every poll: start them on
-- one poll, see whether they're playing on the next. A bell with stride k carries k kinds
-- of request, kind = n % k, so we check the next number of each kind.
local bells = {}

local function AddBell(dir, count, stride, last, onRing, paused)
	table.insert(bells, {
		dir = dir, count = count, stride = stride, last = last, onRing = onRing, paused = paused,
		used = {}, probes = {},
	})
end

local function PollBell(bell)
	local rang = {}
	for _, probe in ipairs(bell.probes) do
		if IsPlaying(probe.handle) then
			-- A file stays valid for the rest of the client process, so never probe it again.
			bell.used[probe.n % bell.count] = true
			rang[#rang + 1] = probe.n
		end
	end
	bell.probes = {}
	if #rang > 0 then
		table.sort(rang)
		for _, n in ipairs(rang) do bell.onRing(n) end
		return
	end
	-- Normally the numbers right after the last one seen; further on if a file was
	-- already used and didn't bring anything newer.
	local last = bell.last()
	for offset = 1, bell.stride do
		local n = last + offset
		while bell.used[n % bell.count] do n = n + bell.stride end
		local handle = StartSound(string.format("%s%s\\%04d.wav", ROOT, bell.dir, n % bell.count))
		if handle then table.insert(bell.probes, { n = n, handle = handle }) end
	end
end

local function Poll()
	if run.pendingLoad then LoadSlot() end
	for _, bell in ipairs(bells) do
		if not (bell.paused and bell.paused()) then PollBell(bell) end
	end
end

AddBell("sig", SIGNAL_COUNT, 1,
	function() return run.seq end,
	function()
		run.pendingLoad = true
		LoadSlot()
	end,
	function() return run.pendingLoad end)

AddBell("req", REQUEST_COUNT, REQUEST_KINDS,
	function() return run.req end,
	function(n)
		if n > run.req then run.req = n end
		if ns.Answer then ns.Answer(n, n % REQUEST_KINDS) end
	end)

local function StartLive()
	local function Off(problem)
		run.liveProblem = problem
		ns.Print("Live updates are off: " .. problem .. ". New data will show after /reload.")
	end
	local problem = SoundProblem()
	if problem then return Off(problem) end
	-- An empty file must not count as playing, or every signal would look raised.
	local handle = StartSound(ROOT .. "ctl\\empty.wav")
	C_Timer.After(POLL_INTERVAL, function()
		if handle and IsPlaying(handle) then return Off("an empty sound file plays") end
		run.live = true
		C_Timer.NewTicker(POLL_INTERVAL, Poll)
	end)
end

---------------------------------------------------------------------------
-- Events and commands
---------------------------------------------------------------------------

local events = CreateFrame("Frame")
events:RegisterEvent("ADDON_LOADED")
events:RegisterEvent("PLAYER_LOGIN")
events:RegisterEvent("PLAYER_REGEN_ENABLED")
events:RegisterEvent("ITEM_DATA_LOAD_RESULT")
pcall(events.RegisterEvent, events, "GET_ITEM_INFO_RECEIVED") -- also when GetItemInfo started the load
events:SetScript("OnEvent", function(_, event, arg1, arg2)
	if event == "ITEM_DATA_LOAD_RESULT" or event == "GET_ITEM_INFO_RECEIVED" then
		ItemArrived(arg1, arg2)
	elseif event == "ADDON_LOADED" and arg1 == ADDON then
		WowMCPDB = WowMCPDB or {}
	elseif event == "PLAYER_LOGIN" then
		run.loggedIn = true
		for _, hook in ipairs(loginHooks) do hook() end
		if run.payload then Apply(run.payload) end
		StartLive()
	elseif event == "PLAYER_REGEN_ENABLED" then
		if run.pendingLoad then LoadSlot() end
	end
end)

ns.RegisterCommand("status", "/mcp status - connection details", function()
	ns.Print(string.format("live updates: %s; slots left this session: %d/%d; last update #%d",
		run.live and "on" or ("off (" .. tostring(run.liveProblem) .. ")"),
		ns.SlotsLeft(), SLOT_COUNT, run.seq))
end)

SLASH_WOWMCP1 = "/mcp"
SLASH_WOWMCP2 = "/wowmcp"
SlashCmdList.WOWMCP = function(input)
	local name, rest = strtrim(input or ""):match("^(%S*)%s*(.-)$")
	name = name:lower()
	local command = commands[name] or (tonumber(name) and commands["#"])
	if command then
		command.run(tonumber(name) or rest, rest)
		return
	end
	local usages = {}
	for _, c in pairs(commands) do usages[#usages + 1] = c.usage end
	table.sort(usages)
	for _, usage in ipairs(usages) do ns.Print(usage) end
end
