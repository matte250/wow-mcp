-- WowMCP areas: shaded, labelled circles on the world map for places that are regions rather
-- than points, e.g. where a mob or herb is found (payload key "areas", sent by highlight_areas).
-- Each circle also shows on the other maps around it, at the same size on the ground.

local _, ns = ...

local Areas = {}
ns.Areas = Areas

local PIN_TEMPLATE = "WowMCPAreaPinTemplate"
local FILL_ALPHA = 0.3
-- The outline: a ring of short lines in a lighter tint of the colour, ~1.5 px on screen at any zoom.
local OUTLINE_SEGMENTS, OUTLINE_WIDTH, OUTLINE_ALPHA, OUTLINE_LIGHTEN = 48, 1.5, 0.9, 0.5
local LABEL_MIN_SIZE = 48 -- canvas units; smaller circles (e.g. on a continent map) only label on hover
local PALETTE = {
	{ 1, 0.3, 0.3 }, { 0.3, 0.6, 1 }, { 0.3, 0.9, 0.3 }, { 1, 0.8, 0.2 },
	{ 0.8, 0.4, 1 }, { 1, 0.55, 0.15 }, { 0.3, 0.9, 0.9 }, { 1, 1, 1 },
}

local current     -- the payload being shown
local areas = {}  -- resolved: an ns.Map.Locate location plus n, label, radius (0-1 of the map's width), yards, color
local guided = {} -- set by another feature with Areas.Guide (Plans.lua: the current step's), shown with them
local provider
local mapAfterCombat

local widths = {}

-- A map's width in yards (world units), or nil for maps without one (e.g. the whole world).
local function Width(mapID)
	if widths[mapID] == nil then
		local ok1, c1, left = pcall(C_Map.GetWorldPosFromMapPos, mapID, CreateVector2D(0, 0.5))
		local ok2, c2, right = pcall(C_Map.GetWorldPosFromMapPos, mapID, CreateVector2D(1, 0.5))
		widths[mapID] = false
		if ok1 and ok2 and c1 and c1 == c2 and left and right then
			local x1, y1 = left.x, left.y
			local x2, y2 = right.x, right.y
			widths[mapID] = math.sqrt((x2 - x1) ^ 2 + (y2 - y1) ^ 2)
		end
	end
	return widths[mapID] or nil
end

-- "rrggbb" (as the server sends it) or a palette colour per area.
local function Color(hex, n)
	local r, g, b = tostring(hex or ""):match("^#?(%x%x)(%x%x)(%x%x)$")
	if r then return { tonumber(r, 16) / 255, tonumber(g, 16) / 255, tonumber(b, 16) / 255 } end
	return PALETTE[(n - 1) % #PALETTE + 1]
end

local function ColorCode(c)
	return string.format("|cff%02x%02x%02x", math.floor(c[1] * 255 + 0.5), math.floor(c[2] * 255 + 0.5), math.floor(c[3] * 255 + 0.5))
end

---------------------------------------------------------------------------
-- World map pins
---------------------------------------------------------------------------

-- Mixed into MapCanvasPinMixin once the world map code is loaded (see AttachToWorldMap).
local Pin = {}

-- MapCanvasPinMixin's version calls SetPassThroughButtons each time the map hands out a pin, which
-- the game blocks for addons in combat ("Interface action failed because of an AddOn" when the map
-- opens in a fight). Blizzard's version, only outside combat.
function Pin:CheckMouseButtonPassthrough(...)
	if not InCombatLockdown() then pcall(MapCanvasPinMixin.CheckMouseButtonPassthrough, self, ...) end
end

function Pin:OnLoad()
	self:UseFrameLevelType("PIN_FRAME_LEVEL_MAP_HIGHLIGHT")
	-- Sized in canvas units, so the circle covers the same ground at any zoom.
	self:SetIgnoreGlobalPinScale(true)
	self.Outline = {}
	for i = 1, OUTLINE_SEGMENTS do self.Outline[i] = self:CreateLine(nil, "ARTWORK") end
end

-- The ring around a circle of the given size, centred on the pin.
local function PlaceOutline(pin, size, c)
	local r, step = size / 2, 2 * math.pi / OUTLINE_SEGMENTS
	local light = function(v) return v + (1 - v) * OUTLINE_LIGHTEN end
	for i, line in ipairs(pin.Outline) do
		local a1, a2 = (i - 1) * step, i * step
		line:SetStartPoint("CENTER", pin, r * math.cos(a1), r * math.sin(a1))
		line:SetEndPoint("CENTER", pin, r * math.cos(a2), r * math.sin(a2))
		line:SetColorTexture(light(c[1]), light(c[2]), light(c[3]), OUTLINE_ALPHA)
	end
end

function Pin:OnAcquired(area, x, y, radius, onOwnMap)
	self.area = area
	local c = area.color
	self.Fill:SetVertexColor(c[1], c[2], c[3], FILL_ALPHA)
	-- In canvas units; the canvas only changes size with the map, which refreshes the pins.
	local size = 2 * radius * self:GetMap():GetCanvas():GetWidth()
	self:SetSize(size, size)
	PlaceOutline(self, size, c)
	self.Label:SetText((onOwnMap or size >= LABEL_MIN_SIZE) and ns.ItemText(area.label) or "")
	self:OnCanvasScaleChanged()
	self:SetPosition(x, y)
end

-- The circle zooms with the map; the label and the outline's thickness stay the same on screen.
function Pin:OnCanvasScaleChanged()
	local scale = self:GetMap():GetCanvasScale()
	self.Label:SetScale(1 / scale)
	for _, line in ipairs(self.Outline) do line:SetThickness(OUTLINE_WIDTH / scale) end
end

function Pin:OnMouseEnter()
	local a = self.area
	GameTooltip:SetOwner(self, "ANCHOR_CURSOR")
	GameTooltip:SetText(ColorCode(a.color) .. ns.ItemText(a.label) .. "|r")
	GameTooltip:AddLine(string.format("%s  %.1f, %.1f", ns.Escape(a.zone), a.x * 100, a.y * 100), 0.7, 0.7, 0.7)
	if a.yards then
		GameTooltip:AddLine(string.format("About %d yards across", math.floor(2 * a.yards + 0.5)), 0.7, 0.7, 0.7)
	end
	GameTooltip:Show()
end

function Pin:OnMouseLeave()
	GameTooltip:Hide()
end

local function CreateProvider()
	local p = CreateFromMixins(MapCanvasDataProviderMixin)

	function p:RemoveAllData()
		self:GetMap():RemoveAllPinsByTemplate(PIN_TEMPLATE)
	end

	function p:RefreshAllData()
		self:RemoveAllData()
		local map = self:GetMap()
		local mapID = map:GetMapID()
		if not mapID then return end
		local width = Width(mapID)
		for _, list in ipairs({ areas, guided }) do
			for _, a in ipairs(list) do
				local x, y = ns.Map.Project(a, mapID)
				local own = a.mapID == mapID
				local radius = own and a.radius or (a.yards and width and a.yards / width)
				if x and radius then map:AcquirePin(PIN_TEMPLATE, a, x, y, radius, own) end
			end
		end
	end

	return p
end

local function RefreshPins()
	if provider and provider:GetMap() then provider:RefreshAllData() end
end

local function AttachToWorldMap()
	if provider or not WorldMapFrame then return end
	WowMCPAreaPinMixin = CreateFromMixins(MapCanvasPinMixin, Pin) -- referenced by Areas.xml
	provider = CreateProvider()
	WorldMapFrame:AddDataProvider(provider)
end

---------------------------------------------------------------------------
-- Payload, map, chat
---------------------------------------------------------------------------

local function OpenOn(mapID)
	if InCombatLockdown() then
		mapAfterCombat = mapID
		return
	end
	if WorldMapFrame and WorldMapFrame:IsShown() then
		WorldMapFrame:SetMapID(mapID)
	elseif OpenWorldMap then
		OpenWorldMap(mapID)
	end
end

local function AreaLine(a)
	return string.format("%s%s|r %s %.1f, %.1f", ColorCode(a.color), ns.Escape(a.label), ns.Escape(a.zone), a.x * 100, a.y * 100)
end

-- Resolves area records ({ zone, x, y, radius (0-100), label, color, mapID }) into list and
-- returns the zones that couldn't be found.
local function Load(list, raw)
	wipe(list)
	local missing = {}
	for i, r in ipairs(raw) do
		local a = type(r) == "table" and type(r.x) == "number" and type(r.y) == "number"
			and ns.Map.Locate(r.zone, r.x, r.y, r.mapID)
		if a then
			a.label = tostring(r.label or ("Area " .. i))
			a.radius = math.min(math.max(tonumber(r.radius) or 3, 0.1), 50) / 100
			local width = Width(a.mapID)
			a.yards = width and a.radius * width
			table.insert(list, a)
			a.n = #list
			a.color = Color(r.color, a.n)
		else
			table.insert(missing, ns.Escape(type(r) == "table" and r.zone or "?"))
		end
	end
	return missing
end

-- Areas we've already announced (e.g. restored by a /reload) are shown quietly.
function Areas.Receive(payload)
	local isNew = payload.id ~= WowMCPDB.areasId
	current = payload
	WowMCPDB.areasId = payload.id

	local raw = type(payload.areas) == "table" and payload.areas or {}
	local missing = Load(areas, raw)
	if WowMCPDB.hiddenAreasId == payload.id then wipe(areas) end
	RefreshPins()

	if not isNew then return end
	if #raw == 0 then
		ns.Print("Area highlights cleared.")
		return
	end
	if areas[1] then
		ns.Print(payload.title and ("|cffffffff" .. ns.Escape(payload.title) .. "|r") or "Highlighted on your map:")
		for _, a in ipairs(areas) do ns.Print(AreaLine(a)) end
	end
	if #missing > 0 then
		ns.Print("|cffff6060Couldn't find these zones on the map:|r " .. table.concat(missing, ", "))
	end
	if areas[1] and payload.openMap ~= false then OpenOn(areas[1].mapID) end
end

-- For other features (Plans.lua): areas shown along with the AI's highlights until changed, e.g.
-- while a plan step is current. Records like the payload's; key names the set, so one the player
-- hid with /mcp areas clear stays hidden. Returns the zones not found.
function Areas.Guide(list, key, title)
	local missing = Load(guided, type(list) == "table" and list or {})
	if key ~= nil and key == WowMCPDB.hiddenGuidedAreas then wipe(guided) end
	guided.key, guided.title = key, title
	RefreshPins()
	return missing
end

function Areas.Unguide()
	if #guided == 0 then return end
	wipe(guided)
	RefreshPins()
end

function Areas.Command(rest)
	rest = strtrim(tostring(rest or "")):lower()
	if rest == "clear" or rest == "hide" then
		if current then WowMCPDB.hiddenAreasId = current.id end
		if guided.key ~= nil then WowMCPDB.hiddenGuidedAreas = guided.key end
		wipe(areas)
		wipe(guided)
		RefreshPins()
		ns.Print("Area highlights hidden until your AI sends new ones.")
	elseif #areas == 0 and #guided == 0 then
		ns.Print("No highlighted areas.")
	else
		if #areas > 0 and current and current.title then ns.Print("|cffffffff" .. ns.Escape(current.title) .. "|r") end
		for _, a in ipairs(areas) do ns.Print(AreaLine(a)) end
		if #guided > 0 and guided.title then ns.Print("|cffffffff" .. ns.Escape(guided.title) .. "|r") end
		for _, a in ipairs(guided) do ns.Print(AreaLine(a)) end
		if rest == "show" then OpenOn((areas[1] or guided[1]).mapID) end
	end
end

function Areas.OnCombatEnded()
	if mapAfterCombat then
		local mapID = mapAfterCombat
		mapAfterCombat = nil
		OpenOn(mapID)
	end
end

function Areas.Init()
	if WorldMapFrame then
		AttachToWorldMap()
	elseif EventUtil and EventUtil.ContinueOnAddOnLoaded then
		EventUtil.ContinueOnAddOnLoaded("Blizzard_WorldMap", AttachToWorldMap)
	end
end

ns.RegisterReceiver("areas", function(payload)
	if type(payload) == "table" then Areas.Receive(payload) end
end)
ns.OnLogin(Areas.Init)
ns.OnItemsLoaded(RefreshPins) -- labels naming items that were still loading
ns.RegisterCommand("areas", "/mcp areas [show, clear] - list the highlighted areas, open the map on them, hide them", Areas.Command)

local combat = CreateFrame("Frame")
combat:RegisterEvent("PLAYER_REGEN_ENABLED")
combat:SetScript("OnEvent", Areas.OnCombatEnded)
