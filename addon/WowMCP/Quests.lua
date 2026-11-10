-- WowMCP quests: completed quests and the quest log (request kind QUESTS).

local _, ns = ...

-- Every quest the character has turned in, as gaps between the sorted IDs (small numbers
-- compress well), plus what's in the quest log now.
local function Quests()
	local completed = C_QuestLog.GetAllCompletedQuestIDs()
	table.sort(completed)
	local gaps, previous = {}, 0
	for i, questID in ipairs(completed) do
		gaps[i] = questID - previous
		previous = questID
	end
	local log = {}
	for i = 1, C_QuestLog.GetNumQuestLogEntries() do
		local info = C_QuestLog.GetInfo(i)
		if info and not info.isHeader and (info.questID or 0) > 0 then
			log[#log + 1] = { id = info.questID, title = info.title, ready = C_QuestLog.IsComplete(info.questID) and true or false }
		end
	end
	return { completedGaps = gaps, log = log }
end

ns.RegisterSection(ns.Kinds.QUESTS, "quests", Quests)
