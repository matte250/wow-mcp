-- The spellbook, talents and spell data (Player.lua's spells and talents, get_spell_details), shaped like Forever's
-- client: Camelot's spellbook reads C_SpellBook by skill line, the professions book by GetProfessionInfo's offset,
-- and the talent window is C_Traits with the class's classic trees as groups of one tree. The player is
-- player.lua's level 12 Dwarf Hunter.

-- Spells by ID: { name, rank (the subtext), passive, level, cost = { power, amount }, cast (ms), range = { min, max },
-- cooldown (s), desc, uncached, loadTime }. An uncached spell has no description until something loads it.
-- Talents' desc has %d for the per-rank value (per).
client.spells = {
	[6603] = { name = "Attack" },
	[75] = { name = "Auto Shot", range = { 8, 35 }, desc = "Automatically shoots the target until cancelled." },
	[2481] = { name = "Find Treasure", rank = "Racial", desc = "Shows the location of all nearby treasure on the minimap." },
	[20594] = { name = "Stoneform", rank = "Racial", cooldown = 180,
		desc = "While active, grants immunity to Bleed, Poison, and Disease effects. Lasts 8 sec." },
	[20595] = { name = "Gun Specialization", rank = "Racial Passive", passive = true, desc = "Guns skill increased by 5." },
	[81] = { name = "Dodge", passive = true },
	[13163] = { name = "Aspect of the Monkey", cost = { "MANA", 20 }, level = 4,
		desc = "The hunter takes on the aspects of a monkey, increasing chance to dodge by 8%." },
	[13165] = { name = "Aspect of the Hawk", rank = "Rank 1", cost = { "MANA", 20 }, level = 10,
		desc = "The hunter takes on the aspects of a hawk, increasing ranged attack power by 20." },
	[1513] = { name = "Scare Beast", rank = "Rank 1", cost = { "MANA", 30 }, cast = 1500, level = 14, range = { 0, 30 },
		cooldown = 30, desc = "Scares a beast, causing it to run in fear for up to 10 sec." },
	[1515] = { name = "Tame Beast", cost = { "MANA", 48 }, cast = 20000, level = 10, range = { 0, 30 } },
	[883] = { name = "Call Pet", level = 10 },
	[136] = { name = "Mend Pet", rank = "Rank 1", cost = { "MANA", 40 }, cast = 5000, level = 12, range = { 0, 20 } },
	[3044] = { name = "Arcane Shot", rank = "Rank 1", cost = { "MANA", 25 }, level = 6, range = { 8, 35 }, cooldown = 6,
		desc = "An instant shot that causes 13 Arcane damage." },
	[1130] = { name = "Hunter's Mark", rank = "Rank 1", cost = { "MANA", 15 }, level = 6, range = { 0, 100 } },
	[1978] = { name = "Serpent Sting", rank = "Rank 1", cost = { "MANA", 15 }, level = 4, range = { 8, 35 },
		desc = "Stings the target, causing 20 Nature damage over 15 sec." },
	[13549] = { name = "Serpent Sting", rank = "Rank 2", cost = { "MANA", 30 }, level = 10, range = { 8, 35 },
		desc = "Stings the target, causing 40 Nature damage over 15 sec." },
	[2973] = { name = "Raptor Strike", rank = "Rank 1", cost = { "MANA", 15 }, level = 1, range = { 0, 5 }, cooldown = 6 },
	[14260] = { name = "Raptor Strike", rank = "Rank 2", cost = { "MANA", 25 }, level = 8, range = { 0, 5 }, cooldown = 6,
		desc = "A strong attack that increases melee damage by 11." },
	[1494] = { name = "Track Beasts", level = 4 },
	[19883] = { name = "Track Humanoids", level = 10 }, -- another specialization's in the stub (greyed out)
	[2108] = { name = "Leatherworking", rank = "Apprentice" },
	[8613] = { name = "Skinning", rank = "Journeyman", passive = true },
	[2550] = { name = "Cooking", rank = "Apprentice" },
	[818] = { name = "Basic Campfire", cast = 10000 },
	[17253] = { name = "Bite", rank = "Rank 1", cost = { "FOCUS", 35 }, range = { 0, 5 }, cooldown = 10 },
	[2649] = { name = "Growl", rank = "Rank 1", cost = { "FOCUS", 15 }, range = { 0, 5 }, cooldown = 5 },
	-- Not the player's.
	[133] = { name = "Fireball", rank = "Rank 1", cost = { "MANA", 30 }, cast = 1500, range = { 0, 35 }, level = 1,
		desc = "Hurls a fiery ball that causes 16 to 25 Fire damage and an additional 2 Fire damage over 4 sec." },
	[5019] = { name = "Shoot", range = { 0, 30 }, uncached = true, loadTime = 20, desc = "Shoot with a wand." },
}

-- The spellbook as the client lists it: skill lines (the tabs, in order) { name, hide, guild, items }, the
-- professions as GetProfessions() orders them (primary 1 and 2, first aid, fishing, cooking), and the pet's items.
-- An item is { id, future = level (not learned yet), low = true (a lower rank of one known), offSpec = true }.
client.spellbook = {
	lines = {
		{ name = "General", items = { { id = 6603 }, { id = 75 }, { id = 2481 }, { id = 20594 }, { id = 20595 }, { id = 81 } } },
		{ name = "Beast Mastery", items = { { id = 13163 }, { id = 13165, future = 10 }, { id = 1513, future = 14 },
			{ id = 1515 }, { id = 883 }, { id = 136 } } },
		{ name = "Marksmanship", items = { { id = 3044 }, { id = 1130 }, { id = 1978, low = true }, { id = 13549 } } },
		{ name = "Survival", items = { { id = 2973, low = true }, { id = 14260 }, { id = 1494 }, { id = 19883, offSpec = true } } },
		{ name = "Hidden", hide = true, items = { { id = 133 } } },
	},
	professions = {
		[1] = { name = "Leatherworking", rank = 60, maxRank = 75, items = { { id = 2108 } } },
		[2] = { name = "Skinning", rank = 87, maxRank = 150, items = { { id = 8613 } } },
		[5] = { name = "Cooking", rank = 40, maxRank = 75, items = { { id = 2550 }, { id = 818 } } },
	},
	pet = { { id = 17253 }, { id = 2649 } },
}
client.cooldowns = { [14260] = 0 } -- spell ID -> when its cooldown started (client.now)

-- Talents: one tree whose groups are the class's trees, as Forever's talent window shows them. A talent is { name,
-- spell, max, tier, col, rank (applied), pending (picked, not applied), after (the talent it needs at max rank),
-- desc, per }. A tier needs 5 points per tier above it in its tree; earned is the points the character has had.
local function Tree(name, list)
	local tree = { name = name, talents = {} }
	for _, t in ipairs(list) do
		table.insert(tree.talents, { name = t[1], spell = t[2], max = t[3], tier = t[4], col = t[5], after = t[6], rank = 0 })
	end
	return tree
end
client.talents = {
	config = 7001, tree = 801, earned = 3, group = 1, groups = 1,
	trees = {
		Tree("Beast Mastery", {
			{ "Improved Aspect of the Hawk", 19552, 5, 1, 2 }, { "Endurance Training", 19583, 5, 1, 3 },
			{ "Improved Eyes of the Beast", 19557, 2, 2, 1 }, { "Improved Aspect of the Monkey", 19549, 5, 2, 2 },
			{ "Thick Hide", 19609, 3, 2, 3 }, { "Improved Revive Pet", 19575, 2, 2, 4 },
			{ "Pathfinding", 19559, 2, 3, 1 }, { "Bestial Swiftness", 19596, 1, 3, 2 }, { "Unleashed Fury", 19616, 5, 3, 3 },
			{ "Improved Mend Pet", 19572, 2, 4, 2 }, { "Ferocity", 19598, 5, 4, 3 },
			{ "Spirit Bond", 19578, 2, 5, 1 }, { "Intimidation", 19577, 1, 5, 2 }, { "Bestial Discipline", 19590, 2, 5, 4 },
			{ "Frenzy", 19621, 5, 6, 3, "Ferocity" }, { "Bestial Wrath", 19574, 1, 7, 2, "Intimidation" },
		}),
		Tree("Marksmanship", {
			{ "Improved Concussive Shot", 19407, 5, 1, 2 }, { "Efficiency", 19416, 5, 1, 3 },
			{ "Improved Hunter's Mark", 19421, 5, 2, 2 }, { "Lethal Shots", 19426, 5, 2, 3 },
			{ "Aimed Shot", 19434, 1, 3, 1 }, { "Improved Arcane Shot", 19454, 5, 3, 2 }, { "Hawk Eye", 19498, 3, 3, 4 },
			{ "Improved Serpent Sting", 19464, 5, 4, 2 }, { "Mortal Shots", 19485, 5, 4, 3 },
			{ "Scatter Shot", 19503, 1, 5, 1 }, { "Barrage", 19461, 3, 5, 2 }, { "Improved Scorpid Sting", 19491, 3, 5, 3 },
			{ "Ranged Weapon Specialization", 19507, 5, 6, 3 }, { "Trueshot Aura", 19506, 1, 7, 2, "Aimed Shot" },
		}),
		Tree("Survival", {
			{ "Monster Slaying", 24293, 3, 1, 1 }, { "Humanoid Slaying", 19151, 3, 1, 2 }, { "Deflection", 19295, 5, 1, 3 },
			{ "Entrapment", 19184, 5, 2, 1 }, { "Savage Strikes", 19159, 2, 2, 2 }, { "Improved Wing Clip", 19228, 5, 2, 3 },
			{ "Clever Traps", 19239, 2, 3, 1 }, { "Survivalist", 19255, 5, 3, 2 }, { "Deterrence", 19263, 1, 3, 3 },
			{ "Trap Mastery", 19376, 2, 4, 1 }, { "Surefooted", 19290, 3, 4, 3 },
			{ "Improved Feign Death", 19286, 2, 5, 1 }, { "Killer Instinct", 19370, 3, 5, 2 },
			{ "Counterattack", 19306, 1, 5, 3, "Deterrence" }, { "Lightning Reflexes", 24297, 5, 6, 3 },
			{ "Wyvern Sting", 19386, 1, 7, 2 },
		}),
	},
}
-- Efficiency 2/5; a point unspent; what Efficiency does per rank.
client.talents.trees[2].talents[2].rank = 2
client.talents.trees[2].talents[2].desc = "Reduces the Mana cost of your Shots and Stings by %d%%."
client.talents.trees[2].talents[2].per = 2
-- A talent's spell is a passive spell of its own, whose tooltip has rank 1's numbers.
for _, tree in ipairs(client.talents.trees) do
	for _, talent in ipairs(tree.talents) do
		client.spells[talent.spell] = { name = talent.name, passive = true,
			desc = talent.desc and string.format(talent.desc, talent.per or 1) }
	end
end

---------------------------------------------------------------------------

local WHITE, RED, GOLD = { r = 1, g = 1, b = 1 }, { r = 1, g = 0.125, b = 0.125 }, { r = 1, g = 0.82, b = 0 }
local SPELL, FUTURE, PET_ACTION = 1, 2, 3

local function Cached(spellID)
	local spell = client.spells[spellID]
	return spell ~= nil and not spell.uncached
end

local function Load(spellID)
	local spell = client.spells[spellID]
	if not spell or not spell.uncached then
		C_Timer.After(0, function() client.Fire("SPELL_DATA_LOAD_RESULT", spellID, spell ~= nil) end)
	elseif not spell.loading then
		spell.loading = true
		C_Timer.After(spell.loadTime or 0, function()
			spell.uncached, spell.loading = nil, nil
			client.Fire("SPELL_DATA_LOAD_RESULT", spellID, true)
		end)
	end
end

local function Cooldown(spellID)
	local started, spell = client.cooldowns[spellID], client.spells[spellID]
	if not (started and spell and spell.cooldown) then return { startTime = 0, duration = 0, isEnabled = true, modRate = 1 } end
	return { startTime = started, duration = spell.cooldown, isEnabled = true, isActive = true, modRate = 1, isOnGCD = false }
end

-- Player bank slots: the skill lines' items in order, then the professions'. { [slot] = { item, line } }, and
-- each line's and profession's offset.
local function Slots()
	local slots, offset, offsets = {}, 0, { lines = {}, professions = {} }
	for i, line in ipairs(client.spellbook.lines) do
		offsets.lines[i] = offset
		for _, item in ipairs(line.items) do
			offset = offset + 1
			slots[offset] = { item = item, line = i }
		end
	end
	for i = 1, 5 do
		local profession = client.spellbook.professions[i]
		if profession then
			offsets.professions[i] = offset
			for _, item in ipairs(profession.items) do
				offset = offset + 1
				slots[offset] = { item = item }
			end
		end
	end
	return slots, offsets
end

local function Item(slot, bank)
	if bank == 1 then return client.spellbook.pet[slot], nil end
	local entry = Slots()[slot]
	if entry then return entry.item, entry.line end
end

C_SpellBook = {
	GetNumSpellBookSkillLines = function() return #client.spellbook.lines end,
	GetSpellBookSkillLineInfo = function(index)
		local line = client.spellbook.lines[index]
		if not line then return nil end
		local _, offsets = Slots()
		return { name = line.name, iconID = 136243, itemIndexOffset = offsets.lines[index], numSpellBookItems = #line.items,
			isGuild = line.guild or false, shouldHide = line.hide or false }
	end,
	GetSpellBookItemInfo = function(slot, bank)
		local item, line = Item(slot, bank)
		local spell = item and client.spells[item.id]
		if not spell then return nil end
		local kind = item.future and FUTURE or bank == 1 and PET_ACTION or SPELL
		return { actionID = item.id, spellID = item.id, itemType = kind, name = spell.name, subName = spell.rank or "",
			iconID = 136243, isPassive = spell.passive or false, isOffSpec = item.offSpec or false, skillLineIndex = line }
	end,
	IsSpellBookItemLowRank = function(slot, bank)
		local item = Item(slot, bank)
		return item and item.low or false
	end,
	GetSpellBookItemLevelLearned = function(slot, bank)
		local item = Item(slot, bank)
		local spell = item and client.spells[item.id]
		return item and (item.future or spell and spell.level) or 0
	end,
	-- Nothing for spells not learned yet, as the client.
	GetSpellBookItemCooldown = function(slot, bank)
		local item = Item(slot, bank)
		if item and not item.future then return Cooldown(item.id) end
	end,
	HasPetSpells = function()
		if #client.spellbook.pet > 0 then return #client.spellbook.pet, "PET" end
	end,
}

function GetProfessions()
	local p = client.spellbook.professions
	return p[1] and 1, p[2] and 2, p[3] and 3, p[4] and 4, p[5] and 5
end

function GetProfessionInfo(index)
	local profession = client.spellbook.professions[index]
	local _, offsets = Slots()
	return profession.name, 136246, profession.rank, profession.maxRank, #profession.items, offsets.professions[index],
		index + 160, 0, 0, 0, profession.name
end

C_Spell = {
	DoesSpellExist = function(spellID) return client.spells[spellID] ~= nil end,
	IsSpellDataCached = Cached,
	RequestLoadSpellData = Load,
	GetSpellInfo = function(spellID)
		local spell = client.spells[spellID]
		if not spell then return nil end
		local range = spell.range or { 0, 0 }
		return { name = spell.name, iconID = 136243, originalIconID = 136243, castTime = spell.cast or 0,
			minRange = range[1], maxRange = range[2], spellID = spellID }
	end,
	GetSpellSubtext = function(spellID)
		local spell = client.spells[spellID]
		return spell and (spell.rank or "")
	end,
	GetSpellDescription = function(spellID)
		local spell = client.spells[spellID]
		if spell then return Cached(spellID) and spell.desc or "" end
	end,
	GetSpellPowerCost = function(spellID)
		local spell = client.spells[spellID]
		if not (spell and spell.cost) then return nil end
		return { { type = 0, name = spell.cost[1], cost = spell.cost[2], minCost = spell.cost[2], costPercent = 0,
			costPerSec = 0, requiredAuraID = 0, hasRequiredAura = false } }
	end,
	GetSpellCooldown = function(spellID)
		if client.spells[spellID] then return Cooldown(spellID) end
	end,
	IsSpellPassive = function(spellID)
		local spell = client.spells[spellID]
		return spell ~= nil and spell.passive == true
	end,
}

---------------------------------------------------------------------------
-- Talents
---------------------------------------------------------------------------

-- Every talent with its IDs: node 9000 + n, entry 19000 + n, definition 29000 + n; a tier's gate condition is
-- 39000 + tree * 10 + tier.
local function Talents()
	local list, byNode, byEntry, byName = {}, {}, {}, {}
	for t, tree in ipairs(client.talents.trees) do
		for _, talent in ipairs(tree.talents) do
			local n = #list + 1
			local node = { n = n, tree = t, groupID = 1000 + t, talent = talent, node = 9000 + n, entry = 19000 + n }
			list[n], byNode[node.node], byEntry[node.entry], byName[talent.name] = node, node, node, node
		end
	end
	return list, byNode, byEntry, byName
end

local function Spent(t, current)
	local spent = 0
	for _, talent in ipairs(client.talents.trees[t].talents) do
		spent = spent + (current and talent.pending or talent.rank)
	end
	return spent
end

local function Unspent(current)
	local spent = 0
	for t in ipairs(client.talents.trees) do spent = spent + Spent(t, current) end
	return client.talents.earned - spent, spent
end

local function Maxed(talent) return talent.rank >= talent.max end

local function NodeInfo(nodeID)
	local list, byNode, _, byName = Talents()
	local node = byNode[nodeID]
	if not node then return nil end
	local talent = node.talent
	local available = Spent(node.tree) >= (talent.tier - 1) * 5
	local prerequisite = talent.after and byName[talent.after]
	local meets = not prerequisite or Maxed(prerequisite.talent)
	local current = talent.pending or talent.rank
	local edges = {}
	for _, other in ipairs(list) do
		if other.talent.after == talent.name then
			table.insert(edges, { targetNode = other.node, type = 3, visualStyle = 1, isActive = Maxed(talent) })
		end
	end
	return {
		ID = nodeID, posX = node.tree * 2400 + talent.col * 600, posY = 1200 + talent.tier * 600, flags = 0,
		entryIDs = { node.entry }, entryIDsWithCommittedRanks = talent.rank > 0 and { node.entry } or {},
		canPurchaseRank = available and meets and current < talent.max and Unspent(true) > 0,
		canRefundRank = current > 0, isAvailable = available, isVisible = true, isDisplayError = false,
		ranksPurchased = current, ranksIncreased = 0, entryIDToRanksIncreased = {}, activeRank = talent.rank,
		currentRank = current, activeEntry = { entryID = node.entry, rank = talent.rank },
		nextEntry = current < talent.max and { entryID = node.entry, rank = current + 1 } or nil,
		maxRanks = talent.max, totalMaxRanks = talent.max, type = 0, visibleEdges = edges,
		meetsEdgeRequirements = meets, groupIDs = { node.groupID }, conditionIDs = {}, isCascadeRepurchasable = false,
	}
end

C_Traits = {
	GetConfigInfo = function(configID)
		if client.talents and configID == client.talents.config then
			return { ID = configID, type = 1, name = "", treeIDs = { client.talents.tree }, usesSharedActionBars = false }
		end
	end,
	GetGroupDisplayInfoByTreeID = function(treeID)
		local list = {}
		for t, tree in ipairs(client.talents.trees) do
			list[t] = { groupID = 1000 + t, treeID = treeID, skillLineID = 50 + t, orderIndex = t, displayName = tree.name,
				icon = 136243 }
		end
		return list
	end,
	-- The window's tree headers: points spent per tree, picks not applied yet too.
	GetGroupCurrencyInfo = function(_, groupIDs)
		local list = {}
		for i, groupID in ipairs(groupIDs) do
			local spent = Spent(groupID - 1000, true)
			list[i] = { traitNodeGroupID = groupID, currencyInfos = { { traitCurrencyID = 1, quantity = 0, spent = spent } } }
		end
		return list
	end,
	GetTreeCurrencyInfo = function(_, _, excludeStagedChanges)
		local unspent, spent = Unspent(not excludeStagedChanges)
		return { { traitCurrencyID = 1, quantity = unspent, spent = spent } }
	end,
	GetTreeNodes = function()
		local ids = {}
		for i, node in ipairs((Talents())) do ids[i] = node.node end
		return ids
	end,
	GetNodeInfo = function(_, nodeID) return NodeInfo(nodeID) end,
	GetEntryInfo = function(_, entryID)
		local _, _, byEntry = Talents()
		local node = byEntry[entryID]
		if not node then return nil end
		local gate = node.talent.tier > 1 and { 39000 + node.tree * 10 + node.talent.tier } or {}
		return { definitionID = 29000 + node.n, type = 1, maxRanks = node.talent.max, isAvailable = true,
			isDisplayError = false, conditionIDs = gate }
	end,
	GetDefinitionInfo = function(definitionID)
		local list = Talents()
		local node = list[definitionID - 29000]
		return node and { spellID = node.talent.spell }
	end,
	GetConditionInfo = function(_, condID)
		local t, tier = math.floor((condID - 39000) / 10), (condID - 39000) % 10
		local needs = (tier - 1) * 5
		return { condID = condID, isAlwaysMet = false, isMet = Spent(t) >= needs, isGate = true, isSufficient = false,
			type = 0, spentAmountRequired = needs, traitCurrencyID = 1, tooltipFormat = "Requires %d points in %s Talents" }
	end,
}

C_ClassTalents = {
	GetActiveConfigID = function() return client.talents and client.talents.config end,
}

function GetNumSpecGroups() return client.talents and client.talents.groups or 1 end

---------------------------------------------------------------------------
-- Tooltips
---------------------------------------------------------------------------

local function Line(lines, left, color, right)
	lines[#lines + 1] = { type = 0, leftText = left, leftColor = color or WHITE, rightText = right, rightColor = right and WHITE }
end

local function Seconds(s)
	if s >= 60 then return string.format("%g min", s / 60) end
	return string.format("%g sec", s)
end

-- A spell's tooltip as the client builds it: name and rank, cost and range, cast time and cooldown, required level,
-- what it does.
local function SpellTooltip(spellID)
	local spell = client.spells[spellID]
	if not spell then return nil end
	local lines = {}
	Line(lines, spell.name, WHITE, spell.rank)
	local range = spell.range and spell.range[2] > 5 and spell.range[2] .. " yd range" or spell.range and "Melee Range" or nil
	if spell.cost or range then
		Line(lines, spell.cost and string.format("%d %s", spell.cost[2], spell.cost[1]:sub(1, 1) .. spell.cost[1]:sub(2):lower()) or "", WHITE, range)
	end
	if not spell.passive then
		local cast = (spell.cast or 0) == 0 and "Instant" or Seconds(spell.cast / 1000) .. " cast"
		Line(lines, cast, WHITE, spell.cooldown and Seconds(spell.cooldown) .. " cooldown")
	end
	if spell.level and spell.level > UnitLevel("player") then Line(lines, "Requires Level " .. spell.level, RED) end
	if Cached(spellID) and spell.desc then Line(lines, spell.desc, GOLD) end
	return { type = Enum.TooltipDataType.Spell, id = spellID, lines = lines }
end

-- What a talent does at a rank, as the talent window adds it.
local function TraitTooltip(entryID, rank)
	local _, _, byEntry = Talents()
	local node = byEntry[entryID]
	if not node or not node.talent.desc then return nil end
	local lines = {}
	Line(lines, string.format(node.talent.desc, (rank or 1) * (node.talent.per or 1)), GOLD)
	return { type = 0, id = entryID, lines = lines }
end

-- Blizzard's own UI is made anew on every load: C_SpecializationInfo (upgrades.lua's) gets the spec groups, and
-- C_Spell.GetSpellName (plans.lua's) knows these spells.
table.insert(client.onLoad, function()
	if C_SpecializationInfo then
		C_SpecializationInfo.GetActiveSpecGroup = function() return client.talents and client.talents.group or 1 end
		C_SpecializationInfo.GetCombatConfigIDForSpecGroup = function(group)
			if client.talents and group == client.talents.group then return client.talents.config end
		end
	end
	if C_TooltipInfo then
		C_TooltipInfo.GetSpellByID = SpellTooltip
		C_TooltipInfo.GetTraitEntry = TraitTooltip
	end
	for id, spell in pairs(client.spells) do client.spellNames[id] = client.spellNames[id] or spell.name end
	for _, tree in ipairs(client.talents and client.talents.trees or {}) do
		for _, talent in ipairs(tree.talents) do client.spellNames[talent.spell] = client.spellNames[talent.spell] or talent.name end
	end
end)
