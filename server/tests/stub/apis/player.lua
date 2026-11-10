-- Who the player is, the zone text and the Skills window (Player.lua).

function UnitName() return "Thorgrim", nil end
function UnitFactionGroup() return "Alliance", "Alliance" end
function UnitRace() return "Dwarf", "Dwarf", 3 end
function UnitClass() return "Hunter", "HUNTER", 3 end
function UnitLevel() return 12 end
function GetSubZoneText() return "Razor Hill" end
function GetRealZoneText() return "Durotar" end
function IsInInstance() return false end

-- The Skills window as the client lists it: "Secondary Skills" is collapsed, so Cooking
-- only shows up when asked for by ID.
local skillLines = {
	{ skillID = 11, name = "Professions", isHeader = true, parentSkillLineID = 0 },
	{ skillID = 393, name = "Skinning", rank = 87, maxRank = 150, skillLineCategoryID = 11, parentSkillLineID = 0 },
	{ skillID = 165, name = "Leatherworking", rank = 60, maxRank = 75, skillLineCategoryID = 11, parentSkillLineID = 0 },
	{ skillID = 9, name = "Secondary Skills", isHeader = true, isCollapsed = true, parentSkillLineID = 0 },
	{ skillID = 6, name = "Weapon Skills", isHeader = true, parentSkillLineID = 0 },
	{ skillID = 44, name = "Axes", rank = 110, maxRank = 115, skillLineCategoryID = 6, parentSkillLineID = 0 },
}
local cooking = { skillID = 185, name = "Cooking", rank = 40, maxRank = 75, skillLineCategoryID = 9, parentSkillLineID = 0 }
C_SkillInfo = {
	GetNumSkillLines = function() return #skillLines end,
	GetSkillLineInfo = function(i) return skillLines[i] end,
	GetSkillLineInfoByID = function(id)
		if id == 185 then return cooking end
		for _, line in ipairs(skillLines) do
			if line.skillID == id and not line.isHeader then return line end
		end
	end,
}

