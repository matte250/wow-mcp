-- Who can wear what, for the upgrade finder (Auction.lua): the character's class and talent specialization, their
-- armor and weapon skills, dual wielding and C_PlayerInfo.CanUseItem; and gear for the tests to find.
--
-- By default nothing changes for the other tests: player.lua's level 12 hunter, its skill lines, no specialization.

ITEM_MIN_LEVEL = "Requires Level %d"
ITEM_RANDOM_ENCHANT = "<Random enchantment>"

client.class = nil  -- { name, token, id } to play another class than player.lua's
client.spec = nil   -- { index, name, role }: the talent specialization, once the character has one
client.skills = nil -- skill lines replacing player.lua's (the Skills window's armor and weapon skills)
client.dualWield = false

local UnitClassBase = UnitClass
function UnitClass(unit)
	if client.class then return client.class[1], client.class[2], client.class[3] end
	return UnitClassBase(unit)
end

function CanDualWield() return client.dualWield == true end

local base = { num = C_SkillInfo.GetNumSkillLines, info = C_SkillInfo.GetSkillLineInfo, byID = C_SkillInfo.GetSkillLineInfoByID }
function C_SkillInfo.GetNumSkillLines() return client.skills and #client.skills or base.num() end
function C_SkillInfo.GetSkillLineInfo(i)
	if client.skills then return client.skills[i] end
	return base.info(i)
end
-- Nothing for a skill the character doesn't have.
function C_SkillInfo.GetSkillLineInfoByID(id)
	if not client.skills then return base.byID(id) end
	for _, line in ipairs(client.skills) do
		if line.skillID == id and not line.isHeader then return line end
	end
end

C_SpecializationInfo = {
	GetSpecialization = function() return client.spec and client.spec[1] end,
	-- specId, name, description, icon, role, primaryStat, pointsSpent
	GetSpecializationInfo = function(index)
		local spec = client.spec
		if spec and spec[1] == index then return 260 + index, spec[2], "", 0, spec[3], 1, 11 end
		return 0
	end,
}

-- The skill line each armor and weapon type needs, as the client knows it.
local SKILL_OF = {
	[4] = { [1] = 415, [2] = 414, [3] = 413, [4] = 293, [6] = 433 },
	[2] = { [0] = 44, [1] = 172, [2] = 45, [3] = 46, [4] = 54, [5] = 160, [6] = 229, [7] = 43, [8] = 55, [10] = 136,
		[15] = 173, [16] = 176, [18] = 226, [19] = 228 },
}

-- Whether the character has the skill an item's armor or weapon type needs (everything, with player.lua's lines).
function client.Proficient(info)
	local skill = SKILL_OF[info.class] and SKILL_OF[info.class][info.subclass or 0]
	if not (skill and client.skills) then return true end
	return C_SkillInfo.GetSkillLineInfoByID(skill) ~= nil
end

local function ClassAllowed(info)
	if not info.classes then return true end
	local mine = UnitClass("player")
	for _, class in ipairs(info.classes) do
		if class == mine then return true end
	end
	return false
end
client.ClassAllowed = ClassAllowed

-- Assumed to check what the tooltip shows in red: the level too.
C_PlayerInfo = {
	CanUseItem = function(itemID)
		local info = client.items[itemID]
		if not info then return false end
		return (info.required or 0) <= UnitLevel("player") and ClassAllowed(info) and client.Proficient(info)
	end,
}

-- A level 25 Shaman, as the player plays one: cloth, leather and shields (mail comes at 40), one-handed maces,
-- staves and two-handed maces; no talent specialization yet.
local function Line(skillID, name, category) return { skillID = skillID, name = name, rank = 1, maxRank = 1,
	skillLineCategoryID = category, parentSkillLineID = 0 } end
function client.Shaman(level)
	client.class = { "Shaman", "SHAMAN", 7 }
	client.level = level or 25
	client.skills = {
		{ skillID = 11, name = "Professions", isHeader = true, parentSkillLineID = 0 },
		{ skillID = 393, name = "Skinning", rank = 120, maxRank = 150, skillLineCategoryID = 11, parentSkillLineID = 0 },
		{ skillID = 165, name = "Leatherworking", rank = 110, maxRank = 150, skillLineCategoryID = 11, parentSkillLineID = 0 },
		{ skillID = 6, name = "Weapon Skills", isHeader = true, parentSkillLineID = 0 },
		Line(54, "Maces", 6), Line(136, "Staves", 6), Line(160, "Two-Handed Maces", 6),
		-- Collapsed in the Skills window: only GetSkillLineInfoByID finds these.
		{ skillID = 8, name = "Armor Proficiencies", isHeader = true, isCollapsed = true, parentSkillLineID = 0 },
	}
	client.armorSkills = { Line(415, "Cloth", 8), Line(414, "Leather", 8), Line(433, "Shield", 8) }
	local byID = C_SkillInfo.GetSkillLineInfoByID
	function C_SkillInfo.GetSkillLineInfoByID(id)
		for _, line in ipairs(client.armorSkills) do
			if line.skillID == id then return line end
		end
		return byID(id)
	end
end

-- WoW runs Lua 5.1, which can't yield inside pcall (or a C function such as table.sort's comparator); Lua 5.5 here
-- can. After this, a yield from inside one fails here too.
function client.CheckYields()
	local protected = 0
	local pcallBase, yieldBase = pcall, coroutine.yield
	function pcall(...)
		protected = protected + 1
		local results = table.pack(pcallBase(...))
		protected = protected - 1
		return table.unpack(results, 1, results.n)
	end
	function coroutine.yield(...)
		assert(protected == 0, "yield across pcall: WoW's Lua 5.1 can't")
		return yieldBase(...)
	end
end

---------------------------------------------------------------------------
-- Gear for the tests (stats made up)
---------------------------------------------------------------------------

local GEAR_SLOTS = { -- equip location, class, subclass
	{ "INVTYPE_HEAD", 4, 2 }, { "INVTYPE_CHEST", 4, 1 }, { "INVTYPE_HAND", 4, 3 }, { "INVTYPE_FEET", 4, 2 },
	{ "INVTYPE_LEGS", 4, 1 }, { "INVTYPE_WAIST", 4, 4 }, { "INVTYPE_WRIST", 4, 2 }, { "INVTYPE_SHOULDER", 4, 3 },
	{ "INVTYPE_CLOAK", 4, 1 }, { "INVTYPE_FINGER", 4, 0 }, { "INVTYPE_NECK", 4, 0 }, { "INVTYPE_TRINKET", 4, 0 },
	{ "INVTYPE_WEAPON", 2, 4 }, { "INVTYPE_2HWEAPON", 2, 10 }, { "INVTYPE_SHIELD", 4, 6 }, { "INVTYPE_WEAPON", 2, 15 },
	{ "INVTYPE_RANGED", 2, 2 }, { "INVTYPE_ROBE", 4, 2 },
}
local STATS = { "ITEM_MOD_STRENGTH_SHORT", "ITEM_MOD_AGILITY_SHORT", "ITEM_MOD_STAMINA_SHORT", "ITEM_MOD_INTELLECT_SHORT",
	"ITEM_MOD_SPIRIT_SHORT" }

-- n pieces of gear from item ID first: every slot, armor and weapon type in turn, required levels 1-40, two or
-- three stats each, an armor value or weapon damage; every seventh takes a moment to load. Repeatable.
function client.FillGear(first, n)
	for i = 0, n - 1 do
		local slot = GEAR_SLOTS[i % #GEAR_SLOTS + 1]
		local required = 1 + (i * 7) % 40
		local stats = {}
		for k = 0, 1 + i % 2 do stats[STATS[(i + k * 2) % #STATS + 1]] = 1 + (i * 3 + k) % (required // 4 + 2) end
		local item = { name = "Test Gear " .. i, quality = 1 + i % 3, level = required + 5, class = slot[2],
			subclass = slot[3], slot = slot[1], required = required, sell = 100 + i, stats = stats }
		if slot[2] == 2 then
			item.damage = { required, required * 2, 2.0 + (i % 5) / 4 }
		elseif slot[3] ~= 0 then
			item.armor = required * (slot[3] + 1)
		end
		if i % 7 == 3 then item.uncached, item.loadTime = true, 0.5 end
		client.items[first + i] = item
	end
end

-- The full scan as Auction.lua stores it, without visiting the auction house: every item ID given, at price,
-- listed once, by a scan at time at.
function client.Scanned(ids, price, at)
	WowMCPDB = WowMCPDB or {}
	WowMCPDB.auction = WowMCPDB.auction or {}
	local realm = WowMCPDB.auction.Firemaw or { items = {}, scan = {}, scans = {}, tracked = {}, variants = {} }
	WowMCPDB.auction.Firemaw = realm
	table.insert(realm.scans, { at = at or time(), items = #ids, listings = #ids, zone = "Stormwind City", faction = "Alliance" })
	realm.current = #realm.scans
	for _, itemID in ipairs(ids) do
		realm.scan[itemID] = string.format("%d,1,1,%d", type(price) == "function" and price(itemID) or price, realm.current)
	end
	return realm
end
