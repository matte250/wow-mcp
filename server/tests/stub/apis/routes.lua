-- What Routes.lua and Areas.lua add on top of map.lua: pins built from the addon's own XML
-- templates (with their regions: textures, lines, font strings), the canvas and player facing.

-- client.player.facing: radians counter-clockwise from north (default 0); false where the game
-- hides it, e.g. in instances.
function GetPlayerFacing()
	if client.player.facing == false then return nil end
	return client.player.facing or 0
end
function IsShiftKeyDown() return client.shift == true end
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
function PinFrame:CreateLine()
	local line = client.NewRegion("Line", self)
	self.lines = self.lines or {}
	table.insert(self.lines, line)
	return line
end

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
	for key, kind in pairs(t.regions) do pin[key] = client.NewRegion(kind, pin) end
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

