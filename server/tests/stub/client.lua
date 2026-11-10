-- A tiny stand-in for the WoW client, enough to run the WowMCP addon outside the game.
-- File behaviour follows what the Forever client does with addon files (measured live):
--   * PlaySoundFile returns a handle for any existing file, empty or not; only a valid
--     one is still playing a moment later. A valid file stays cached for the rest of the
--     process, an empty one is read again next time.
--   * LoadAddOn: reads the addon's Lua from disk when called, once per UI session.
-- Usage: lua client.lua <AddOns dir> <script.lua>; the script drives the test.
--
-- This file is the client's core (frames, events, timers, loading the addon). Game APIs
-- live in apis/, one file per area; every file there is loaded before the test script.

local ADDONS = assert(arg[1], "AddOns dir")
local SCRIPT = assert(arg[2], "test script")

local function DiskPath(gamePath)
	local rel = gamePath:gsub("^Interface\\AddOns\\", ""):gsub("\\", "/")
	return ADDONS .. "/" .. rel
end

local function ReadFile(path)
	local f = io.open(path, "rb")
	if not f then return nil end
	local data = f:read("a")
	f:close()
	return data
end

client = {
	chat = {}, tickers = {}, timers = {}, validSounds = {}, playing = {}, loaded = {}, opened = {},
	combat = false, sound = true, now = 0, frames = {}, openedThisFrame = {},
	player = { map = 1411, x = 0.5, y = 0.5 },
}

function wipe(t) for k in pairs(t) do t[k] = nil end return t end
function strtrim(s) return (s:gsub("^%s+", ""):gsub("%s+$", "")) end
function InCombatLockdown() return client.combat end
SlashCmdList = {}

function CreateFromMixins(...)
	local t = {}
	for i = 1, select("#", ...) do
		for k, v in pairs((select(i, ...))) do t[k] = v end
	end
	return t
end

-- What game functions return for positions: plain { x, y } tables. Unlike the addon's own
-- CreateVector2D objects (and some functions' results), not every one has :GetXY().
function client.Vector(x, y) return { x = x, y = y } end

function CreateVector2D(x, y)
	return { x = x, y = y, GetXY = function(self) return self.x, self.y end }
end

Enum = { UIMapType = { Cosmic = 0, World = 1, Continent = 2, Zone = 3, Dungeon = 4, Micro = 5, Orphan = 6 } }

DEFAULT_CHAT_FRAME = { AddMessage = function(_, msg) table.insert(client.chat, msg) end }
GameTooltip = setmetatable({}, { __index = function() return function() end end })
-- EventRegistry: callbacks by event and owner, called as callback(owner, ...) when the UI triggers
-- the event. Chat links (|Haddon:...|h): every SetItemRef callback hears client.itemRef(link).
local callbacks = {}
EventRegistry = {}
function EventRegistry:RegisterCallback(event, fn, owner)
	owner = owner or {}
	callbacks[event] = callbacks[event] or {}
	callbacks[event][owner] = fn
	return owner
end
function EventRegistry:TriggerEvent(event, ...)
	for owner, callback in pairs(callbacks[event] or {}) do callback(owner, ...) end
end
function client.itemRef(link)
	EventRegistry:TriggerEvent("SetItemRef", link)
end

-- A post-hook: the hook runs after the original, with the same arguments; the original's results stand.
function hooksecurefunc(t, name, hook)
	if type(t) == "string" then t, name, hook = _G, t, name end
	local original = assert(t[name], "no function to hook")
	t[name] = function(...)
		local results = table.pack(original(...))
		hook(...)
		return table.unpack(results, 1, results.n)
	end
end

-- Builders for Blizzard's own UI, which a /reload makes anew: they run on every client.LoadAddon,
-- before the addon loads. What the client keeps across reloads (e.g. chat window settings) they
-- keep in client.*.
client.onLoad = {}

---------------------------------------------------------------------------
-- Widgets. Regions (textures, font strings, lines) and frames remember what they were told,
-- for tests to inspect. client.fontStrings lists every font string created.
---------------------------------------------------------------------------

local Region = {}
Region.__index = Region
function Region:SetPoint(...) table.insert(self.points, { ... }) end
function Region:GetPoint() return table.unpack(self.points[#self.points] or {}) end
function Region:ClearAllPoints() self.points = {} end
function Region:SetSize(w, h) self.width, self.height = w, h end
function Region:SetWidth(w) self.width = w end
function Region:SetHeight(h) self.height = h end
function Region:SetAlpha(a) self.alpha = a end
function Region:GetAlpha() return self.alpha or 1 end
function Region:SetScale(scale) self.scale = scale end
function Region:Show() self.shown = true end
function Region:Hide() self.shown = false end
function Region:SetShown(shown) self.shown = not not shown end
function Region:IsShown() return self.shown ~= false end
-- Font strings
function Region:SetText(text) self.text = text end
function Region:GetText() return self.text end
function Region:SetTextColor(r, g, b) self.textColor = { r, g, b } end
function Region:SetJustifyH(justify) self.justify = justify end
function Region:SetMaxLines(n) self.maxLines = n end
function Region:SetFontObject(font) self.font = font end
function Region:SetWordWrap(wrap) self.wordWrap = wrap end
-- Text measures: 7 px per character shown, 12 px per inline texture (height 0: as high as the
-- line, and square), 12 px per line; wraps to the width set (if word wrap is on). Colours and
-- links' data take no room.
function Region:GetUnboundedStringWidth()
	local text, textures = (self.text or ""):gsub("||", "\1"):gsub("|T.-|t", "")
	text = text:gsub("|c%x%x%x%x%x%x%x%x", ""):gsub("|cn[^:]*:", ""):gsub("|r", ""):gsub("|H.-|h", ""):gsub("|h", "")
	return #text * 7 + textures * 12
end
function Region:GetStringHeight()
	local width, lines = self:GetUnboundedStringWidth(), 1
	if self.wordWrap and (self.width or 0) > 0 then lines = math.max(1, math.ceil(width / self.width)) end
	return 12 * math.min(lines, self.maxLines or lines)
end
-- Textures
function Region:SetTexture(t) self.texture = t end
function Region:SetAtlas(atlas) self.atlas = atlas end
function Region:SetColorTexture(r, g, b, a) self.color = { r, g, b, a or 1 } end
function Region:SetVertexColor(r, g, b, a) self.color = { r, g, b, a or 1 } end
function Region:SetRotation(radians) self.rotation = radians end
function Region:SetAllPoints(relativeTo) self.allPoints = relativeTo or true end
-- Lines
function Region:SetStartPoint(point, relativeTo, x, y) self.from = { point = point, relativeTo = relativeTo, x = x, y = y } end
function Region:SetEndPoint(point, relativeTo, x, y) self.to = { point = point, relativeTo = relativeTo, x = x, y = y } end
function Region:SetThickness(thickness) self.thickness = thickness end

client.fontStrings = {}
function client.NewRegion(kind, parent, template)
	local region = setmetatable({ kind = kind, parent = parent, template = template, points = {} }, Region)
	if kind == "FontString" then table.insert(client.fontStrings, region) end
	return region
end

local Frame = setmetatable({}, { __index = Region })
Frame.__index = Frame
function Frame:RegisterEvent(e) self.registered[e] = true end
function Frame:UnregisterEvent(e) self.registered[e] = nil end
function Frame:IsEventRegistered(e) return self.registered[e] == true end
function Frame:SetScript(name, fn)
	if name == "OnEvent" then self.onEvent = fn else self[name] = fn end
end
function Frame:CreateTexture(_, _, template) return client.NewRegion("Texture", self, template) end
function Frame:CreateFontString(_, _, template) return client.NewRegion("FontString", self, template) end
function Frame:CreateLine() return client.NewRegion("Line", self) end
function Frame:SetFrameStrata(strata) self.strata = strata end
function Frame:StartMoving() self.moving = true end
function Frame:StopMovingOrSizing() self.moving = false end
for _, name in ipairs({ "SetClampedToScreen", "SetMovable", "SetDontSavePosition", "EnableMouse", "RegisterForDrag",
                        "SetIgnoreParentScale" }) do
	Frame[name] = function() end
end

function CreateFrame(_, name, parent)
	local frame = setmetatable({ registered = {}, points = {}, parent = parent }, Frame)
	table.insert(client.frames, frame)
	if name then _G[name] = frame end
	return frame
end

UIParent = CreateFrame("Frame", "UIParent")

function client.Fire(event, ...)
	for _, frame in ipairs(client.frames) do
		if frame.registered[event] and frame.onEvent then frame.onEvent(frame, event, ...) end
	end
end

local function AddTimer(delay, fn)
	local timer = { at = client.now + delay, fn = fn }
	function timer:Cancel() self.cancelled = true end
	table.insert(client.timers, timer)
	return timer
end

C_Timer = {
	NewTicker = function(_, fn) table.insert(client.tickers, fn) end,
	After = function(delay, fn) AddTimer(delay, fn) end,
	NewTimer = AddTimer,
}

-- One poll interval (0.5 s) passes: due timers fire, then tickers.
function client.Tick()
	client.now = client.now + 0.5
	local pending = {}
	for _, timer in ipairs(client.timers) do
		if timer.at <= client.now then
			if not timer.cancelled then timer.fn() end
		else
			table.insert(pending, timer)
		end
	end
	client.timers = pending
	wipe(client.openedThisFrame)
	for _, fn in ipairs(client.tickers) do fn() end
	for _, frame in ipairs(client.frames) do
		-- Like the client: OnUpdate only runs while the frame is shown.
		if frame.OnUpdate and frame:IsShown() then frame.OnUpdate(frame, 0.5) end
	end
end

-- Keep ticking in real time, so a test can talk to this client from another process.
function client.RunFor(seconds)
	local stop = os.time() + seconds
	while os.time() < stop do
		client.Tick()
		os.execute("sleep 0.02")
	end
end

function debugprofilestop() return os.clock() * 1000 end

-- Unix time follows the virtual clock (client.now); a test can move client.epoch.
client.epoch = os.time()
function time() return math.floor(client.epoch + client.now) end
date = os.date
function GetRealmName() return "Firemaw" end

function client.Wait(ticks)
	for _ = 1, ticks or 3 do client.Tick() end
end

-- Load the addon the way the client does: the TOC's Lua files in order, shared namespace.
function client.LoadAddon()
	wipe(callbacks) -- a /reload starts with a fresh registry
	for _, build in ipairs(client.onLoad) do build() end
	local ns = {}
	for line in io.lines(ADDONS .. "/WowMCP/WowMCP.toc") do
		if line:match("%.lua$") then
			assert(loadfile(ADDONS .. "/WowMCP/" .. line))("WowMCP", ns)
		end
	end
	client.Fire("ADDON_LOADED", "WowMCP")
	client.Fire("PLAYER_LOGIN")
	return ns
end

function client.Publish(json)
	-- Hand control to the test runner, which publishes through the real transport.
	assert(os.execute(os.getenv("WOWMCP_PUBLISH") .. " '" .. json .. "'"))
end

client.addonsDir, client.DiskPath, client.ReadFile = ADDONS, DiskPath, ReadFile

local here = arg[0]:match("^(.*)/[^/]*$") or "."
local apis = io.popen('ls "' .. here .. '/apis"')
for name in apis:lines() do
	if name:match("%.lua$") then dofile(here .. "/apis/" .. name) end
end
apis:close()

dofile(SCRIPT)
print("OK")
