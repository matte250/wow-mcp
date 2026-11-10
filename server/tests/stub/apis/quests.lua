-- C_QuestLog (Quests.lua).

-- The quest log as the modern client lists it: each header followed by its quests, also
-- under a collapsed header (Westfall). Headers carry questID 0.
local entries = {
	{ title = "Redridge Mountains", isHeader = true, isCollapsed = false, questID = 0, level = 0, difficultyLevel = 0 },
	{ title = "Blackrock Menace", isHeader = false, questID = 20, level = 22, difficultyLevel = 22, suggestedGroup = 0 },
	{ title = "Underbelly Scales", isHeader = false, questID = 122, level = 21, difficultyLevel = 21, suggestedGroup = 0 },
	{ title = "Tharil'zun", isHeader = false, questID = 19, level = 26, difficultyLevel = 26, suggestedGroup = 3 },
	{ title = "Westfall", isHeader = true, isCollapsed = true, questID = 0, level = 0, difficultyLevel = 0 },
	{ title = "The People's Militia", isHeader = false, questID = 12, level = 14, difficultyLevel = 14, suggestedGroup = 0 },
	{ title = "The Defias Brotherhood", isHeader = false, questID = 65, level = 14, difficultyLevel = 14, suggestedGroup = 0 },
	{ title = "Loch Modan", isHeader = true, isCollapsed = false, questID = 0, level = 0, difficultyLevel = 0 },
	{ title = "Protecting the Shipment", isHeader = false, questID = 309, level = 15, difficultyLevel = 15, suggestedGroup = 0 },
}

-- GetQuestObjectives: text as the game shows it, with the counts. A delivery quest has none.
local objectives = {
	[20] = { { text = "Blackrock Champion slain: 7/15", type = "monster", finished = false, numFulfilled = 7, numRequired = 15 } },
	[122] = { { text = "Underbelly Whelp Scale: 6/6", type = "item", finished = true, numFulfilled = 6, numRequired = 6 } },
	[19] = { { text = "Tharil'zun's Head: 0/1", type = "item", finished = false, numFulfilled = 0, numRequired = 1 } },
	[12] = {
		{ text = "Defias Trapper slain: 15/15", type = "monster", finished = true, numFulfilled = 15, numRequired = 15 },
		{ text = "Defias Smuggler slain: 7/15", type = "monster", finished = false, numFulfilled = 7, numRequired = 15 },
	},
	[65] = {},
	[309] = { { text = "Protect Miran", type = "event", finished = false, numFulfilled = 0, numRequired = 1 } },
}
local complete = { [122] = true, [65] = true }
local failed = { [309] = true }
local tags = { [19] = { tagID = 1, tagName = "Elite", isElite = true } } -- Constants.QuestConsts.QUEST_INFO_ELITE_ID

-- The client hands out a new table on every call.
local function Copy(value)
	if type(value) ~= "table" then return value end
	local copy = {}
	for k, v in pairs(value) do copy[k] = Copy(v) end
	return copy
end

-- Turned in (unsorted, like the client).
C_QuestLog = {
	GetAllCompletedQuestIDs = function() return { 783, 7, 33, 6, 5261 } end,
	GetNumQuestLogEntries = function() return #entries, 6 end, -- entries, quests
	GetInfo = function(i)
		local info = Copy(entries[i])
		if info then info.questLogIndex = i end
		return info
	end,
	IsComplete = function(questID) return complete[questID] or false end,
	IsFailed = function(questID) return failed[questID] or false end,
	-- Returns nothing for a quest that isn't in the log.
	GetQuestObjectives = function(questID) return Copy(objectives[questID]) end,
	GetQuestTagInfo = function(questID) return Copy(tags[questID]) end,
}
