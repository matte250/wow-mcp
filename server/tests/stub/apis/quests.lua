-- C_QuestLog (Quests.lua).

-- Turned in (unsorted, like the client), and the quest log with a zone header.
C_QuestLog = {
	GetAllCompletedQuestIDs = function() return { 783, 7, 33, 6, 5261 } end,
	GetNumQuestLogEntries = function() return 3 end,
	GetInfo = function(i)
		return ({
			{ title = "Redridge Mountains", isHeader = true, questID = 0 },
			{ title = "Blackrock Menace", isHeader = false, questID = 20 },
			{ title = "Underbelly Scales", isHeader = false, questID = 122 },
		})[i]
	end,
	IsComplete = function(questID) return questID == 122 end,
}

