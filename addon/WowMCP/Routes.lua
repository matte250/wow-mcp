-- WowMCP routes: numbered stops joined by lines on the world map, and a navigator that points
-- the way to the current stop and moves on as the player arrives (payload key "routes", sent
-- by the show_route tool). Other features guide the player through places of their own the
-- same way with Routes.Guide (Plans.lua: the current step's places).

local _, ns = ...

local Routes = {}
ns.Routes = Routes

local STOP_TEMPLATE = "WowMCPRouteStopPinTemplate"
local LINE_TEMPLATE = "WowMCPRouteLinePinTemplate"
local ARRIVE_YARDS = 12
local LINE_WIDTH = 3          -- on screen, at any zoom
local UPDATE_INTERVAL = 0.1
local ARROW_ATLAS = "Navigation-Tracked-Arrow" -- the game's own waypoint arrow; points up
local ARROW_SIZE = 36
local NO_POSITION = "no position here"
local COLOR = {
	done = { 0.45, 0.45, 0.45 },
	current = { 0.1, 0.7, 0.2 },
	ahead = { 0.2, 0.45, 0.9 },
	line = { 1, 0.82, 0, 0.85 },
}

-- Two sets of stops can drive the navigator and the map, each a "track": the route the AI sent
-- with show_route, and a guide another feature set with Routes.Guide. The route shows while it's
-- there, not hidden and not finished; otherwise the guide does. Each track's progress is saved,
-- so it survives a /reload: WowMCPDB.route { id, index (current stop), done, hidden } and
-- WowMCPDB.routeGuide { key, index, done, hidden }.
local route      -- the route payload being shown
local stops = {} -- the route's resolved stops: an ns.Map.Locate location plus n, label, note, track
local plain = { stops = stops, link = "route", Saved = function() return WowMCPDB.route or {} end }
local guide = { stops = {}, link = "guide", guided = true, Saved = function() return WowMCPDB.routeGuide or {} end }
local shownTrack = false -- the track shown at the last refresh (nil: none; false: not refreshed yet)
local restoring = true   -- while the saved payload is applied at login, the waypoint is left alone
local provider, nav
local armed      -- whether arriving at the current stop counts yet (see Go)
local mapAfterCombat

local atan2 = math.atan2 or math.atan -- Lua 5.1 (the game) or 5.3+ (the tests)

local function Visible(t)
	return #t.stops > 0 and not t.Saved().hidden
end

-- The track the navigator and the map show, if any.
local function Track()
	if Visible(plain) and not (plain.Saved().done and Visible(guide)) then return plain end
	if Visible(guide) then return guide end
end

-- A route needs three stops to go round; a guide (e.g. two farming spots) two.
local function Loops(t)
	return t.loop and #t.stops > (t.guided and 1 or 2)
end

---------------------------------------------------------------------------
-- Map geometry
---------------------------------------------------------------------------

-- How a map lies in its continent's world coordinates: its top left corner and the world vectors
-- along its x (east) and y (south) edges. Works whichever way the world axes point.
local geometry = {}

local function Geometry(mapID)
	if geometry[mapID] == nil then
		local function At(x, y)
			local ok, continent, pos = pcall(C_Map.GetWorldPosFromMapPos, mapID, CreateVector2D(x, y))
			if ok and continent and pos then return continent, pos.x, pos.y end
		end
		local continent, ox, oy = At(0, 0)
		local _, ex, ey = At(1, 0)
		local _, sx, sy = At(0, 1)
		geometry[mapID] = (continent and ex and sx) and {
			continent = continent, ox = ox, oy = oy, ex = ex - ox, ey = ey - oy, sx = sx - ox, sy = sy - oy,
		} or false
	end
	return geometry[mapID] or nil
end

-- Where a stop sits on map mapID (0-1, or beyond the edges), and whether it's on the map.
local function Place(stop, mapID)
	local x, y = ns.Map.Project(stop, mapID)
	if x then return x, y, true end
	local g = Geometry(mapID)
	if not (g and stop.continent == g.continent) then return end
	local wx, wy = stop.worldPos.x, stop.worldPos.y
	local dx, dy = wx - g.ox, wy - g.oy
	return (dx * g.ex + dy * g.ey) / (g.ex ^ 2 + g.ey ^ 2), (dx * g.sx + dy * g.sy) / (g.sx ^ 2 + g.sy ^ 2), false
end

-- Yards from the player to a stop, and the angle to turn the arrow by (counter-clockwise, 0 =
-- straight ahead). Either is nil when unknown; without yards, the reason comes third.
local function Bearing(stop)
	local mapID = C_Map.GetBestMapForUnit("player")
	local pos = mapID and C_Map.GetPlayerMapPosition(mapID, "player")
	local g = mapID and Geometry(mapID)
	if not (pos and g) then return nil, nil, NO_POSITION end
	if g.continent ~= stop.continent then return nil, nil, ns.Escape(stop.zone) .. " (another continent)" end
	local px, py = pos.x, pos.y
	local wx, wy = stop.worldPos.x, stop.worldPos.y
	local dx = wx - (g.ox + px * g.ex + py * g.sx)
	local dy = wy - (g.oy + px * g.ey + py * g.sy)
	local east = (dx * g.ex + dy * g.ey) / math.sqrt(g.ex ^ 2 + g.ey ^ 2)
	local south = (dx * g.sx + dy * g.sy) / math.sqrt(g.sx ^ 2 + g.sy ^ 2)
	local yards = math.sqrt(east ^ 2 + south ^ 2)
	-- Facing: radians counter-clockwise from north; nil where the game hides it.
	local facing = GetPlayerFacing and GetPlayerFacing()
	if not facing then return yards end
	local angle = atan2(-east, -south) - facing
	return yards, (angle + math.pi) % (2 * math.pi) - math.pi
end

---------------------------------------------------------------------------
-- World map pins
---------------------------------------------------------------------------

-- Mixed into MapCanvasPinMixin once the world map code is loaded (see AttachToWorldMap).
local StopPin, LinePin = {}, {}

-- MapCanvasPinMixin's version calls SetPassThroughButtons each time the map hands out a pin, which
-- the game blocks for addons in combat ("Interface action failed because of an AddOn" when the map
-- opens in a fight). Blizzard's version, only outside combat.
function StopPin:CheckMouseButtonPassthrough(...)
	if not InCombatLockdown() then pcall(MapCanvasPinMixin.CheckMouseButtonPassthrough, self, ...) end
end
LinePin.CheckMouseButtonPassthrough = StopPin.CheckMouseButtonPassthrough

local function StopColor(stop)
	local saved = stop.track.Saved()
	if stop.n == saved.index and not saved.done then return COLOR.current end
	if saved.done or (stop.n < (saved.index or 1) and not Loops(stop.track)) then return COLOR.done end
	return COLOR.ahead
end

function StopPin:OnLoad()
	self:UseFrameLevelType("PIN_FRAME_LEVEL_WAYPOINT_LOCATION")
	self:SetScalingLimits(1, 1.0, 1.2)
end

function StopPin:OnAcquired(stop, x, y, onOwnMap)
	self.stop = stop
	self.Number:SetText(tostring(stop.n))
	local c = StopColor(stop)
	self.Fill:SetVertexColor(c[1], c[2], c[3], 1)
	-- Labels only on the stop's own map, so continent maps stay readable.
	self.Label:SetText(onOwnMap and ns.ItemText(stop.label) or "")
	self:SetPosition(x, y)
end

function StopPin:OnMouseEnter()
	local s = self.stop
	GameTooltip:SetOwner(self, "ANCHOR_RIGHT")
	GameTooltip:SetText(string.format("%d. %s", s.n, ns.ItemText(s.label)), 1, 0.82, 0)
	if s.note then GameTooltip:AddLine(ns.ItemText(s.note), 1, 1, 1, true) end
	GameTooltip:AddLine(string.format("%s  %.1f, %.1f", ns.Escape(s.zone), s.x * 100, s.y * 100), 0.7, 0.7, 0.7)
	local yards = ns.Map.Distance(s)
	if yards then GameTooltip:AddLine(string.format("%d yards away", math.floor(yards + 0.5)), 0.7, 0.7, 0.7) end
	GameTooltip:AddLine("Click to make this the current stop", 0.4, 0.8, 1)
	GameTooltip:AddLine(s.track.guided and "Right-click to remove these directions" or "Right-click to remove the route", 0.4, 0.8, 1)
	GameTooltip:Show()
end

function StopPin:OnMouseLeave()
	GameTooltip:Hide()
end

function StopPin:OnClick(...) ns.Map.PinOnClick(self, ...) end -- see Map.lua

-- Right-clicks are for removing the route, not for zooming the map out (as Map.lua's markers).
function StopPin:ShouldMouseButtonBePassthrough()
	return false
end

function StopPin:OnMouseClickAction(button)
	self.clickActionRan = true
	if button == "RightButton" or (button == "LeftButton" and IsShiftKeyDown and IsShiftKeyDown()) then
		Routes.Hide(self.stop.track)
	elseif button == "LeftButton" then
		Routes.GoTo(self.stop.n, self.stop.track)
	end
end

function LinePin:OnLoad()
	self:UseFrameLevelType("PIN_FRAME_LEVEL_DIG_SITE") -- above area highlights, below icons
	self:SetIgnoreGlobalPinScale(true)
end

function LinePin:OnAcquired(x1, y1, x2, y2)
	local canvas = self:GetMap():GetCanvas()
	local w, h = canvas:GetWidth(), canvas:GetHeight()
	-- In canvas units; the canvas only changes size with the map, which refreshes the pins.
	self.Fill:SetStartPoint("TOPLEFT", canvas, x1 * w, -y1 * h)
	self.Fill:SetEndPoint("TOPLEFT", canvas, x2 * w, -y2 * h)
	local c = COLOR.line
	self.Fill:SetVertexColor(c[1], c[2], c[3], c[4])
	self:OnCanvasScaleChanged()
	-- The frame's own position only gives it a frame level; keep it on the map.
	local function Clamp(v) return math.min(math.max(v, 0), 1) end
	self:SetPosition(Clamp((x1 + x2) / 2), Clamp((y1 + y2) / 2))
end

function LinePin:OnCanvasScaleChanged()
	self.Fill:SetThickness(LINE_WIDTH / self:GetMap():GetCanvasScale())
end

local function CreateProvider()
	local p = CreateFromMixins(MapCanvasDataProviderMixin)

	function p:RemoveAllData()
		local map = self:GetMap()
		map:RemoveAllPinsByTemplate(LINE_TEMPLATE)
		map:RemoveAllPinsByTemplate(STOP_TEMPLATE)
	end

	function p:RefreshAllData()
		self:RemoveAllData()
		local map = self:GetMap()
		local mapID, t = map:GetMapID(), Track()
		if not (mapID and t) then return end
		local list = t.stops
		-- A leg is drawn if either end is on this map; the map clips the rest.
		for i = 1, Loops(t) and #list or #list - 1 do
			local x1, y1, on1 = Place(list[i], mapID)
			local x2, y2, on2 = Place(list[i % #list + 1], mapID)
			if x1 and x2 and (on1 or on2) then map:AcquirePin(LINE_TEMPLATE, x1, y1, x2, y2) end
		end
		for _, s in ipairs(list) do
			local x, y = ns.Map.Project(s, mapID)
			if x then map:AcquirePin(STOP_TEMPLATE, s, x, y, s.mapID == mapID) end
		end
	end

	return p
end

local function RefreshPins()
	if provider and provider:GetMap() then provider:RefreshAllData() end
end

local function AttachToWorldMap()
	if provider or not WorldMapFrame then return end
	WowMCPRouteStopPinMixin = CreateFromMixins(MapCanvasPinMixin, StopPin) -- referenced by Routes.xml
	WowMCPRouteLinePinMixin = CreateFromMixins(MapCanvasPinMixin, LinePin)
	provider = CreateProvider()
	WorldMapFrame:AddDataProvider(provider)
end

---------------------------------------------------------------------------
-- Waypoint, map, chat
---------------------------------------------------------------------------

-- Remove the game's waypoint, but only if it's still the one the route set.
local function ClearWaypoint()
	local ours = WowMCPDB.routeWaypoint
	WowMCPDB.routeWaypoint = nil
	if not (ours and C_Map.HasUserWaypoint and C_Map.HasUserWaypoint()) then return end
	local point = C_Map.GetUserWaypoint()
	if not point or point.uiMapID ~= ours.mapID then return end
	local x, y = point.position.x, point.position.y
	if math.abs(x - ours.x) < 0.001 and math.abs(y - ours.y) < 0.001 then
		C_Map.ClearUserWaypoint()
	end
end

-- ns.Map.SetWaypoint records the waypoint as the map markers'; leave that record theirs, so
-- clearing the markers doesn't take the route's waypoint with it.
local function SetWaypoint(stop)
	local markers = WowMCPDB.waypoint
	if ns.Map.SetWaypoint(stop) then
		local w = WowMCPDB.waypoint
		WowMCPDB.routeWaypoint = { mapID = w.mapID, x = w.x, y = w.y }
		WowMCPDB.waypoint = markers
	else
		ClearWaypoint()
	end
end

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

local function StopLine(s)
	local saved = s.track.Saved()
	local current = s.n == saved.index and not saved.done
	local line = string.format("%s%d. |Haddon:WowMCP:%s:%d|h|cffffd100[%s]|r|h %s %.1f, %.1f",
		current and "|cff33ff33>|r " or "", s.n, s.track.link, s.n, ns.Escape(s.label), ns.Escape(s.zone), s.x * 100, s.y * 100)
	local yards = ns.Map.Distance(s)
	if yards then line = line .. string.format(" |cffaaaaaa(%d yd)|r", math.floor(yards + 0.5)) end
	return line
end

local function Title(t)
	return t.title and ns.Escape(t.title) or (t.guided and "Directions" or "Route")
end

local function Heading(t)
	return string.format("|cffffffff%s|r (%d stops%s)", Title(t), #t.stops, Loops(t) and ", loop" or "")
end

---------------------------------------------------------------------------
-- Navigator
---------------------------------------------------------------------------

local Update

local function SavePosition(f)
	local point, _, relativePoint, x, y = f:GetPoint(1)
	WowMCPDB.routeNavigator = { point = point, relativePoint = relativePoint, x = x, y = y }
end

local function NavigatorTooltip(f)
	GameTooltip:SetOwner(f, "ANCHOR_BOTTOM")
	GameTooltip:SetText("Route navigator", 1, 0.82, 0)
	GameTooltip:AddLine("Drag to move. Right-click: next stop, shift-right-click: previous.", 1, 1, 1, true)
	GameTooltip:AddLine("/mcp route lists the stops.", 0.7, 0.7, 0.7)
	GameTooltip:Show()
end

-- The navigator grows with its text up to NAV_MAX_WIDTH; a longer stop name wraps onto a
-- second line rather than being cut off. The measures include item icons, as wide as the line is high.
local NAV_MIN_WIDTH, NAV_MAX_WIDTH, TEXT_LEFT, TEXT_RIGHT = 180, 360, 58, 10

local function Layout(f)
	local texts = { f.Title, f.Stop, f.Distance }
	local widest = 0
	for _, text in ipairs(texts) do widest = math.max(widest, text:GetUnboundedStringWidth()) end
	local width = math.min(NAV_MAX_WIDTH, math.max(NAV_MIN_WIDTH, TEXT_LEFT + widest + TEXT_RIGHT))
	local height = 14
	for _, text in ipairs(texts) do
		text:SetWidth(width - TEXT_LEFT - TEXT_RIGHT)
		height = height + text:GetStringHeight() + 3
	end
	f:SetSize(width, math.max(58, height))
end

local function CreateNavigator()
	local f = CreateFrame("Frame", "WowMCPRouteNavigator", UIParent)
	f:SetSize(NAV_MIN_WIDTH, 58)
	f:SetFrameStrata("MEDIUM")
	f:SetClampedToScreen(true)
	f:SetMovable(true)
	f:SetDontSavePosition(true) -- we keep the position ourselves, in WowMCPDB
	f:EnableMouse(true)
	f:RegisterForDrag("LeftButton")
	local saved = WowMCPDB.routeNavigator
	if saved then
		f:SetPoint(saved.point, UIParent, saved.relativePoint, saved.x, saved.y)
	else
		f:SetPoint("TOP", UIParent, "TOP", 0, -150)
	end

	local bg = f:CreateTexture(nil, "BACKGROUND")
	bg:SetAllPoints()
	bg:SetColorTexture(0, 0, 0, 0.55)

	f.Arrow = f:CreateTexture(nil, "ARTWORK")
	f.Arrow:SetAtlas(ARROW_ATLAS)
	-- Keep the atlas's proportions; rotation turns the whole texture.
	local info = C_Texture and C_Texture.GetAtlasInfo and C_Texture.GetAtlasInfo(ARROW_ATLAS)
	local scale = info and ARROW_SIZE / math.max(info.width, info.height)
	f.Arrow:SetSize(scale and info.width * scale or ARROW_SIZE, scale and info.height * scale or ARROW_SIZE)
	f.Arrow:SetPoint("CENTER", f, "LEFT", 29, 0)

	-- Stacked below each other, so a two-line stop name pushes the distance down.
	local function Text(font, above)
		local text = f:CreateFontString(nil, "ARTWORK", font)
		if above then
			text:SetPoint("TOPLEFT", above, "BOTTOMLEFT", 0, -3)
		else
			text:SetPoint("TOPLEFT", f, "TOPLEFT", TEXT_LEFT, -7)
		end
		text:SetJustifyH("LEFT")
		text:SetWordWrap(false)
		return text
	end
	f.Title = Text("GameFontNormalSmall")
	f.Stop = Text("GameFontHighlight", f.Title)
	f.Stop:SetWordWrap(true)
	f.Stop:SetMaxLines(2)
	f.Distance = Text("GameFontHighlightSmall", f.Stop)

	f:SetScript("OnDragStart", f.StartMoving)
	f:SetScript("OnDragStop", function(self)
		self:StopMovingOrSizing()
		SavePosition(self)
	end)
	f:SetScript("OnMouseUp", function(_, button)
		if button == "RightButton" then Routes.Step(IsShiftKeyDown() and -1 or 1) end
	end)
	f:SetScript("OnEnter", NavigatorTooltip)
	f:SetScript("OnLeave", function() GameTooltip:Hide() end)
	local sinceUpdate = 0
	f:SetScript("OnUpdate", function(_, elapsed)
		sinceUpdate = sinceUpdate + elapsed
		if sinceUpdate >= UPDATE_INTERVAL then
			sinceUpdate = 0
			Update()
		end
	end)
	return f
end

local function RefreshNavigator()
	local t = Track()
	local show = t ~= nil and not t.Saved().done
	if show and not nav then nav = CreateNavigator() end
	if not nav then return end
	nav:SetShown(show)
	if show then Update() end
end

local function Refresh()
	local t = Track()
	if t ~= shownTrack then
		-- The other track takes over (e.g. the guide once the route is finished or hidden), and the
		-- waypoint goes with it.
		if shownTrack ~= false and not restoring then
			armed = false
			local saved = t and t.Saved()
			if saved and not saved.done and t.stops[saved.index] then SetWaypoint(t.stops[saved.index]) else ClearWaypoint() end
		end
		shownTrack = t
	end
	RefreshPins()
	RefreshNavigator()
end

-- Make stop `index` of track t the current one. `arm`: arriving there counts straight away;
-- otherwise the player first has to be seen further than ARRIVE_YARDS from it, so choosing the
-- stop you're standing at (or a loop of stops close together) doesn't skip ahead by itself.
local function Go(t, index, arm)
	local saved = t.Saved()
	saved.index, saved.done, saved.hidden = index, nil, nil
	if Track() == t then -- otherwise the route is shown over this guide, which waits its turn
		shownTrack, armed = t, arm
		SetWaypoint(t.stops[index])
	end
	Refresh()
end

-- The track is done: hide the navigator and remove its waypoint.
local function Finish(t, message)
	t.Saved().done = true
	ClearWaypoint()
	Refresh()
	ns.Print(message)
end

local function Arrive(t)
	local saved = t.Saved()
	local reached = t.stops[saved.index]
	if PlaySound and SOUNDKIT and SOUNDKIT.UI_MAP_WAYPOINT_SUPER_TRACK_ON then
		pcall(PlaySound, SOUNDKIT.UI_MAP_WAYPOINT_SUPER_TRACK_ON)
	end
	local nextIndex = saved.index < #t.stops and saved.index + 1 or (Loops(t) and 1)
	if nextIndex then
		ns.Print(string.format("Reached %s. Next: %s", ns.Escape(reached.label), StopLine(t.stops[nextIndex])))
		-- In a loop the next stop must be walked to; otherwise e.g. two quest givers side by side
		-- both count as soon as you're there.
		Go(t, nextIndex, not Loops(t))
	elseif t.guided then
		Finish(t, string.format("Reached %s.", ns.Escape(reached.label)))
	else
		Finish(t, string.format("Route complete: reached %s, the last stop.", ns.Escape(reached.label)))
	end
	-- Told on the next frame, so whatever it changes doesn't happen inside this update.
	local onArrive = t.onArrive
	if onArrive then C_Timer.After(0, function() pcall(onArrive, reached.n, reached, not nextIndex) end) end
end

function Update()
	local t = Track()
	local saved = t and t.Saved()
	local stop = saved and t.stops[saved.index]
	if not (nav and stop and not saved.done) then return end
	local ok, yards, angle, problem = pcall(Bearing, stop) -- position data may be secret here
	if not ok then yards, angle, problem = nil, nil, NO_POSITION end
	-- The last stop of a route counts even if just chosen: there's nothing to skip ahead to.
	local last = not Loops(t) and saved.index == #t.stops
	if yards and yards <= ARRIVE_YARDS and (armed or last) then return Arrive(t) end
	if yards and yards > ARRIVE_YARDS then armed = true end

	if t.guided and #t.stops == 1 then
		nav.Title:SetText(ns.RenderItems(Title(t)))
	else
		nav.Title:SetText(ns.RenderItems(string.format("%s  %d/%d", Title(t), saved.index, #t.stops)))
	end
	nav.Stop:SetText(ns.ItemText(stop.label))
	nav.Distance:SetText(yards and string.format("%d yd", math.floor(yards + 0.5)) or problem)
	nav.Arrow:SetShown(angle ~= nil)
	if angle then nav.Arrow:SetRotation(angle) end
	Layout(nav)
end

---------------------------------------------------------------------------
-- Payload and commands
---------------------------------------------------------------------------

-- Resolves stop records ({ zone, x, y (0-100), label, note, mapID }) into track t's stops and
-- returns the zones that couldn't be found.
local function Load(t, raw)
	wipe(t.stops)
	local missing = {}
	for i, r in ipairs(raw) do
		local s = type(r) == "table" and type(r.x) == "number" and type(r.y) == "number"
			and ns.Map.Locate(r.zone, r.x, r.y, r.mapID)
		if s then
			s.label = tostring(r.label or ("Stop " .. i))
			s.note = r.note and tostring(r.note) or nil
			s.track = t
			table.insert(t.stops, s)
			s.n = #t.stops
		else
			table.insert(missing, ns.Escape(type(r) == "table" and r.zone or "?"))
		end
	end
	return missing
end

-- A route we've already announced (e.g. restored by a /reload) is picked up where it was, quietly.
function Routes.Receive(payload)
	local saved = WowMCPDB.route
	local isNew = not saved or saved.id ~= payload.id
	if isNew then
		saved = { id = payload.id, index = 1 }
		WowMCPDB.route = saved
	end
	route = payload
	plain.title, plain.loop = payload.title, payload.loop

	local raw = type(payload.stops) == "table" and payload.stops or {}
	local missing = Load(plain, raw)
	if not stops[saved.index] then saved.index = 1 end

	if not isNew then
		if shownTrack == plain then armed = false end
		Refresh()
		return
	end
	if shownTrack ~= guide then ClearWaypoint() end -- the previous route's
	if #raw == 0 then
		Refresh()
		ns.Print("Route cleared.")
		return
	end
	if stops[1] then
		ns.Print(Heading(plain))
		for _, s in ipairs(stops) do ns.Print(StopLine(s)) end
	end
	if #missing > 0 then
		ns.Print("|cffff6060Couldn't find these zones on the map:|r " .. table.concat(missing, ", "))
	end
	if not stops[1] then
		Refresh()
		return
	end
	Go(plain, 1, true)
	if payload.openMap ~= false then OpenOn(stops[1].mapID) end
end

-- For other features (Plans.lua): guide the player through `places` (records like show_route's
-- stops) with the navigator, waypoint and map lines, in place of the route. The route the AI
-- sent still shows over a guide while it's newer and not finished. opts:
--   key: names these places; the same key again (e.g. after a /reload) picks up where it was.
--   title: shown on the navigator. loop: go round the places until the guide changes.
--   onArrive(n, stop, last): called after the player arrives at place n.
--   front: show the guide now (also if hidden or finished), hiding the route if one is shown.
-- No places removes the guide. Returns the zones that couldn't be found, and whether it hid
-- the route.
function Routes.Guide(places, opts)
	opts = opts or {}
	local saved = WowMCPDB.routeGuide
	local isNew = not saved or saved.key ~= opts.key
	if isNew then
		saved = { key = opts.key, index = 1 }
		WowMCPDB.routeGuide = saved
	end
	guide.title, guide.loop, guide.onArrive = opts.title, opts.loop and true or false, opts.onArrive
	local missing = Load(guide, type(places) == "table" and places or {})
	if not guide.stops[saved.index] then saved.index = 1 end

	local replaced = false
	if opts.front and Visible(plain) and not plain.Saved().done then
		plain.Saved().hidden, replaced = true, true
	end
	if not guide.stops[1] then
		WowMCPDB.routeGuide = nil
		Refresh()
	elseif isNew then
		Go(guide, 1, true)
	elseif opts.front then
		Go(guide, saved.done and 1 or saved.index, false)
	else
		Refresh()
	end
	return missing, replaced
end

function Routes.Unguide()
	Routes.Guide({})
end

-- What the route commands act on: the track shown, or else the route, or else the guide.
local function Target()
	return Track() or (#stops > 0 and plain) or (#guide.stops > 0 and guide) or nil
end

local function NoRoute(t)
	if t then return false end
	ns.Print("No route. Ask your AI for one!")
	return true
end

function Routes.List()
	local t = Target()
	if NoRoute(t) then return end
	ns.Print(Heading(t) .. (t.Saved().hidden and " - hidden, /mcp route show brings it back" or ""))
	for _, s in ipairs(t.stops) do ns.Print(StopLine(s)) end
end

function Routes.GoTo(index, t)
	t = t or Target()
	if NoRoute(t) then return end
	if not t.stops[index] then
		ns.Print("No stop " .. tostring(index) .. ". Type /mcp route to list them.")
		return
	end
	-- Choosing one of the guide's stops brings it in front of a route shown over it.
	if t == guide and Track() == plain and not plain.Saved().done then plain.Saved().hidden = true end
	Go(t, index, false)
	ns.Print("Heading to " .. StopLine(t.stops[index]))
end

function Routes.Step(delta)
	local t = Target()
	if NoRoute(t) then return end
	local saved = t.Saved()
	local index = (saved.done and #t.stops + 1 or saved.index) + delta
	if Loops(t) then
		index = (index - 1) % #t.stops + 1
	elseif index > #t.stops then
		-- Past the last stop: that's the end of the route.
		if not saved.done then Finish(t, t.guided and "Directions complete." or "Route complete.") end
		return
	elseif index < 1 then
		ns.Print("Already at the first stop.")
		return
	end
	Routes.GoTo(index, t)
end

-- Brings back what was hidden last: a hidden guide, else the route, else the guide.
function Routes.Show()
	local t = (#guide.stops > 0 and guide.Saved().hidden and guide) or (#stops > 0 and plain)
		or (#guide.stops > 0 and guide) or nil
	if NoRoute(t) then return end
	local saved = t.Saved()
	Go(t, saved.done and 1 or saved.index, false)
	OpenOn(t.stops[saved.index].mapID)
end

function Routes.Stop()
	Routes.Hide(Target())
end

-- Hides a track (the route, or a guide's directions): its stops, lines, navigator and waypoint,
-- until /mcp route show or a new one. Right-click on a stop does this too.
function Routes.Hide(t)
	if NoRoute(t) then return end
	t.Saved().hidden = true
	ClearWaypoint()
	Refresh()
	ns.Print(t.guided and "Directions hidden. /mcp route show brings them back."
		or "Route hidden. /mcp route show brings it back.")
end

local SUBCOMMANDS = {
	[""] = Routes.List, list = Routes.List, show = Routes.Show, stop = Routes.Stop, hide = Routes.Stop,
	next = function() Routes.Step(1) end, prev = function() Routes.Step(-1) end,
}

function Routes.Command(rest)
	rest = strtrim(tostring(rest or "")):lower()
	local run = SUBCOMMANDS[rest]
	if run then
		run()
	elseif tonumber(rest) then
		Routes.GoTo(tonumber(rest))
	else
		ns.Print("/mcp route [show, next, prev, <n>, stop] - list the route's stops, show it and its navigator, change the current stop, hide it")
	end
end

function Routes.OnCombatEnded()
	if mapAfterCombat then
		local mapID = mapAfterCombat
		mapAfterCombat = nil
		OpenOn(mapID)
	end
end

function Routes.Init()
	if WorldMapFrame then
		AttachToWorldMap()
	elseif EventUtil and EventUtil.ContinueOnAddOnLoaded then
		EventUtil.ContinueOnAddOnLoaded("Blizzard_WorldMap", AttachToWorldMap)
	end
	-- Chat links: |Haddon:WowMCP:route:<n>|h, or guide:<n> for a guide's stops.
	if EventRegistry then
		EventRegistry:RegisterCallback("SetItemRef", function(_, link)
			local kind, index = (type(link) == "string" and link or ""):match("^addon:WowMCP:(%a+):(%d+)")
			if kind == "route" or kind == "guide" then Routes.GoTo(tonumber(index), kind == "guide" and guide or plain) end
		end, Routes)
	end
	-- The saved payload is applied right after the login hooks; changes after that are live.
	C_Timer.After(0, function() restoring = false end)
end

ns.RegisterReceiver("routes", function(payload)
	if type(payload) == "table" then Routes.Receive(payload) end
end)
ns.OnLogin(Routes.Init)
ns.OnItemsLoaded(RefreshPins) -- labels naming items that were still loading; the navigator redraws itself
ns.RegisterCommand("route", "/mcp route [show, next, prev, <n>, stop] - the route and its navigator", Routes.Command)

local combat = CreateFrame("Frame")
combat:RegisterEvent("PLAYER_REGEN_ENABLED")
combat:SetScript("OnEvent", Routes.OnCombatEnded)
