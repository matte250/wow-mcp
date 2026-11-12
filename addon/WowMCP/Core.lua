-- WowMCP core: receives data from the wow-mcp server while the game is running.
--
-- Addons can't read files or sockets at runtime, but the client does read two kinds
-- of files on demand (technique from github.com/chelinho139/wow-ai):
--   * PlaySoundFile() reads a long sound from disk on every play (it streams it), and an
--     empty file never plays. The server keeps a frame of bits in bell/ (wow_mcp/bell.py),
--     each file 8 s of silence or empty; we play them and check C_Sound.IsPlaying() on the
--     next step. A frame says there's new data in the slots, asks for game data, or carries
--     the bridge's notes about an /ai question.
--   * C_AddOns.LoadAddOn() reads a load-on-demand addon's Lua from disk when called.
--     The server writes the latest payload into every WowMCP_Sxxx slot addon; each
--     slot can be loaded once per UI session.
-- Inbox.lua (read on login and /reload) carries the same payload, so nothing is lost
-- if the live channel is unavailable.
--
-- Requests.lua collects what a request asks for, and Stream.lua sends it back, as it sends the
-- receipt for each frame.
--
-- Features plug in through the registries below (payload receivers, slash commands, login
-- hooks) and Requests.lua (request sections), so each one lives in its own file.

local ADDON, ns = ...

local SLOT_COUNT = 300
ns.SLOT_COUNT = SLOT_COUNT
local ROOT = "Interface\\AddOns\\" .. ADDON .. "\\"

local PREFIX = "|cff33ff99WowMCP|r "

local run = {
	seq = 0,            -- newest payload seen
	req = 0,            -- newest request answered
	payload = nil,
	loggedIn = false,
	live = false,       -- the self-test at login passed
	liveProblem = nil,
	verified = false,   -- seen the bell's files change on disk (the handshake with the server)
	frame = nil,        -- the bell's frame as last read whole: its bits and parity
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
local longs = {}      -- kind -> function(bytes), for messages that come over the bell in several frames
local loginHooks = {}
local commands = {}   -- /mcp <name> -> { usage, run }; "" = no arguments, "#" = a number

function ns.RegisterReceiver(key, receive)
	receivers[key] = receive
end

-- receive(bytes) runs for each message of this kind (0-15) the server sends over the bell as a long
-- message (wow_mcp/bell.py's long()): small data that needn't wait for a slot.
function ns.RegisterLong(kind, receive)
	longs[kind] = receive
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

-- The bell (wow_mcp/bell.py has its format). A frame is 24 files, 00.wav-23.wav, each a bit: 8 s of
-- silence (the server links it to on.wav) or empty. The client streams long sounds, so it reads each
-- from disk on every play; we play it and see whether it plays. Two markers carry the frame's parity,
-- which flips with every frame: the server sets lead.wav first and seal.wav last, and we read seal.wav
-- first and lead.wav last, so a frame read while the next one is written shows markers that differ and is
-- read again. A CRC catches anything else torn. Each frame taken gets a receipt over the stream
-- (Stream.Receipt), and the server writes the next one only then.
--
-- A play costs about 0.1 ms, so we read as little as we can. While nothing happens, only seal.wav, every
-- IDLE_EVERY seconds; every step while the server was busy lately (or we just logged in), an /ai question
-- waits, or the auction house or a mailbox is open. When it flips, the whole frame on the next step, its plays spread over render
-- frames (BUDGET_MS each). Sounds are checked and stopped CHECK_AFTER seconds after they started, a little
-- before the next step's: Windows can't replace a file a program has open, so the server gets its moment.
--
-- Were the client to keep these files like short sounds, the bits would stick. So until we've seen a
-- marker go from valid to empty, receipts say so and the server writes test frames that make it do that.
-- If what we read then doesn't hold together for a while, the files aren't read afresh: live updates off.

local BELL = ROOT .. "bell\\"
local DATA_BITS = 24
local SEAL, LEAD = BELL .. "seal.wav", BELL .. "lead.wav"
local FRAME = { SEAL } -- in the order read: seal.wav, the data (lowest bit first), lead.wav
for i = 0, DATA_BITS - 1 do FRAME[#FRAME + 1] = string.format("%s%02d.wav", BELL, i) end
FRAME[#FRAME + 1] = LEAD

local STEP = 0.25       -- seconds between steps: each starts the sounds it reads
local CHECK_AFTER = 0.2 -- seconds after they started that IsPlaying says what they were
local IDLE_EVERY = 2    -- seconds between looks at seal.wav while nothing happens
local ACTIVE_FOR = 90   -- seconds the server counts as busy after a frame (agents call tools in bursts) and login
local BUDGET_MS = 1.5   -- frame time per render frame for starting sounds, as Stream.lua's
local RETRIES = 4       -- unreadable frames read again before waiting for seal.wav to flip
local EXPECT = 4        -- seconds the server's test frames may take after a receipt that asks for them
local STUCK = 8         -- unreadable reads in a row while expecting them: the files don't change as written

local PARITY, VERIFIED = 16777216, 33554432 -- 2^24, 2^25: above the bits, in a frame and in a receipt
local BODY = 524288                         -- 2^19: below it a frame's type and payload, above it its CRC
local MORE, DATA, REQUEST, ACK, PROGRESS, TEST, LONG = 0, 1, 2, 3, 4, 5, 6 -- frame types (bell.py)

local bell = {
	plays = nil,       -- this step's sounds: { kind = "start" | "seal" | "frame", files, handles, next }
	wait = 0,          -- seconds to the next step
	hot = 0,           -- steps to read the whole frame on, whatever seal.wav says
	retries = 0,
	unreadable = 0,    -- frames in a row that didn't hold together
	nextLook = 0,      -- GetTime() of the next look at seal.wav while nothing happens
	last = -math.huge, -- GetTime() of the last frame taken, or of the login (an agent may go on after a /reload)
	taken = nil,       -- its bits; WowMCPDB.bell keeps them over a /reload
	sawValid = false,  -- a marker read valid: once one reads empty after that, run.verified
	expecting = nil,   -- GetTime() until which the server's test frames are due
	long = nil,        -- a long message coming: { kind, length, bytes }
}
local reader = CreateFrame("Frame")

local function Off(problem)
	run.live, run.liveProblem, bell.plays = false, problem, nil
	reader:SetScript("OnUpdate", nil)
	ns.Print("Live updates are off: " .. problem .. ". New data will show after /reload.")
end

-- n's bit worth `bit` (a power of two): 0 or 1. Lua 5.1 has no bit operators.
local function Bit(n, bit)
	return math.floor(n / bit) % 2
end

-- CRC-5 (x^5 + x^2 + 1, from 0x1F) of a frame's parity, type and payload, highest bit first (bell.crc).
local function Crc(message)
	local crc, bit = 31, BODY
	while bit >= 1 do
		local top = math.floor(crc / 16)
		crc = (crc * 2) % 32
		if top ~= Bit(message, bit) then
			crc = crc + (Bit(crc, 1) == 1 and -1 or 1) + (Bit(crc, 4) == 1 and -4 or 4) -- crc xor 0x05
		end
		bit = bit / 2
	end
	return crc
end

-- Tokens written as a PROGRESS frame carries them (bell.tokens_count); nil if not counted.
local function TokenCount(code)
	if code >= 2047 then return nil end
	if code < 1000 then return code end
	return code < 1990 and (code - 990) * 100 or (code - 1980) * 10000
end

-- Whether the server may well send something soon: it did lately, or the player waits for an answer or
-- is at the auction house or a mailbox, where agents tend to ask for prices and mail.
local function Busy(now)
	return now - bell.last < ACTIVE_FOR
		or (ns.AIChat and ns.AIChat.Waiting and ns.AIChat.Waiting())
		or (ns.Auction and ns.Auction.IsOpen and ns.Auction.IsOpen())
		or (ns.Inventory and ns.Inventory.MailOpen and ns.Inventory.MailOpen())
		or false
end

local function Receipt(frame)
	if ns.Stream then ns.Stream.Receipt(frame + (run.verified and VERIFIED or 0)) end
	-- Not verified: the server answers with test frames, and we read every step until they've come.
	if not run.verified then bell.expecting = GetTime() + EXPECT end
end

local function Unreadable()
	bell.unreadable = bell.unreadable + 1
	if bell.expecting then
		if bell.unreadable >= STUCK then
			return Off("the live-update files don't change on disk as written (the game may keep them in memory); "
				.. "fully restart WoW")
		end
		bell.hot = math.max(bell.hot, 1)
	elseif bell.retries < RETRIES then
		bell.retries = bell.retries + 1
		bell.hot = math.max(bell.hot, 1)
	end
end

local function Take(kind, payload)
	if kind ~= MORE then bell.long = nil end -- a long message cut short is dropped
	if kind == DATA then
		if payload ~= run.seq % 65536 then
			run.pendingLoad = true
			LoadSlot()
		end
	elseif kind == REQUEST then
		local id = math.floor(payload / 8)
		run.req = id
		if ns.Answer then ns.Answer(id, payload % 8) end
	elseif kind == ACK and ns.AIChat then
		ns.AIChat.Status({ status = "ack", question = payload % 16, quiet = math.floor(payload / 16) })
	elseif kind == PROGRESS and ns.AIChat then
		ns.AIChat.Status({ status = "progress", step = payload % 32, tokens = TokenCount(math.floor(payload / 32)) })
	elseif kind == LONG then
		bell.long = { kind = payload % 16, length = math.floor(payload / 16), bytes = {} }
	elseif kind == MORE and bell.long then
		table.insert(bell.long.bytes, string.char(math.floor(payload / 256), payload % 256))
	end
	local long = bell.long
	if long and #long.bytes * 2 >= long.length then
		bell.long = nil
		if longs[long.kind] then pcall(longs[long.kind], table.concat(long.bytes):sub(1, long.length)) end
	elseif long then
		bell.hot = math.max(bell.hot, 1) -- the rest follows right away
	end
end

-- The whole frame as read (on[i]: FRAME[i] played).
local function Read(on)
	local seal, lead = on[1] and 1 or 0, on[#FRAME] and 1 or 0
	if seal ~= lead then return Unreadable() end
	local bits = 0
	for i = DATA_BITS + 1, 2, -1 do bits = bits * 2 + (on[i] and 1 or 0) end
	local frame = bits + seal * PARITY
	run.frame = frame
	if seal == 1 then
		bell.sawValid = true
	elseif bell.sawValid and not run.verified then
		run.verified, bell.expecting = true, nil
	end
	if bits == 0 and seal == 0 then return end -- as installed: nothing written yet
	if bits == bell.taken then -- the frame we took (its CRC covers the parity too)
		bell.unreadable, bell.retries = 0, 0
		return
	end
	if Crc(seal * BODY + bits % BODY) ~= math.floor(bits / BODY) then return Unreadable() end
	bell.unreadable, bell.retries = 0, 0
	bell.taken, bell.last = bits, GetTime()
	WowMCPDB.bell = bits
	Receipt(frame) -- first: a reply to a request goes after it
	Take(bits % 8, math.floor(bits % BODY / 8))
end

-- Starts this step's sounds, as many as fit in this render frame's budget.
local function PlaySome()
	local plays = bell.plays
	if not plays or plays.next > #plays.files then return end
	local started = debugprofilestop()
	repeat
		plays.handles[plays.next] = StartSound(plays.files[plays.next]) or false
		plays.next = plays.next + 1
	until plays.next > #plays.files or debugprofilestop() - started >= BUDGET_MS
	if plays.next > #plays.files then plays.at = GetTime() end
end

local function Start(kind, files)
	bell.plays = { kind = kind, files = files, handles = {}, next = 1 }
	PlaySome()
end

-- What the sounds were, once they've all played CHECK_AFTER seconds.
local function Check()
	local plays = bell.plays
	if not (plays and plays.at and GetTime() - plays.at >= CHECK_AFTER) then return end
	bell.plays = nil
	local on = {}
	for i, handle in ipairs(plays.handles) do on[i] = handle and IsPlaying(handle) or false end
	if plays.kind == "start" then
		-- The self-test: a long silent sound plays, an empty one doesn't. The handshake follows with the
		-- server's first frames.
		if not plays.handles[1] then return Off("the game hasn't seen the live-update files yet: fully restart WoW") end
		if not on[1] then return Off("a long silent sound doesn't play") end
		if on[2] then return Off("an empty sound file plays") end
		run.live = true
		bell.hot = 1 -- what the server left there
	elseif plays.kind == "seal" then
		if not run.frame or (on[1] and 1 or 0) ~= math.floor(run.frame / PARITY) then
			bell.hot, bell.retries = math.max(bell.hot, 1), 0
		end
	else
		Read(on)
	end
end

local function Step()
	local now = GetTime()
	if bell.plays or not run.live then return end -- still playing
	if run.pendingLoad then LoadSlot() end
	if bell.expecting then
		if now > bell.expecting then bell.expecting = nil else bell.hot = math.max(bell.hot, 1) end
	end
	if bell.hot > 0 then
		bell.hot = bell.hot - 1
		Start("frame", FRAME)
	elseif now >= bell.nextLook or Busy(now) then
		bell.nextLook = now + IDLE_EVERY
		Start("seal", { SEAL })
	end
end

-- A step every STEP seconds, at most one per render frame.
local function OnUpdate(_, elapsed)
	PlaySome()
	Check()
	bell.wait = bell.wait - elapsed
	if bell.wait > 0 then return end
	bell.wait = math.max(bell.wait, -STEP) + STEP
	Step()
end

local function StartLive()
	local problem = SoundProblem()
	if problem then return Off(problem) end
	bell.taken, bell.last = WowMCPDB.bell, GetTime()
	Start("start", { BELL .. "on.wav", BELL .. "off.wav" })
	reader:SetScript("OnUpdate", OnUpdate)
end

-- After a loading screen: the server may have written meanwhile, more than seal.wav can tell.
local function ReadAgain()
	if run.live then bell.hot = math.max(bell.hot, 1) end
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
pcall(events.RegisterEvent, events, "LOADING_SCREEN_DISABLED")
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
	elseif event == "LOADING_SCREEN_DISABLED" then
		ReadAgain()
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
