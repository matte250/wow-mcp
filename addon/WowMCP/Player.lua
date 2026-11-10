-- WowMCP player: where the player is, who they are and their professions (request kind STATE).

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
