-- WowMCP status: addon build, live channel and slots left (request kind STATUS), so the
-- server can tell whether the game runs the installed code and whether updates get through.

local _, ns = ...

-- "Interface action failed because of an AddOn" doesn't say which addon or what it was doing. The
-- game raises these events synchronously, inside the blocked call, so the stack here shows the code
-- path that led to it. The last few are kept (any addon's) for get_status.
local BLOCKED_KEPT, STACK_LENGTH = 10, 1200
local blocked = CreateFrame("Frame")
for _, event in ipairs({ "ADDON_ACTION_BLOCKED", "ADDON_ACTION_FORBIDDEN", "MACRO_ACTION_BLOCKED", "MACRO_ACTION_FORBIDDEN" }) do
	pcall(blocked.RegisterEvent, blocked, event)
end
blocked:SetScript("OnEvent", function(_, event, addon, func)
	if type(WowMCPDB) ~= "table" then return end
	local list = type(WowMCPDB.blocked) == "table" and WowMCPDB.blocked or {}
	WowMCPDB.blocked = list
	local ok, stack = pcall(debugstack, 2, 20, 0)
	table.insert(list, 1, {
		at = time(), event = event, addon = tostring(addon), func = tostring(func),
		stack = ok and type(stack) == "string" and stack:sub(1, STACK_LENGTH) or nil,
	})
	for i = #list, BLOCKED_KEPT + 1, -1 do list[i] = nil end
end)

local function Status()
	local run = ns.run
	return {
		build = ns.BUILD,               -- nil if Build.lua wasn't loaded (new file: restart WoW)
		live = run.live,
		liveProblem = run.liveProblem,
		slotsLeft = ns.SlotsLeft(),
		slotCount = ns.SLOT_COUNT,
		slotProblem = run.slotProblem,  -- LoadAddOn's reason (e.g. "MISSING", "DISABLED") or "EXHAUSTED"
		pendingLoad = run.pendingLoad,  -- an update is waiting for combat to end
		seq = run.seq,
		req = run.req,
		blocked = WowMCPDB and WowMCPDB.blocked,  -- newest first
	}
end

ns.RegisterSection(ns.Kinds.STATUS, "status", Status)
