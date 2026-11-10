-- What Routes.lua and Areas.lua add on top of map.lua: pins built from the addon's own XML
-- templates (with their regions: textures, lines, font strings), the canvas, player facing,
-- and frames with regions for the route navigator.

-- client.player.facing: radians counter-clockwise from north (default 0); false where the game
-- hides it, e.g. in instances.
function GetPlayerFacing()
	if client.player.facing == false then return nil end
	return client.player.facing or 0
end
function IsShiftKeyDown() return client.shift == true end
UIParent = UIParent or {}

---------------------------------------------------------------------------
-- Regions: textures, lines and font strings remember what they show
---------------------------------------------------------------------------

local Region = {}
Region.__index = Region
function Region:SetText(text) self.text = text end
function Region:GetText() return self.text end
function Region:SetTexture(t) self.texture = t end
function Region:SetAtlas(atlas) self.atlas = atlas end
function Region:SetColorTexture(r, g, b, a) self.color = { r, g, b, a or 1 } end
function Region:SetVertexColor(r, g, b, a) self.color = { r, g, b, a or 1 } end
function Region:SetRotation(radians) self.rotation = radians end
function Region:SetStartPoint(point, relativeTo, x, y) self.from = { point = point, relativeTo = relativeTo, x = x, y = y } end
function Region:SetEndPoint(point, relativeTo, x, y) self.to = { point = point, relativeTo = relativeTo, x = x, y = y } end
function Region:SetThickness(thickness) self.thickness = thickness end
function Region:SetScale(scale) self.scale = scale end
function Region:Show() self.shown = true end
function Region:Hide() self.shown = false end
function Region:SetShown(shown) self.shown = not not shown end
function Region:IsShown() return self.shown end
function Region:SetSize(w, h) self.width, self.height = w, h end
for _, name in ipairs({ "SetPoint", "ClearAllPoints", "SetAllPoints", "SetFontObject", "SetJustifyH", "SetWordWrap" }) do
	Region[name] = function() end
end

local function NewRegion(kind)
	return setmetatable({ kind = kind, shown = true }, Region)
end

---------------------------------------------------------------------------
-- Frames: CreateFrame from client.lua plus the widget methods the navigator uses
---------------------------------------------------------------------------

local Frame = {}
function Frame:Show() self.shown = true end
function Frame:Hide() self.shown = false end
function Frame:SetShown(shown) self.shown = not not shown end
function Frame:IsShown() return self.shown end
function Frame:SetSize(w, h) self.width, self.height = w, h end
function Frame:SetPoint(...) self.point = { ... } end
function Frame:GetPoint() return table.unpack(self.point or {}) end
function Frame:ClearAllPoints() self.point = nil end
function Frame:CreateTexture() return NewRegion("Texture") end
function Frame:CreateFontString() return NewRegion("FontString") end
function Frame:StartMoving() self.moving = true end
function Frame:StopMovingOrSizing() self.moving = false end
for _, name in ipairs({ "SetFrameStrata", "SetClampedToScreen", "SetMovable", "SetDontSavePosition", "EnableMouse", "RegisterForDrag" }) do
	Frame[name] = function() end
end

local createFrame = CreateFrame
function CreateFrame(kind, name, ...)
	local frame = createFrame(kind, name, ...)
	frame.shown = true
	for method, fn in pairs(Frame) do
		if frame[method] == nil then frame[method] = fn end
	end
	-- Like the client: OnUpdate only runs while the frame is shown.
	local setScript = frame.SetScript
	function frame:SetScript(script, fn)
		if script == "OnUpdate" and fn then
			return setScript(self, script, function(f, elapsed)
				if f:IsShown() then fn(f, elapsed) end
			end)
		end
		return setScript(self, script, fn)
	end
	if name then _G[name] = frame end
	return frame
end

---------------------------------------------------------------------------
-- The world map canvas: pins from the addon's XML templates
---------------------------------------------------------------------------

-- Templates as the XML files in the TOC define them: mixin and parentKey regions.
local templates

local function Template(name)
	if not templates then
		templates = {}
		local dir = client.addonsDir .. "/WowMCP/"
		for line in io.lines(dir .. "WowMCP.toc") do
			if line:match("%.xml$") then
				local xml = assert(client.ReadFile(dir .. line))
				for template, attributes, body in xml:gmatch('\n\t<Frame name="([%w_]+)"([^>]*)>(.-)\n\t</Frame>') do
					local t = { mixin = attributes:match('mixin="([%w_]+)"'), regions = {} }
					for kind, key in body:gmatch('<(%a+) parentKey="([%w_]+)"') do t.regions[key] = kind end
					templates[template] = t
				end
			end
		end
	end
	return assert(templates[name], "no XML template " .. name)
end

local PinFrame = {}
function PinFrame:SetSize(w, h) self.width, self.height = w, h end
function PinFrame:Show() self.shown = true end
function PinFrame:Hide() self.shown = false end
function PinFrame:IsShown() return self.shown end

MapCanvasPinMixin.GetMap = function(self) return self.owningMap end
MapCanvasPinMixin.SetIgnoreGlobalPinScale = function(self, ignore) self.ignoreGlobalPinScale = ignore end

local canvas = { GetWidth = function() return 1002 end, GetHeight = function() return 668 end }
function WorldMapFrame:GetCanvas() return canvas end
function WorldMapFrame:GetCanvasScale() return client.canvasScale or 1 end

function WorldMapFrame:AcquirePin(template, ...)
	local t = Template(template)
	local mixin = assert(_G[t.mixin], "mixin " .. tostring(t.mixin) .. " isn't defined")
	local pin = CreateFromMixins(mixin)
	for method, fn in pairs(PinFrame) do
		if pin[method] == nil then pin[method] = fn end
	end
	for key, kind in pairs(t.regions) do pin[key] = NewRegion(kind) end
	pin.pinTemplate, pin.owningMap, pin.shown = template, self, true
	pin:OnLoad()
	pin:OnAcquired(...)
	table.insert(self.pins, pin)
	return pin
end

function WorldMapFrame:RemoveAllPinsByTemplate(template)
	for i = #self.pins, 1, -1 do
		if self.pins[i].pinTemplate == template then table.remove(self.pins, i) end
	end
end

-- The pins of one template currently on the map, in the order they were acquired.
function client.Pins(template)
	local found = {}
	for _, pin in ipairs(WorldMapFrame.pins) do
		if pin.pinTemplate == template then table.insert(found, pin) end
	end
	return found
end

-- Zooming: the map tells every pin and data provider.
function client.SetCanvasScale(scale)
	client.canvasScale = scale
	for _, pin in ipairs(WorldMapFrame.pins) do
		if pin.OnCanvasScaleChanged then pin:OnCanvasScaleChanged() end
	end
end

---------------------------------------------------------------------------
-- Chat links: EventRegistry keeps a callback per owner, so several features can listen
---------------------------------------------------------------------------

local itemRefCallbacks = {}
function EventRegistry:RegisterCallback(event, fn, owner)
	if event ~= "SetItemRef" then return end
	itemRefCallbacks[owner] = fn
	client.itemRef = function(link)
		for o, callback in pairs(itemRefCallbacks) do callback(o, link) end
	end
end

-- A /reload starts with a fresh registry.
local loadAddon = client.LoadAddon
function client.LoadAddon()
	wipe(itemRefCallbacks)
	return loadAddon()
end
