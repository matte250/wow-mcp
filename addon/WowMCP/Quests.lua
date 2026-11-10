-- WowMCP quests: completed quests and the quest log with objective progress (request kind QUESTS).

local _, ns = ...

-- The quest tags Forever's quest log shows (Constants.QuestConsts.QUEST_INFO_*_ID).
local TAGS = { [1] = "elite", [41] = "pvp", [62] = "raid", [81] = "dungeon" }

-- Failed state, tag and objective progress of a quest in the log. Runs under pcall: if the game
-- won't give some of it (e.g. a secret value), the quest is still listed with what it got.
local function AddDetails(entry, questID)
	entry.failed = C_QuestLog.IsFailed(questID) or nil
	local tag = C_QuestLog.GetQuestTagInfo(questID)
	entry.tag = tag and TAGS[tag.tagID]
	local objectives = {}
	for _, objective in ipairs(C_QuestLog.GetQuestObjectives(questID) or {}) do
		if objective.text and objective.text ~= "" then
			objectives[#objectives + 1] = {
				text = objective.text,
				fulfilled = objective.numFulfilled,
				required = objective.numRequired,
				finished = objective.finished and true or false,
			}
		end
	end
	entry.objectives = objectives
end

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
	-- The log lists every quest, also those under collapsed headers. Each quest gets the header
	-- above it: usually its zone, otherwise a category such as a class or profession.
	local log, header = {}, nil
	for i = 1, C_QuestLog.GetNumQuestLogEntries() do
		local info = C_QuestLog.GetInfo(i)
		if info and info.isHeader then
			header = info.title
		elseif info and (info.questID or 0) > 0 then
			local entry = {
				id = info.questID,
				title = info.title,
				ready = C_QuestLog.IsComplete(info.questID) and true or false,
				-- The level the log shows ("[12] Title").
				level = (info.difficultyLevel or 0) > 0 and info.difficultyLevel or info.level,
				header = header,
			}
			pcall(AddDetails, entry, info.questID)
			log[#log + 1] = entry
		end
	end
	return { completedGaps = gaps, log = log }
end

ns.RegisterSection(ns.Kinds.QUESTS, "quests", Quests)
