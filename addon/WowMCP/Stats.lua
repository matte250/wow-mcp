-- WowMCP stats: money, XP, rested XP, hearthstone location and /played (section of request kind STATE).

local _, ns = ...

local PLAYED_EVENT = "TIME_PLAYED_MSG"
local PLAYED_DELAY = 5     -- seconds after login, once the chat frames are set up
local UNMUTE_AFTER = 10    -- give the chat frames /played back even if the answer never comes

-- /played as the game last told us, and when (GetTime()); time since then is added on reads.
local played
local muted -- chat frames we took /played away from while our own request is out

local function Mute(frame)
	if frame:IsEventRegistered(PLAYED_EVENT) then
		frame:UnregisterEvent(PLAYED_EVENT)
		muted[#muted + 1] = frame
	end
end

local function Unmute()
	for _, frame in ipairs(muted or {}) do pcall(frame.RegisterEvent, frame, PLAYED_EVENT) end
	muted = nil
end

-- RequestTimePlayed() makes every chat frame print /played, so it's asked once per UI session,
-- with the chat frames' event switched off until the answer is in.
local function RequestPlayed()
	if type(RequestTimePlayed) ~= "function" then return end
	muted = {}
	pcall(ChatFrameUtil and ChatFrameUtil.ForEachChatFrame, function(frame) pcall(Mute, frame) end)
	RequestTimePlayed()
	C_Timer.After(UNMUTE_AFTER, Unmute)
end

local events = CreateFrame("Frame")
events:RegisterEvent(PLAYED_EVENT)
events:RegisterEvent("PLAYER_LEVEL_UP")
events:SetScript("OnEvent", function(_, event, total, level)
	if event == PLAYED_EVENT then
		played = { total = total, level = level, at = GetTime() }
		-- On the next frame: registering now could still hand them this answer.
		if muted then C_Timer.After(0, Unmute) end
	elseif event == "PLAYER_LEVEL_UP" and played then
		played = { total = played.total + GetTime() - played.at, level = 0, at = GetTime() }
	end
end)

ns.OnLogin(function() C_Timer.After(PLAYED_DELAY, RequestPlayed) end)

local function MaxLevel()
	local max
	for _, name in ipairs({ "GetMaxPlayerLevel", "GetMaxLevelForPlayerExpansion" }) do
		local ok, n = pcall(_G[name])
		if ok and type(n) == "number" and n > 0 then max = math.min(max or n, n) end
	end
	return max
end

local function Stats()
	local level, maxLevel = UnitLevel("player"), MaxLevel()
	local stats = {
		money = GetMoney(),
		level = level,
		maxLevel = maxLevel,
		hearth = GetBindLocation(),
		resting = IsResting() and true or false,
		xpDisabled = IsXPUserDisabled() and true or nil,
	}
	if not stats.xpDisabled and not (maxLevel and level >= maxLevel) then
		stats.xp, stats.xpMax, stats.rested = UnitXP("player"), UnitXPMax("player"), GetXPExhaustion() or 0
	end
	if played then
		local elapsed = GetTime() - played.at
		stats.played = { total = math.floor(played.total + elapsed), level = math.floor(played.level + elapsed) }
	end
	return stats
end

ns.RegisterSection(ns.Kinds.STATE, "stats", Stats)
