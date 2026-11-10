-- WowMCP status: addon build, live channel and slots left (request kind STATUS), so the
-- server can tell whether the game runs the installed code and whether updates get through.

local _, ns = ...

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
	}
end

ns.RegisterSection(ns.Kinds.STATUS, "status", Status)
