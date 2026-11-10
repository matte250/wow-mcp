-- C_QuestLog and the quest reward functions (Quests.lua).

-- The quest log as the modern client lists it: each header followed by its quests, also
-- under a collapsed header (Westfall). Headers carry questID 0.
client.questLog = {
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
client.questObjectives = {
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

-- Rewards (made up for the tests): XP for this character's level, money in copper, items given
-- ({ id, count }) and items to choose from. Until the client has loaded a quest's rewards
-- (unloaded), every reward function answers 0, as if it gave nothing; asking loads them by the
-- next frame. The quest log shows no rewards for a hidden one.
client.questRewards = {
	[20] = { xp = 1450, money = 1200, items = {}, choices = { { id = 2232 }, { id = 1282 } } },
	[122] = { xp = 1350, money = 0, items = {}, choices = {} },
	[19] = { xp = 2100, money = 2500, items = {}, choices = { { id = 3562 }, { id = 1264 } } },
	[12] = { xp = 900, money = 750, items = { { id = 929, count = 2 } }, choices = {}, unloaded = true },
	[65] = { xp = 450, money = 350, items = {}, choices = {} },
	[309] = { xp = 1020, money = 600, items = {}, choices = {}, hidden = true },
}
-- Reward items join the item data of inventory.lua (loaded before this file): an uncached one
-- has no name until something asks the client to load it.
client.items[2232] = { name = "Dark Runner Boots", quality = 2, level = 25, class = 4, subclass = 2, slot = "INVTYPE_FEET" }
client.items[1282] = { name = "Sparkmetal Coif", quality = 2, level = 24, class = 4, subclass = 0, slot = "INVTYPE_HEAD" }
client.items[3562] = { name = "Belt of Vindication", quality = 2, level = 28, class = 4, subclass = 2,
	slot = "INVTYPE_WAIST", uncached = true }
client.items[1264] = { name = "Headbasher", quality = 2, level = 28, class = 2, subclass = 1, slot = "INVTYPE_2HWEAPON" }
client.items[929] = { name = "Healing Potion", quality = 1, level = 22, class = 0 }

-- The quest selected in the quest log (0: none). The reward functions read that one when called
-- without a questID, as the quest log's own panel calls them.
client.selectedQuest = 0

-- The client hands out a new table on every call.
local function Copy(value)
	if type(value) ~= "table" then return value end
	local copy = {}
	for k, v in pairs(value) do copy[k] = Copy(v) end
	return copy
end

local function Rewards(questID)
	local rewards = client.questRewards[questID or client.selectedQuest]
	return rewards and not rewards.unloaded and rewards or nil
end

local function LoadRewards(questID)
	local rewards = client.questRewards[questID]
	if rewards and rewards.unloaded then
		C_Timer.After(0, function()
			rewards.unloaded = nil
			client.Fire("QUEST_DATA_LOAD_RESULT", questID, true)
		end)
	end
end

-- name, texture, count, quality, isUsable, itemID, itemLevel; nothing past the end. The ID is
-- there before the item has loaded, its name and icon only after.
local function RewardInfo(list, index)
	local reward = list and list[index]
	if not reward then return nil end
	local item = client.items[reward.id]
	local cached = C_Item.IsItemDataCachedByID(reward.id)
	return cached and item.name or nil, cached and 134400 or nil, reward.count or 1, item.quality, true, reward.id,
		item.level
end

local function Count(questID, kind)
	local rewards = Rewards(questID)
	return rewards and #rewards[kind] or 0
end

-- Turned in (unsorted, like the client).
C_QuestLog = {
	GetAllCompletedQuestIDs = function() return { 783, 7, 33, 6, 5261 } end,
	GetNumQuestLogEntries = function()
		local quests = 0
		for _, entry in ipairs(client.questLog) do
			if not entry.isHeader then quests = quests + 1 end
		end
		return #client.questLog, quests
	end,
	GetInfo = function(i)
		local info = Copy(client.questLog[i])
		if info then info.questLogIndex = i end
		return info
	end,
	IsComplete = function(questID) return complete[questID] or false end,
	IsFailed = function(questID) return failed[questID] or false end,
	-- Returns nothing for a quest that isn't in the log.
	GetQuestObjectives = function(questID) return Copy(client.questObjectives[questID]) end,
	GetQuestTagInfo = function(questID) return Copy(tags[questID]) end,
	GetSelectedQuest = function() return client.selectedQuest end,
	SetSelectedQuest = function(questID) client.selectedQuest = questID end,
	ShouldShowQuestRewards = function(questID)
		local rewards = client.questRewards[questID]
		return not (rewards and rewards.hidden)
	end,
	RequestLoadQuestByID = LoadRewards,
}
C_TaskQuest = { RequestPreloadRewardData = LoadRewards }

-- The reward functions: the questID is optional (see client.selectedQuest).
function HaveQuestRewardData(questID) return Rewards(questID) ~= nil end
function GetQuestLogRewardXP(questID) -- totalXp, baseXp (without bonuses)
	local rewards = Rewards(questID)
	return rewards and rewards.xp or 0, rewards and rewards.xp or 0
end
function GetQuestLogRewardMoney(questID)
	local rewards = Rewards(questID)
	return rewards and rewards.money or 0
end
function GetNumQuestLogRewards(questID) return Count(questID, "items") end
function GetNumQuestLogChoices(questID) return Count(questID, "choices") end
function GetQuestLogRewardInfo(index, questID)
	local rewards = Rewards(questID)
	return RewardInfo(rewards and rewards.items, index)
end
function GetQuestLogChoiceInfo(index, questID)
	local rewards = Rewards(questID)
	return RewardInfo(rewards and rewards.choices, index)
end
