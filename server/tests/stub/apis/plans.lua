-- What Plans.lua reads and the player changes while following a plan: item counts (bags and
-- bank), quests being accepted, progressed and turned in, level, skills, known spells, the zone;
-- and the widgets of the plan window. Helpers fire the events the client fires for each change.

client.level = nil       -- set by client.LevelUp; until then player.lua's level
client.knownSpells = {}  -- spell IDs C_SpellBook.IsSpellKnown knows
client.spellNames = {
	[2881] = "Light Leather", [3816] = "Cured Light Hide", [3770] = "Toughened Leather Gloves", [3817] = "Cured Medium Hide",
	[3766] = "Dark Leather Belt", [3753] = "Handstitched Leather Belt", [2152] = "Light Armor Kit", [1978] = "Serpent Sting",
}
client.questLog = {}     -- quests accepted with client.AcceptQuest: questID -> { title, objectives }
client.turnedIn = {}     -- quests turned in with client.TurnInQuest
client.zoneText, client.subzoneText = nil, nil -- override player.lua's zone texts when set

client.items[3182] = { name = "Spider's Silk", quality = 1, level = 15, class = 7, sell = 150 }
client.itemNames[3182] = "Spider's Silk" -- not loaded until something asks for it once
client.items[4237] = { name = "Handstitched Leather Belt", quality = 1, level = 10, class = 4, subclass = 2,
	slot = "INVTYPE_WAIST", sell = 18, armor = 16, required = 5 }
client.items[4249] = { name = "Dark Leather Belt", quality = 2, level = 25, class = 4, subclass = 2,
	slot = "INVTYPE_WAIST", sell = 247, armor = 40, required = 20 }

-- The chat lines the client prints when the player learns something (GlobalStrings.lua).
ERR_LEARN_RECIPE_S = "You have learned how to create a new item: %s."
ERR_LEARN_SPELL_S = "You have learned a new spell: %s."

---------------------------------------------------------------------------
-- Widgets: what the plan window uses beyond client.lua's frames and regions.
---------------------------------------------------------------------------

local Frame = getmetatable(UIParent)
local Region = getmetatable(UIParent:CreateTexture())
function Frame:EnableMouseWheel(on) self.mouseWheel = on end
function Frame:SetNormalTexture(texture) self.normalTexture = texture end
function Frame:SetHighlightTexture(texture) self.highlightTexture = texture end
function Frame:GetParent() return self.parent end
function Region:SetJustifyV(justify) self.justifyV = justify end

---------------------------------------------------------------------------
-- Items. The client counts the bank also away from the banker (once it knows its contents), and
-- counts by an item ID, a link, or the name of an item the player has.
---------------------------------------------------------------------------

local CARRIED = { [0] = true, [1] = true, [2] = true, [3] = true, [4] = true, [5] = true }

function C_Item.GetItemCount(item, includeBank)
	local itemID = type(item) == "number" and item or tonumber(tostring(item):match("item:(%d+)"))
	local total = 0
	for bag, b in pairs(client.bags) do
		if CARRIED[bag] or (includeBank and bag >= 6) then
			for _, s in pairs(b.slots) do
				local named = not itemID and client.items[s.id] and client.items[s.id].name == item
				if s.id == itemID or named then total = total + (s.count or 1) end
			end
		end
	end
	return total
end

-- Loot or buy count of an item into a bag (default the backpack), in a free slot.
function client.AddItem(itemID, count, bag)
	bag = bag or 0
	local b = client.bags[bag]
	for slot = 1, b.size do
		if not b.slots[slot] then
			b.slots[slot] = { id = itemID, count = count }
			client.Fire("BAG_UPDATE", bag)
			client.Fire(bag >= 6 and "PLAYERBANKSLOTS_CHANGED" or "BAG_UPDATE_DELAYED", slot)
			return
		end
	end
	error("bag " .. bag .. " is full")
end

-- Use up (craft with, sell) count of an item from the bags.
function client.RemoveItem(itemID, count)
	for bag = 0, 4 do
		for slot, s in pairs(client.bags[bag] and client.bags[bag].slots or {}) do
			if s.id == itemID and count > 0 then
				local take = math.min(count, s.count or 1)
				s.count, count = (s.count or 1) - take, count - take
				if s.count == 0 then client.bags[bag].slots[slot] = nil end
			end
		end
	end
	client.Fire("BAG_UPDATE_DELAYED")
end

---------------------------------------------------------------------------
-- Quests, level, skills, spells and the zone: installed over quests.lua and player.lua (which load
-- after this file) when the addon first loads, wrapping what's there.
---------------------------------------------------------------------------

local function Copy(value)
	if type(value) ~= "table" then return value end
	local copy = {}
	for k, v in pairs(value) do copy[k] = Copy(v) end
	return copy
end

local installed = false
local function Install()
	if installed then return end
	installed = true

	local base = {}
	for name, fn in pairs(C_QuestLog) do base[name] = fn end
	local function InBaseLog(questID)
		for i = 1, base.GetNumQuestLogEntries() do
			local info = base.GetInfo(i)
			if info and info.questID == questID then return info end
		end
	end
	local function Flagged(questID)
		if client.turnedIn[questID] then return true end
		for _, id in ipairs(base.GetAllCompletedQuestIDs()) do
			if id == questID then return true end
		end
		return false
	end
	function C_QuestLog.IsOnQuest(questID)
		return client.questLog[questID] ~= nil or (InBaseLog(questID) ~= nil and not client.turnedIn[questID])
	end
	function C_QuestLog.GetTitleForQuestID(questID)
		local quest = client.questLog[questID] or InBaseLog(questID)
		return quest and quest.title
	end
	C_QuestLog.IsQuestFlaggedCompleted = Flagged
	function C_QuestLog.GetAllCompletedQuestIDs()
		local list = base.GetAllCompletedQuestIDs()
		for questID in pairs(client.turnedIn) do table.insert(list, questID) end
		return list
	end
	function C_QuestLog.GetQuestObjectives(questID)
		local quest = client.questLog[questID]
		if quest then return Copy(quest.objectives) end
		return base.GetQuestObjectives(questID)
	end
	function C_QuestLog.IsComplete(questID)
		local quest = client.questLog[questID]
		if not quest then return base.IsComplete(questID) end
		for _, objective in ipairs(quest.objectives) do
			if not objective.finished then return false end
		end
		return true
	end

	local level = UnitLevel
	function UnitLevel(unit) return client.level or level(unit) end
	local zone, subzone = GetRealZoneText, GetSubZoneText
	function GetRealZoneText() return client.zoneText or zone() end
	function GetSubZoneText() return client.subzoneText or subzone() end

	C_SpellBook = C_SpellBook or {}
	function C_SpellBook.IsSpellKnown(spellID) return client.knownSpells[spellID] == true end
	C_Spell = C_Spell or {}
	function C_Spell.GetSpellName(spellID) return client.spellNames[spellID] end
end

local LoadAddon = client.LoadAddon
function client.LoadAddon()
	Install()
	return LoadAddon()
end

-- objectives: { { text = "Kobold Vermin slain", required = 10 } }; the game's text has the count.
function client.AcceptQuest(questID, title, objectives)
	Install()
	local list = {}
	for i, o in ipairs(objectives or {}) do
		list[i] = { name = o.text, text = string.format("%s: 0/%d", o.text, o.required), type = "monster",
			finished = false, numFulfilled = 0, numRequired = o.required }
	end
	client.questLog[questID] = { title = title, objectives = list }
	client.Fire("QUEST_ACCEPTED", questID)
	client.Fire("QUEST_LOG_UPDATE")
end

function client.Progress(questID, index, fulfilled)
	local o = client.questLog[questID].objectives[index]
	o.numFulfilled, o.finished = fulfilled, fulfilled >= o.numRequired
	o.text = string.format("%s: %d/%d", o.name, fulfilled, o.numRequired)
	client.Fire("UNIT_QUEST_LOG_CHANGED", "player")
	client.Fire("QUEST_LOG_UPDATE")
end

-- Like the client, the quest leaves the log at once, but is only flagged completed a moment later.
function client.TurnInQuest(questID)
	Install()
	client.questLog[questID] = nil
	client.Fire("QUEST_TURNED_IN", questID, 450, 100)
	client.Fire("QUEST_REMOVED", questID, false)
	client.Fire("QUEST_LOG_UPDATE")
	C_Timer.After(1, function() client.turnedIn[questID] = true end)
end

-- UnitLevel still says the old level while PLAYER_LEVEL_UP is handled.
function client.LevelUp(level)
	client.Fire("PLAYER_LEVEL_UP", level, 15, 10, 0, 0, 1, 1, 1, 1)
	C_Timer.After(1, function() client.level = level end)
end

-- A skill line's rank, e.g. after crafting (Cooking sits under a collapsed header).
function client.SetSkill(name, rank)
	for i = 1, C_SkillInfo.GetNumSkillLines() do
		local info = C_SkillInfo.GetSkillLineInfo(i)
		if info.name == name then info.rank = rank end
	end
	local cooking = C_SkillInfo.GetSkillLineInfoByID(185)
	if cooking.name == name then cooking.rank = rank end
	client.Fire("SKILL_LINES_CHANGED")
end

-- The signals of a learn, in order: "event" (NEW_RECIPE_LEARNED), "skillLine"
-- (LEARNED_SPELL_IN_SKILL_LINE), "chat" (the chat line naming it), "link" (the chat line with a
-- spell link, as newer clients write it).
local function Signal(spellID, line, signals)
	local name = client.spellNames[spellID]
	for _, signal in ipairs(signals) do
		if signal == "event" then
			client.Fire("NEW_RECIPE_LEARNED", spellID)
		elseif signal == "skillLine" then
			client.Fire("LEARNED_SPELL_IN_SKILL_LINE", spellID, 3, false)
		elseif signal == "chat" or signal == "link" then
			local what = signal == "link" and string.format("|cff71d5ff|Hspell:%d:0|h[%s]|h|r", spellID, name) or name
			client.Fire("CHAT_MSG_SYSTEM", line:format(what), "", "", "", "", "", 0, 0, "", 0, 0, nil, 0)
		else
			error("no such signal: " .. tostring(signal))
		end
	end
end

-- Learning a recipe from a trainer or a recipe item. Which signals Forever gives isn't known, so a
-- test can pick them (see Signal); all of them by default. The profession window lists it after.
function client.LearnRecipe(spellID, ...)
	client.knownSpells[spellID] = true
	for _, profession in pairs(client.professions) do
		for _, recipe in ipairs(profession.recipes) do
			if recipe.id == spellID then recipe.learned = true end
		end
	end
	Signal(spellID, ERR_LEARN_RECIPE_S or "You have learned how to create a new item: %s.",
		select("#", ...) > 0 and { ... } or { "event", "skillLine", "link" })
end

-- Learning a class spell: the same signals as a recipe, but no NEW_RECIPE_LEARNED and another chat line.
function client.LearnSpell(spellID, ...)
	client.knownSpells[spellID] = true
	Signal(spellID, ERR_LEARN_SPELL_S or "You have learned a new spell: %s.",
		select("#", ...) > 0 and { ... } or { "skillLine", "link" })
end

function client.Enter(map, x, y, zone, subzone)
	client.player.map, client.player.x, client.player.y = map, x, y
	client.zoneText, client.subzoneText = zone, subzone
	client.Fire("ZONE_CHANGED_NEW_AREA")
end
