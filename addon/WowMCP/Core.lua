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

function ns.Print(msg)
	DEFAULT_CHAT_FRAME:AddMessage(PREFIX .. msg)
end

-- Text from the AI must not be able to inject UI escape sequences.
function ns.Escape(text)
	return (tostring(text or ""):gsub("|", "||"))
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
events:SetScript("OnEvent", function(_, event, arg1)
	if event == "ADDON_LOADED" and arg1 == ADDON then
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
