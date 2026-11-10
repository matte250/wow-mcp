-- WowMCP quests: completed quests and the quest log with objective progress and rewards (request kind QUESTS).

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

-- Reward items as { id, name, count } (count left out for one). The client may not have an item's
-- name yet: it's asked for, so it's likely there when the server asks again.
local function RewardItems(count, info, questID)
	local items, missing = {}, false
	for i = 1, count do
		local name, _, number, _, _, itemID = info(i, questID)
		if itemID then
			local item = { id = itemID, count = (number or 1) > 1 and number or nil }
			if name and name ~= "" then
				item.name = name
			else
				pcall(C_Item.RequestLoadItemDataByID, itemID)
				missing = true
			end
			items[#items + 1] = item
		else
			missing = true
		end
	end
	return items, missing
end

-- What turning the quest in gives: XP (what this character gets at their level, as the quest log
-- shows it), money in copper, items, and items to choose from. All of these take the questID
-- (without one they read the quest selected in the log), so the player's selection stays as it is.
-- Rewards the client hasn't loaded read as 0, so they're asked for and reported as loading, never
-- as no reward. entry.rewards is left out when everything's there, else "loading", "hidden" or
-- (if the game wouldn't give them) "unknown".
local function AddRewards(entry, questID)
	local ok, shown = pcall(C_QuestLog.ShouldShowQuestRewards, questID)
	if ok and shown == false then
		entry.rewards = "hidden" -- the quest log shows none for this quest
		return
	end
	local xp, money = GetQuestLogRewardXP(questID), GetQuestLogRewardMoney(questID)
	local numItems, numChoices = GetNumQuestLogRewards(questID), GetNumQuestLogChoices(questID)
	local loaded
	ok, loaded = pcall(HaveQuestRewardData, questID)
	-- Rewards that did come through are there, whatever HaveQuestRewardData says.
	if not (ok and loaded) and xp == 0 and money == 0 and numItems == 0 and numChoices == 0 then
		pcall(C_QuestLog.RequestLoadQuestByID, questID)
		pcall(C_TaskQuest.RequestPreloadRewardData, questID)
		entry.rewards = "loading"
		return
	end
	entry.xp, entry.money = xp, money
	local itemsMissing, choicesMissing
	entry.items, itemsMissing = RewardItems(numItems, GetQuestLogRewardInfo, questID)
	entry.choices, choicesMissing = RewardItems(numChoices, GetQuestLogChoiceInfo, questID)
	if itemsMissing or choicesMissing then entry.rewards = "loading" end
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
			if not pcall(AddRewards, entry, info.questID) then entry.rewards = "unknown" end
			log[#log + 1] = entry
		end
	end
	return { completedGaps = gaps, log = log }
end

ns.RegisterSection(ns.Kinds.QUESTS, "quests", Quests)
