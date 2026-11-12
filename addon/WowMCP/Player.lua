-- WowMCP player: where the player is, who they are and their professions (request kind STATE); their spellbook and
-- talents (kind REPUTATION); any spell's tooltip and numbers by ID (kind ITEM_DETAILS).

local _, ns = ...

-- Skill line IDs, for lines hidden under a collapsed header in the Skills window.
local PRIMARY = { [164] = true, [165] = true, [171] = true, [182] = true, [186] = true, [197] = true, [202] = true, [333] = true, [393] = true }
local SECONDARY = { [129] = true, [185] = true, [356] = true }
local CATEGORY_SECONDARY, CATEGORY_PROFESSIONS = 9, 11

local function ContinentName(mapID)
	for _ = 1, 20 do
		local info = mapID and C_Map.GetMapInfo(mapID)
		if not info then return nil end
		if info.mapType == Enum.UIMapType.Continent then return info.name end
		mapID = info.parentMapID
	end
end

local function Location()
	local mapID = C_Map.GetBestMapForUnit("player")
	local info = mapID and C_Map.GetMapInfo(mapID)
	local pos = mapID and C_Map.GetPlayerMapPosition(mapID, "player")
	local x, y
	if pos then
		x, y = pos.x, pos.y
		x, y = math.floor(x * 10000 + 0.5) / 100, math.floor(y * 10000 + 0.5) / 100
	end
	return {
		mapID = mapID,
		zone = info and info.name or GetRealZoneText(),
		subzone = GetSubZoneText(),
		continent = ContinentName(mapID),
		x = x,
		y = y,
		inInstance = IsInInstance() and true or false,
	}
end

-- The talent specialization the character frame names (its "primary talent tree" in Forever's Camelot code), and
-- its role ("DAMAGER", "TANK", "HEALER"); nothing before the character has one.
local function Spec()
	local index = C_SpecializationInfo.GetSpecialization()
	if not index or index == 0 then return nil end
	local _, name, _, _, role = C_SpecializationInfo.GetSpecializationInfo(index)
	if name and name ~= "" then return name, role end
end

local function Character()
	local class, classFile = UnitClass("player")
	local character = {
		name = UnitName("player"),
		faction = UnitFactionGroup("player"),
		race = UnitRace("player"),
		class = class,
		classFile = classFile,
		level = UnitLevel("player"),
	}
	local ok, spec, role = pcall(Spec)
	if ok then character.spec, character.role = spec, role end
	return character
end

local function Professions()
	local found, list = {}, {}
	local function Add(info)
		if type(info) ~= "table" or info.isHeader or (info.parentSkillLineID or 0) ~= 0 or found[info.skillID] then return end
		local primary = info.skillLineCategoryID == CATEGORY_PROFESSIONS or PRIMARY[info.skillID]
		local secondary = info.skillLineCategoryID == CATEGORY_SECONDARY or SECONDARY[info.skillID]
		if not (primary or secondary) or (info.maxRank or 0) <= 0 then return end
		found[info.skillID] = true
		list[#list + 1] = { name = info.name, rank = info.rank, maxRank = info.maxRank, primary = primary and true or false }
	end
	for i = 1, C_SkillInfo.GetNumSkillLines() do Add(C_SkillInfo.GetSkillLineInfo(i)) end
	for _, ids in ipairs({ PRIMARY, SECONDARY }) do
		for skillID in pairs(ids) do
			if not found[skillID] then Add(C_SkillInfo.GetSkillLineInfoByID(skillID)) end
		end
	end
	return list
end

ns.RegisterSection(ns.Kinds.STATE, "location", Location)
ns.RegisterSection(ns.Kinds.STATE, "character", Character)
ns.RegisterSection(ns.Kinds.STATE, "professions", Professions)

---------------------------------------------------------------------------
-- Spells and talents (get_spells, get_talents, get_spell_details)
---------------------------------------------------------------------------

-- The spellbook and talent trees are a few KB, so they're sections of kind REPUTATION, which only get_reputations
-- asks for besides: on STATE, every location request (most answers start with one) would carry them. They're read
-- the way Forever's own UI reads them: Camelot's classic-style spellbook (a tab per skill line, then the professions
-- book's slots and the pet's), and its talent window, retail's C_Traits with the class's trees as groups of one tree.
do
	local PLAYER_BANK, PET_BANK = 0, 1 -- Enum.SpellBookSpellBank
	local SPELL, FUTURE_SPELL, PET_ACTION = 1, 2, 3 -- Enum.SpellBookItemType; a flyout's spells are listed loose too
	local GENERAL = 1 -- Enum.SpellBookSkillLineIndex
	local ARROWS = { [2] = true, [3] = true } -- Enum.TraitEdgeType: Sufficient-, RequiredForAvailability
	local GCD = 1.5
	local MAX_LOOKUP = 20
	local KEEP = 120 -- seconds a spell lookup is answered; then other ITEM_DETAILS replies stop carrying it

	local function Subtext(spellID)
		local text = C_Spell and C_Spell.GetSpellSubtext and C_Spell.GetSpellSubtext(spellID)
		if text ~= "" then return text end
	end

	local function Left(info)
		if not info.isEnabled or info.isOnGCD or info.duration <= GCD then return nil end
		local left = info.startTime + info.duration - GetTime()
		if left >= 0.5 then return math.floor(left + 0.5) end
	end

	-- Seconds left on a cooldown, from get(...)'s SpellCooldownInfo; nil when it's ready, only the global cooldown,
	-- or secret (restricted content).
	local function CooldownLeft(get, ...)
		local ok, info = pcall(get, ...)
		if ok and type(info) == "table" then
			local read, left = pcall(Left, info)
			return read and left or nil
		end
	end

	local function LowRank(slot, bank)
		return C_SpellBook.IsSpellBookItemLowRank and C_SpellBook.IsSpellBookItemLowRank(slot, bank)
	end

	-- The spells in a bank's slots first+1 to first+count: { id, name, rank, passive, cd, lower }, or { ..., learn }
	-- (the level it's learned at) for one the spellbook lists as not learned yet. Lower ranks go with the highest
	-- known (lower: their IDs, lowest first), as the spellbook shows them unless "Show all spell ranks" is on.
	-- Another specialization's spells (greyed out) are left out. seen: slots read already.
	local function Read(bank, first, count, seen)
		local spells, low = {}, {}
		for slot = first + 1, first + count do
			local info = not seen[slot] and C_SpellBook.GetSpellBookItemInfo(slot, bank)
			local kind = info and info.itemType
			if info then seen[slot] = true end
			local listed = kind == SPELL or kind == FUTURE_SPELL or kind == PET_ACTION
			if listed and info.spellID and not info.isOffSpec then
				local rank = info.subName ~= "" and info.subName or Subtext(info.spellID)
				local spell = { id = info.spellID, name = info.name, rank = rank, passive = info.isPassive or nil }
				if kind == FUTURE_SPELL then
					spell.learn = C_SpellBook.GetSpellBookItemLevelLearned(slot, bank)
					spells[#spells + 1] = spell
				elseif LowRank(slot, bank) then
					low[spell.name] = low[spell.name] or {}
					table.insert(low[spell.name], spell)
				else
					spell.cd = CooldownLeft(C_SpellBook.GetSpellBookItemCooldown, slot, bank)
					spells[#spells + 1] = spell
				end
			end
		end
		for _, spell in ipairs(spells) do
			local ranks = not spell.learn and low[spell.name]
			if ranks then
				spell.lower, low[spell.name] = {}, nil
				for i, lower in ipairs(ranks) do spell.lower[i] = lower.id end
			end
		end
		for _, ranks in pairs(low) do -- no higher rank listed with them
			for _, lower in ipairs(ranks) do spells[#spells + 1] = lower end
		end
		return spells
	end

	-- Professions' spells have slots of their own past the tabs', which the professions book finds by
	-- GetProfessionInfo's offset. GetProfessions() has gaps (nil) for professions the character lacks.
	local function AddProfessions(tabs, seen, ...)
		for i = 1, select("#", ...) do
			local index = select(i, ...)
			local name, count, offset, _
			if index then name, _, _, _, count, offset = GetProfessionInfo(index) end
			local spells = name and (count or 0) > 0 and Read(PLAYER_BANK, offset, count, seen)
			if spells and #spells > 0 then tabs[#tabs + 1] = { name = name, kind = "profession", spells = spells } end
		end
	end

	local function Spellbook()
		if not (C_SpellBook and C_SpellBook.GetNumSpellBookSkillLines and C_SpellBook.GetSpellBookItemInfo) then
			return { problem = "This game client has no spellbook API (C_SpellBook)." }
		end
		local tabs, seen = {}, {}
		for index = 1, C_SpellBook.GetNumSpellBookSkillLines() do
			local line = C_SpellBook.GetSpellBookSkillLineInfo(index)
			if line and not line.shouldHide then
				tabs[#tabs + 1] = {
					name = line.name, kind = index == GENERAL and "general" or line.isGuild and "guild" or "class",
					spells = Read(PLAYER_BANK, line.itemIndexOffset, line.numSpellBookItems, seen),
				}
			end
		end
		if GetProfessions and GetProfessionInfo then AddProfessions(tabs, seen, GetProfessions()) end
		local pets = C_SpellBook.HasPetSpells and C_SpellBook.HasPetSpells()
		if pets and pets > 0 and (not PetHasSpellbook or PetHasSpellbook()) then
			tabs[#tabs + 1] = { name = PET or "Pet", kind = "pet", spells = Read(PET_BANK, 0, pets, {}) }
		end
		return { class = UnitClass("player"), level = UnitLevel("player"), tabs = tabs }
	end

	-- The config the talent window shows: the active spec group's (Camelot's ClassTalentsFrame:SetTab), else the
	-- active one (its RefreshConfigID).
	local function TalentConfig()
		local specs = C_SpecializationInfo
		local group = specs.GetActiveSpecGroup and specs.GetActiveSpecGroup() or 1
		local config = specs.GetCombatConfigIDForSpecGroup and specs.GetCombatConfigIDForSpecGroup(group)
		if not config and C_ClassTalents and C_ClassTalents.GetActiveConfigID then
			config = C_ClassTalents.GetActiveConfigID()
		end
		return config, group
	end

	-- The most points a talent's conditions want spent in its tree first (its tier's gate), if any.
	local function Gate(config, conditions, needs)
		for _, condID in ipairs(conditions or {}) do
			local cond = C_Traits.GetConditionInfo(config, condID)
			if cond and cond.isGate and (cond.spentAmountRequired or 0) > (needs or 0) then needs = cond.spentAmountRequired end
		end
		return needs
	end

	-- An entry's name as the talent window gives it (TalentUtil.GetTalentName), its spell, and the entry.
	local function Entry(config, entryID)
		local entry = entryID and C_Traits.GetEntryInfo(config, entryID)
		local definition = entry and entry.definitionID and C_Traits.GetDefinitionInfo(entry.definitionID)
		if not definition then return nil, nil, entry end
		local name = definition.overrideName
		if not name or name == "" then name = definition.spellID and C_Spell and C_Spell.GetSpellName(definition.spellID) end
		return name, definition.spellID, entry
	end

	-- Tiers are a tree's rows, top down; its talents go top down, left to right.
	local function Tiers(tree)
		local rows, tier = {}, {}
		for _, t in ipairs(tree.talents) do
			if not tier[t.y] then
				tier[t.y] = true
				rows[#rows + 1] = t.y
			end
		end
		table.sort(rows)
		for i, y in ipairs(rows) do tier[y] = i end
		for _, t in ipairs(tree.talents) do t.tier = tier[t.y] end
		table.sort(tree.talents, function(a, b) return a.y < b.y or a.y == b.y and a.x < b.x end)
	end

	-- { group, unspent, spent, trees = { { name, spent, talents } }, bySpell }; each talent { entry, name, spell, rank,
	-- max, tier, needs, after, open, pending, choices }. Ranks are the applied ones; pending is one picked in the
	-- window but not applied. nil while the character has no talent config.
	local function ReadTalents()
		local config, group = TalentConfig()
		local info = config and C_Traits.GetConfigInfo(config)
		local treeID = info and info.treeIDs and info.treeIDs[1]
		if not treeID then return nil end
		local read = { group = group, trees = {}, bySpell = {} }
		local treeOf, groupIDs, other = {}, {}, nil
		for _, display in ipairs(C_Traits.GetGroupDisplayInfoByTreeID(treeID) or {}) do
			local tree = { name = display.displayName, talents = {} }
			read.trees[#read.trees + 1] = tree
			treeOf[display.groupID] = tree
			groupIDs[#groupIDs + 1] = display.groupID
		end
		-- Points spent per tree, as the window's tree headers show them.
		for _, g in ipairs(#groupIDs > 0 and C_Traits.GetGroupCurrencyInfo(config, groupIDs) or {}) do
			local currency = treeOf[g.traitNodeGroupID] and g.currencyInfos and g.currencyInfos[1]
			if currency then treeOf[g.traitNodeGroupID].spent = currency.spent end
		end
		-- Unspent as applied, like the ranks (the window's count takes picks not applied yet off).
		local currency = (C_Traits.GetTreeCurrencyInfo(config, treeID, true) or {})[1]
		if currency then read.unspent, read.spent = currency.quantity, currency.spent end

		local byNode, incoming = {}, {}
		for _, nodeID in ipairs(C_Traits.GetTreeNodes(treeID) or {}) do
			local node = C_Traits.GetNodeInfo(config, nodeID)
			if node and node.isVisible and (node.ID or 0) ~= 0 then
				local active = node.activeEntry and node.activeEntry.entryID
				local talent = { entry = active or node.entryIDs[1], rank = node.activeRank, max = node.maxRanks,
					x = node.posX, y = node.posY }
				local entry
				talent.name, talent.spell, entry = Entry(config, talent.entry)
				if #node.entryIDs > 1 then
					talent.choices = {}
					for i, id in ipairs(node.entryIDs) do talent.choices[i] = Entry(config, id) or "?" end
					if not active then talent.name = table.concat(talent.choices, " / ") end
				end
				talent.needs = Gate(config, node.conditionIDs, Gate(config, entry and entry.conditionIDs))
				-- Open: its tier and the talents it needs are there and it isn't maxed, so a point can go in (the
				-- window shows it green while there's one to spend).
				talent.open = node.isAvailable and node.meetsEdgeRequirements and node.activeRank < node.maxRanks or nil
				if node.currentRank ~= node.activeRank then talent.pending = node.currentRank end
				for _, edge in ipairs(node.visibleEdges or {}) do
					if ARROWS[edge.type] then
						incoming[edge.targetNode] = incoming[edge.targetNode] or {}
						table.insert(incoming[edge.targetNode], nodeID)
					end
				end
				local tree
				for _, groupID in ipairs(node.groupIDs or {}) do tree = tree or treeOf[groupID] end
				if not tree then -- under none of the window's trees
					other = other or { name = "Other", talents = {} }
					tree = other
				end
				table.insert(tree.talents, talent)
				byNode[nodeID] = talent
				if talent.spell then read.bySpell[talent.spell] = talent end
			end
		end
		read.trees[#read.trees + 1] = other
		for target, sources in pairs(incoming) do
			local talent = byNode[target]
			for _, source in ipairs(talent and sources or {}) do
				talent.after = talent.after or {}
				if byNode[source] then table.insert(talent.after, byNode[source].name) end
			end
		end
		for _, tree in ipairs(read.trees) do
			Tiers(tree)
			if not tree.spent then
				tree.spent = 0
				for _, t in ipairs(tree.talents) do tree.spent = tree.spent + t.rank end
			end
		end
		return read
	end

	local function Talents()
		if not (C_Traits and C_Traits.GetTreeNodes and C_SpecializationInfo) then
			return { problem = "This game client has no talent API (C_Traits)." }
		end
		local talents = { class = UnitClass("player"), level = UnitLevel("player"), trees = {} }
		local ok, spec, role = pcall(Spec)
		if ok then talents.spec, talents.role = spec, role end
		local read = ReadTalents()
		if not read then
			talents.none = true
			return talents
		end
		talents.unspent, talents.spent = read.unspent, read.spent
		local groups = GetNumSpecGroups and GetNumSpecGroups() or 1
		if groups > 1 then talents.group, talents.groups = read.group, groups end
		for i, tree in ipairs(read.trees) do
			local list = {}
			for j, t in ipairs(tree.talents) do
				list[j] = { name = t.name, spell = t.spell, rank = t.rank, max = t.max, tier = t.tier, needs = t.needs,
					after = t.after, open = t.open, pending = t.pending, choices = t.choices }
			end
			talents.trees[i] = { name = tree.name, spent = tree.spent, talents = list }
		end
		return talents
	end

	-- Any spell by ID, like items (Inventory.lua): the server publishes the IDs (payload key spellLookup = { id, at,
	-- spells }), then asks; the reply names the lookup it answers. Spells the client hasn't loaded are requested and
	-- reported as loading, and the server asks again a moment later.
	local lookup -- { id, spells, at (GetTime) }
	local unknown = {} -- spellID -> true: the client couldn't load it

	local function Cached(spellID)
		return not (C_Spell and C_Spell.IsSpellDataCached) or C_Spell.IsSpellDataCached(spellID)
	end

	local function RequestLoad(spellID)
		if C_Spell and C_Spell.RequestLoadSpellData then pcall(C_Spell.RequestLoadSpellData, spellID) end
	end

	-- A talent's tooltip lines at a rank, as the talent window adds them (GetTraitEntry).
	local function RankLines(entryID, rank)
		local ok, data = pcall(C_TooltipInfo.GetTraitEntry, entryID, rank)
		return ok and data and ns.Inventory.Lines(data) or nil
	end

	-- { id, status, name, rank, passive, known, castTime (ms), minRange, maxRange, costs, cd, description, lines,
	-- talent = { rank, max, now, next, top } }; status is left out when the spell's there, else "loading" or "unknown".
	local function Detail(spellID, talents)
		local spell = { id = spellID }
		if unknown[spellID] or (C_Spell.DoesSpellExist and not C_Spell.DoesSpellExist(spellID)) then
			spell.status = "unknown"
			return spell
		end
		if not Cached(spellID) then
			RequestLoad(spellID)
			spell.status = "loading"
			return spell
		end
		local info = C_Spell.GetSpellInfo(spellID)
		if not info then
			spell.status = "unknown"
			return spell
		end
		spell.name, spell.castTime, spell.minRange, spell.maxRange = info.name, info.castTime, info.minRange, info.maxRange
		spell.rank = Subtext(spellID)
		spell.passive = C_Spell.IsSpellPassive and C_Spell.IsSpellPassive(spellID) or nil
		spell.known = C_SpellBook and C_SpellBook.IsSpellKnown and C_SpellBook.IsSpellKnown(spellID) or nil
		local description = C_Spell.GetSpellDescription and C_Spell.GetSpellDescription(spellID)
		if description ~= "" then spell.description = description end
		for _, cost in ipairs(C_Spell.GetSpellPowerCost and C_Spell.GetSpellPowerCost(spellID) or {}) do
			spell.costs = spell.costs or {}
			table.insert(spell.costs, { power = cost.name, cost = cost.cost,
				percent = (cost.costPercent or 0) > 0 and cost.costPercent or nil,
				perSec = (cost.costPerSec or 0) > 0 and cost.costPerSec or nil })
		end
		if C_Spell.GetSpellCooldown then spell.cd = CooldownLeft(C_Spell.GetSpellCooldown, spellID) end
		spell.lines = ns.Inventory.Lines(C_TooltipInfo.GetSpellByID(spellID))
		local talent = talents and talents[spellID]
		if talent then
			spell.talent = {
				rank = talent.rank, max = talent.max,
				now = talent.rank > 0 and RankLines(talent.entry, talent.rank) or nil,
				next = talent.rank < talent.max and RankLines(talent.entry, talent.rank + 1) or nil,
				top = talent.max > talent.rank + 1 and RankLines(talent.entry, talent.max) or nil,
			}
		end
		return spell
	end

	local function SpellDetails()
		if not lookup then return { spells = {} } end
		if GetTime() - lookup.at > KEEP then return { lookup = lookup.id, expired = true } end
		if not (C_Spell and C_Spell.GetSpellInfo and C_TooltipInfo and C_TooltipInfo.GetSpellByID) then
			return { lookup = lookup.id, problem = "This game client has no spell data API (C_Spell, C_TooltipInfo)." }
		end
		local ok, read = pcall(ReadTalents)
		local talents = ok and read and read.bySpell or nil
		local spells = {}
		for _, spellID in ipairs(lookup.spells) do
			local done, spell = pcall(Detail, spellID, talents)
			spells[#spells + 1] = done and spell or { id = spellID, status = "error", error = tostring(spell) }
		end
		return { lookup = lookup.id, spells = spells }
	end

	local function ReceiveLookup(value)
		if type(value) ~= "table" or type(value.id) ~= "number" or type(value.spells) ~= "table" then return end
		if lookup and lookup.id == value.id then return end
		local spells = {}
		for _, spellID in ipairs(value.spells) do
			if type(spellID) == "number" and #spells < MAX_LOOKUP then
				spells[#spells + 1] = spellID
				unknown[spellID] = nil
				-- Start loading now, so it's likely there by the time the server asks.
				local ok, cached = pcall(Cached, spellID)
				if ok and not cached then RequestLoad(spellID) end
			end
		end
		lookup = { id = value.id, spells = spells, at = GetTime() }
	end

	local loads = CreateFrame("Frame")
	loads:RegisterEvent("SPELL_DATA_LOAD_RESULT")
	loads:SetScript("OnEvent", function(_, _, spellID, success)
		if type(spellID) == "number" then unknown[spellID] = not success or nil end
	end)

	ns.RegisterSection(ns.Kinds.REPUTATION, "spellbook", Spellbook)
	ns.RegisterSection(ns.Kinds.REPUTATION, "talents", Talents)
	ns.RegisterSection(ns.Kinds.ITEM_DETAILS, "spellDetails", SpellDetails)
	ns.RegisterReceiver("spellLookup", ReceiveLookup)
end
