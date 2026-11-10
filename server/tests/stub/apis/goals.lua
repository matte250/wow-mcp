-- What Goals.lua reads besides the plan's checks: fights and instances, unit tooltips, item names of
-- the leatherworking materials the tests use; and what its window uses beyond client.lua's widgets.

client.instance = nil -- an instance type ("party", "raid") while inside one

-- GameTooltip keeps its lines, so a test can read what hovering a goal shows.
client.tooltip = { lines = {} }
function GameTooltip:SetOwner(owner) client.tooltip = { owner = owner, lines = {} } end
function GameTooltip:SetText(text) client.tooltip.lines = { text } end
function GameTooltip:AddLine(text) table.insert(client.tooltip.lines, text) end
function GameTooltip:Show() client.tooltip.shown = true end
function GameTooltip:Hide() client.tooltip.shown = false end

local Frame = getmetatable(UIParent)
local Region = getmetatable(UIParent:CreateTexture())
function Frame:GetScript(name) return name == "OnEvent" and self.onEvent or self[name] end
function Region:GetHeight() return self.height or 0 end

local MATERIALS = {
	[4232] = { name = "Medium Hide", quality = 1, level = 20, class = 7, sell = 50 },
	[4289] = { name = "Salt", quality = 1, level = 5, class = 7, sell = 12 },
	[2321] = { name = "Fine Thread", quality = 1, level = 15, class = 7, sell = 25 },
	[4340] = { name = "Gray Dye", quality = 1, level = 25, class = 7, sell = 87 },
	[4231] = { name = "Cured Light Hide", quality = 1, level = 15, class = 7, sell = 50 },
}

-- Installed over the other API files (which load after this one) when the addon first loads.
local installed = false
local function Install()
	if installed then return end
	installed = true
	for itemID, item in pairs(MATERIALS) do
		client.items[itemID] = client.items[itemID] or item
		client.itemNames[itemID] = item.name
	end
	client.itemsLoaded[4232] = true -- the hides were looted before; Salt and the rest load when asked for
	local inInstance = IsInInstance
	function IsInInstance()
		if client.instance then return true, client.instance end
		return inInstance()
	end
	function UnitTokenFromGUID() return nil end -- tooltips name their unit in their first line
end

local LoadAddon = client.LoadAddon
function client.LoadAddon()
	Install()
	return LoadAddon()
end

function client.EnterCombat()
	client.combat = true
	client.Fire("PLAYER_REGEN_DISABLED")
end

function client.LeaveCombat()
	client.combat = false
	client.Fire("PLAYER_REGEN_ENABLED")
end

-- No map position inside instances, like the real client.
function client.EnterInstance(zone, kind)
	client.instance = kind or "party"
	client.player.map = nil
	client.zoneText, client.subzoneText = zone, ""
	client.Fire("ZONE_CHANGED_NEW_AREA")
end

function client.LeaveInstance(map, x, y, zone, subzone)
	client.instance = nil
	client.Enter(map, x, y, zone, subzone)
end

-- Hovering a unit: its tooltip as the game builds it (name first), with the post-calls' lines.
function client.HoverUnit(name, npcID)
	local guid = npcID and string.format("Creature-0-5250-0-1-%d-00002B7C1E", npcID) or "Player-5250-0A1B2C3D"
	return client.ShowTooltip({ type = Enum.TooltipDataType.Unit, guid = guid, lines = { { leftText = name } } })
end
