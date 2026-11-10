-- C_Reputation and the standing labels (Reputation.lua).

FACTION_STANDING_LABEL1, FACTION_STANDING_LABEL2 = "Hated", "Hostile"
FACTION_STANDING_LABEL3, FACTION_STANDING_LABEL4 = "Unfriendly", "Neutral"
FACTION_STANDING_LABEL5, FACTION_STANDING_LABEL6 = "Friendly", "Honored"
FACTION_STANDING_LABEL7, FACTION_STANDING_LABEL8 = "Revered", "Exalted"

function UnitSex() return 2 end
-- A global string, or its _FEMALE variant for female characters where there is one.
function GetText(token, gender) return gender == 3 and _G[token .. "_FEMALE"] or _G[token] end

-- The Reputation window's tree: level 0 headers, level 1 factions and sub-headers, level 2 factions
-- under a sub-header. Collapsed headers hide what's under them from the list, like the client.
client.factions = {
	{ level = 0, factionID = 469, name = "Alliance", isHeader = true },
	{ level = 1, factionID = 54, name = "Gnomeregan Exiles", reaction = 5, low = 3000, high = 9000, standing = 4500 },
	{ level = 1, factionID = 47, name = "Ironforge", reaction = 6, low = 9000, high = 21000, standing = 12250 },
	{ level = 0, factionID = 1169, name = "Other", isHeader = true, collapsed = true },
	{ level = 1, factionID = 169, name = "Steamwheedle Cartel", isHeader = true, isHeaderWithRep = true, collapsed = true, reaction = 4, low = 0, high = 3000, standing = 1200 },
	{ level = 2, factionID = 21, name = "Booty Bay", reaction = 4, low = 0, high = 3000, standing = 1500 },
	{ level = 1, factionID = 87, name = "Bloodsail Buccaneers", reaction = 2, low = -6000, high = -3000, standing = -5500, atWar = true },
	{ level = 0, factionID = 0, name = "Inactive", isHeader = true, collapsed = true },
	{ level = 1, factionID = 589, name = "Wintersaber Trainers", reaction = 4, low = 0, high = 3000, standing = 0 },
}

local function Visible()
	local visible, hiddenBelow = {}, nil
	for _, row in ipairs(client.factions) do
		if not (hiddenBelow and row.level > hiddenBelow) then
			hiddenBelow = nil
			visible[#visible + 1] = row
			if row.isHeader and row.collapsed then hiddenBelow = row.level end
		end
	end
	return visible
end

local function SetCollapsed(index, collapsed)
	local row = Visible()[index]
	if row and row.isHeader then row.collapsed = collapsed end
end

C_Reputation = {
	GetNumFactions = function() return #Visible() end,
	GetFactionDataByIndex = function(index)
		local row = Visible()[index]
		if not row then return nil end
		-- Midnight's secret values can't be used in arithmetic by addon code.
		local standing = client.secretFaction == row.name and setmetatable({}, {}) or row.standing or 0
		return {
			factionID = row.factionID, name = row.name, description = "",
			reaction = row.reaction or 4, currentReactionThreshold = row.low or 0,
			nextReactionThreshold = row.high or 0, currentStanding = standing,
			atWarWith = row.atWar == true, canToggleAtWar = false,
			isChild = row.level == 2 or (row.isHeader == true and row.level == 1),
			isHeader = row.isHeader == true, isHeaderWithRep = row.isHeaderWithRep == true,
			isCollapsed = row.collapsed == true, isWatched = false, hasBonusRepGain = false,
			canSetInactive = not row.isHeader, isAccountWide = false,
		}
	end,
	ExpandFactionHeader = function(index) SetCollapsed(index, false) end,
	CollapseFactionHeader = function(index) SetCollapsed(index, true) end,
}
