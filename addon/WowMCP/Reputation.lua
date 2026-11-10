-- WowMCP reputation: faction standings (request kind REPUTATION).

local _, ns = ...

local function HeaderKey(data)
	return data.factionID .. "|" .. data.name
end

-- Localized "Friendly", "Honored", ... as the Reputation window shows it.
local function StandingLabel(reaction)
	local token = "FACTION_STANDING_LABEL" .. reaction
	local ok, label = pcall(function() return GetText(token, UnitSex("player")) end)
	return ok and label or _G[token]
end

-- The faction list as the Reputation window shows it, flattened: each faction carries the
-- header (and sub-header) it's listed under. Headers that track a standing themselves
-- (e.g. Steamwheedle Cartel) are factions too.
local function Read()
	local list, header, subheader = {}, nil, nil
	for i = 1, C_Reputation.GetNumFactions() do
		local data = C_Reputation.GetFactionDataByIndex(i)
		if data then
			local group, sub = header, data.isChild and subheader or nil
			if data.isHeader and data.isChild then
				subheader, sub = data.name, nil
			elseif data.isHeader then
				header, subheader, group = data.name, nil, nil
			end
			if not data.isHeader or data.isHeaderWithRep then
				local base = data.currentReactionThreshold
				list[#list + 1] = {
					id = data.factionID,
					name = data.name,
					header = group,
					subheader = sub,
					reaction = data.reaction,
					standing = StandingLabel(data.reaction),
					value = data.currentStanding - base,
					max = data.nextReactionThreshold - base,
					atWar = data.atWarWith or nil,
				}
			end
		end
	end
	return list
end

-- Collapsed headers hide their factions from the list, so they're opened for the read (sub-headers
-- show up as their parents open) and closed again afterwards, bottom up so indexes stay valid.
local function Factions()
	local opened = {}
	local ok, list = pcall(function()
		local i = 1
		while i <= C_Reputation.GetNumFactions() do
			local data = C_Reputation.GetFactionDataByIndex(i)
			if data and data.isHeader and data.isCollapsed then
				opened[HeaderKey(data)] = true
				C_Reputation.ExpandFactionHeader(i)
			end
			i = i + 1
		end
		return Read()
	end)
	if next(opened) then
		for j = C_Reputation.GetNumFactions(), 1, -1 do
			local data = C_Reputation.GetFactionDataByIndex(j)
			if data and data.isHeader and not data.isCollapsed and opened[HeaderKey(data)] then
				C_Reputation.CollapseFactionHeader(j)
			end
		end
	end
	if not ok then error(list, 0) end
	return list
end

ns.RegisterSection(ns.Kinds.REPUTATION, "factions", Factions)
