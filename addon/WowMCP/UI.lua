-- WowMCP's own front door: a minimap button, a main window with tabs (Plans, Goals, Routes,
-- Messages) and a settings page under Options > AddOns.
--
-- Every picture here is one of the game's own textures, referred to by its path; the addon ships
-- none. The window only shows what the other features already have (their Snapshot functions), so
-- the plan window, goals list, route navigator and chat keep working as before, with or without it.
--
-- WowMCPDB.ui: { minimap = { angle (degrees around the minimap), hidden }, window = { point,
-- relativePoint, x, y, tab, locked }, scale, off = { [payload key] = true } (features turned off in
-- the settings: Core.lua's Apply skips them) }.

local ADDON, ns = ...

local UI = {}
ns.UI = UI

local ICON = "Interface\\Icons\\INV_Misc_Orb_01"
local DOTS = {
	on = "Interface\\Common\\Indicator-Green",
	wait = "Interface\\Common\\Indicator-Yellow",
	off = "Interface\\Common\\Indicator-Red",
}
local GOLD, WHITE, GREEN, GREY, RED = "|cffffd100", "|cffffffff", "|cff1eff00", "|cff9d9d9d", "|cffff3a3a"
local INK = { 0.18, 0.1, 0.02 } -- text on parchment
local REFRESH = 1               -- seconds between redraws while the window shows

local function DB()
	WowMCPDB.ui = type(WowMCPDB.ui) == "table" and WowMCPDB.ui or {}
	local db = WowMCPDB.ui
	db.minimap = type(db.minimap) == "table" and db.minimap or { angle = 225 }
	db.window = type(db.window) == "table" and db.window or {}
	db.off = type(db.off) == "table" and db.off or {}
	db.scale = tonumber(db.scale) or 1
	return db
end

---------------------------------------------------------------------------
-- Connection: whether updates reach the game, and when the AI was last heard from
---------------------------------------------------------------------------

-- Returns state ("on", "wait" or "off"), a short line, and a detail line.
function UI.Connection()
	local run = ns.run
	if not run.live then
		return "off", "Not connected", "Live updates are off" .. (run.liveProblem and (": " .. tostring(run.liveProblem)) or "")
	end
	local chat = ns.AIChat
	local heard = chat and chat.LastHeard and chat.LastHeard()
	local ago
	if heard and heard > -math.huge then
		local s = math.max(0, math.floor(GetTime() - heard))
		ago = s < 60 and (s .. " s ago") or s < 3600 and (math.floor(s / 60) .. " min ago") or (math.floor(s / 3600) .. " h ago")
	end
	if chat and chat.Waiting and chat.Waiting() then
		return "wait", "Waiting for an answer", ago and ("Last news from your AI " .. ago) or "Your AI is working on it"
	end
	return "on", "Connected", ago and ("Your AI replied " .. ago) or "Live updates on"
end

---------------------------------------------------------------------------
-- Minimap button
---------------------------------------------------------------------------

local button

local function PlaceButton()
	local angle = math.rad(DB().minimap.angle or 225)
	local radius = (Minimap:GetWidth() / 2) + 10
	button:ClearAllPoints()
	button:SetPoint("CENTER", Minimap, "CENTER", math.cos(angle) * radius, math.sin(angle) * radius)
end

local function DragUpdate()
	local mx, my = Minimap:GetCenter()
	local scale = Minimap:GetEffectiveScale()
	local cx, cy = GetCursorPosition()
	DB().minimap.angle = math.deg(math.atan2(cy / scale - my, cx / scale - mx)) % 360
	PlaceButton()
end

local function ButtonTooltip(self)
	local state, line, detail = UI.Connection()
	GameTooltip:SetOwner(self, "ANCHOR_LEFT")
	GameTooltip:AddLine("WowMCP", 1, 1, 1)
	local r, g, b = 0.12, 1, 0
	if state == "wait" then r, g, b = 1, 0.82, 0 elseif state == "off" then r, g, b = 1, 0.23, 0.23 end
	GameTooltip:AddLine(line, r, g, b)
	GameTooltip:AddLine(detail, 0.62, 0.62, 0.62)
	GameTooltip:AddLine(" ")
	GameTooltip:AddLine(GOLD .. "Left-click|r open the window", 1, 1, 1)
	GameTooltip:AddLine(GOLD .. "Right-click|r settings", 1, 1, 1)
	GameTooltip:AddLine(GOLD .. "Drag|r move the button", 1, 1, 1)
	GameTooltip:Show()
end

local function UpdateButton()
	if not button then return end
	local state = UI.Connection()
	button.Dot:SetTexture(DOTS[state])
	button.Icon:SetDesaturated(state == "off")
	button.Icon:SetVertexColor(state == "off" and 0.7 or 1, state == "off" and 0.7 or 1, state == "off" and 0.7 or 1)
	if GameTooltip:GetOwner() == button and GameTooltip:IsShown() then ButtonTooltip(button) end
end

local function MakeButton()
	button = CreateFrame("Button", "WowMCPMinimapButton", Minimap)
	button:SetSize(31, 31)
	button:SetFrameStrata("MEDIUM")
	button:SetFrameLevel(Minimap:GetFrameLevel() + 8)
	button:RegisterForClicks("LeftButtonUp", "RightButtonUp")
	button:RegisterForDrag("LeftButton")
	button:SetHighlightTexture("Interface\\Minimap\\UI-Minimap-ZoomButton-Highlight")

	local bg = button:CreateTexture(nil, "BACKGROUND")
	bg:SetTexture("Interface\\Minimap\\UI-Minimap-Background")
	bg:SetSize(20, 20)
	bg:SetPoint("TOPLEFT", 7, -5)

	button.Icon = button:CreateTexture(nil, "ARTWORK")
	button.Icon:SetTexture(ICON)
	button.Icon:SetSize(17, 17)
	button.Icon:SetPoint("TOPLEFT", 7, -6)
	button.Icon:SetTexCoord(0.05, 0.95, 0.05, 0.95)

	local ring = button:CreateTexture(nil, "OVERLAY")
	ring:SetTexture("Interface\\Minimap\\MiniMap-TrackingBorder")
	ring:SetSize(53, 53)
	ring:SetPoint("TOPLEFT")

	button.Dot = button:CreateTexture(nil, "OVERLAY", nil, 2)
	button.Dot:SetSize(12, 12)
	button.Dot:SetPoint("TOPLEFT", 20, -19)

	button:SetScript("OnClick", function(_, which)
		if which == "RightButton" then UI.OpenSettings() else UI.Toggle() end
	end)
	button:SetScript("OnDragStart", function(self)
		self:LockHighlight()
		self:SetScript("OnUpdate", DragUpdate)
	end)
	button:SetScript("OnDragStop", function(self)
		self:UnlockHighlight()
		self:SetScript("OnUpdate", nil)
	end)
	button:SetScript("OnEnter", ButtonTooltip)
	button:SetScript("OnLeave", function() GameTooltip:Hide() end)

	PlaceButton()
	button:SetShown(not DB().minimap.hidden)
	UpdateButton()
end

function UI.ShowButton(shown)
	DB().minimap.hidden = not shown
	if button then button:SetShown(shown) end
end

---------------------------------------------------------------------------
-- Main window: the quest log's frame, tabs along its bottom like the character window's
---------------------------------------------------------------------------

local TABS = { "Plans", "Goals", "Routes", "Messages" }
local window
local rows = {}    -- reused lines in the parchment area
local scroll = 0   -- the first line shown
local shownRows = 0

local function Ink(fs)
	fs:SetTextColor(INK[1], INK[2], INK[3])
	fs:SetShadowOffset(0, 0)
end

local function Row(i)
	local row = rows[i]
	if row then return row end
	row = CreateFrame("Button", nil, window.Content)
	row:SetSize(window.Content:GetWidth(), 22)
	row.Box = row:CreateTexture(nil, "ARTWORK")
	row.Box:SetTexture("Interface\\Buttons\\UI-CheckBox-Up")
	row.Box:SetSize(22, 22)
	row.Box:SetPoint("LEFT")
	row.Check = row:CreateTexture(nil, "OVERLAY")
	row.Check:SetTexture("Interface\\Buttons\\UI-CheckBox-Check")
	row.Check:SetAllPoints(row.Box)
	row.Icon = row:CreateTexture(nil, "ARTWORK")
	row.Icon:SetSize(16, 16)
	row.Meta = row:CreateFontString(nil, "ARTWORK", "GameFontNormalSmall")
	row.Meta:SetPoint("RIGHT", -2, 0)
	row.Meta:SetJustifyH("RIGHT")
	Ink(row.Meta)
	row.Text = row:CreateFontString(nil, "ARTWORK", "GameFontNormal")
	row.Text:SetJustifyH("LEFT")
	row.Text:SetWordWrap(true)
	row:SetHighlightTexture("Interface\\QuestFrame\\UI-QuestLogTitleHighlight", "ADD")
	row:GetHighlightTexture():SetAlpha(0.35)
	rows[i] = row
	return row
end

-- kind: "head" (a heading), "check" (a box, ticked when opts.done), "icon" (opts.icon), "text".
local lines = {}
local function Add(kind, text, opts)
	lines[#lines + 1] = { kind = kind, text = text, opts = opts or {} }
end

local function Fill(tab)
	wipe(lines)
	if tab == "Plans" then
		local plan = ns.Plans and ns.Plans.Snapshot and ns.Plans.Snapshot()
		if not plan then
			Add("head", "No plan")
			Add("text", "Ask your AI for one, for example \"plan my next hour\".")
			return
		end
		Add("head", plan.title)
		for _, step in ipairs(plan.steps) do
			Add("check", (step.current and "|cff1e8a00>|r " or "") .. step.n .. ". " .. step.text, {
				done = step.done, faded = step.done,
				click = function() ns.Plans.GoTo(step.n, true) end,
			})
		end
	elseif tab == "Goals" then
		local goals = ns.Goals and ns.Goals.Snapshot and ns.Goals.Snapshot() or {}
		if #goals == 0 then
			Add("head", "No goals")
			Add("text", "Ask your AI for some, for example \"help me save for my mount\".")
			return
		end
		Add("head", "Active goals")
		for _, goal in ipairs(goals) do
			Add("check", goal.title, { done = goal.done, faded = goal.done or goal.muted, meta = goal.done and "done" or goal.text })
		end
	elseif tab == "Routes" then
		local route = ns.Routes and ns.Routes.Snapshot and ns.Routes.Snapshot()
		if not route then
			Add("head", "No route")
			Add("text", "Ask your AI for one, for example \"a route for copper ore in Westfall\".")
			return
		end
		Add("head", route.title .. (route.loop and " (loop)" or "") .. (route.hidden and " (hidden)" or ""))
		for _, stop in ipairs(route.stops) do
			Add("icon", (stop.current and "|cff1e8a00>|r " or "") .. stop.label, {
				icon = "Interface\\TargetingFrame\\UI-RaidTargetingIcon_" .. (((stop.n - 1) % 8) + 1),
				meta = stop.yards and (math.floor(stop.yards + 0.5) .. " yd") or stop.zone,
				click = function() ns.Routes.GoTo(stop.n) end,
			})
		end
	else
		local messages = ns.Messages and ns.Messages.Recent and ns.Messages.Recent(20) or {}
		if #messages == 0 then
			Add("head", "No messages yet")
			Add("text", "Answers and notes from your AI show here, and in its chat tab.")
			return
		end
		for _, m in ipairs(messages) do
			Add("text", (m.sent and ("|cff6a5a3a" .. m.sent .. "|r ") or "") .. "|cff6b1d00" .. (m.from or "AI") .. ":|r " .. m.text)
		end
	end
end

local function Draw()
	local content = window.Content
	local width = content:GetWidth()
	local y, used = 0, 0
	scroll = math.max(0, math.min(scroll, #lines - 1))
	for i = 1, #lines do
		local line = lines[i]
		if i <= scroll then
			-- above the view
		else
			used = used + 1
			local row = Row(used)
			row:ClearAllPoints()
			row:SetPoint("TOPLEFT", 0, -y)
			row.Box:SetShown(line.kind == "check")
			row.Check:SetShown(line.kind == "check" and line.opts.done)
			row.Icon:SetShown(line.kind == "icon")
			local left = 0
			if line.kind == "check" then left = 24 end
			if line.kind == "icon" then
				row.Icon:SetTexture(line.opts.icon)
				row.Icon:ClearAllPoints()
				row.Icon:SetPoint("LEFT", 2, 0)
				left = 22
			end
			row.Meta:SetText(line.opts.meta or "")
			local metaWidth = (line.opts.meta and line.opts.meta ~= "") and (row.Meta:GetStringWidth() + 8) or 0
			row.Text:SetFontObject(line.kind == "head" and (_G.QuestTitleFont or GameFontNormalLarge) or GameFontNormal)
			row.Text:ClearAllPoints()
			row.Text:SetPoint("LEFT", left, 0)
			row.Text:SetWidth(width - left - metaWidth)
			row.Text:SetText(line.text)
			Ink(row.Text)
			if line.kind == "head" then row.Text:SetTextColor(0, 0, 0) end
			if line.opts.faded then row.Text:SetTextColor(0.35, 0.29, 0.16) end
			local height = math.max(22, row.Text:GetStringHeight() + 6)
			row:SetHeight(height)
			row:SetScript("OnClick", line.opts.click)
			row:EnableMouse(line.opts.click ~= nil)
			row:Show()
			y = y + height + (line.kind == "head" and 4 or 0)
			if y > content:GetHeight() then break end
		end
	end
	for i = used + 1, #rows do rows[i]:Hide() end
	shownRows = used
end

local function Status()
	local state, line, detail = UI.Connection()
	window.Dot:SetTexture(DOTS[state])
	local color = state == "on" and GREEN or state == "wait" and GOLD or RED
	window.State:SetText(color .. line .. "|r " .. GREY .. "· " .. detail .. "|r")
	local plan = ns.Plans and ns.Plans.Snapshot and ns.Plans.Snapshot()
	local now
	if plan and plan.current then
		for _, step in ipairs(plan.steps) do
			if step.n == plan.current then now = step.text end
		end
	end
	window.Now:SetText(WHITE .. "Now:|r " .. (now or GREY .. "nothing planned|r"))
end

function UI.Refresh()
	if not window or not window:IsShown() then return end
	Status()
	Fill(DB().window.tab or "Goals")
	Draw()
end

local function TabTextures(tab, active)
	local file = active and "Interface\\PaperDollInfoFrame\\UI-Character-ActiveTab"
		or "Interface\\PaperDollInfoFrame\\UI-Character-InactiveTab"
	tab.Left:SetTexture(file)
	tab.Middle:SetTexture(file)
	tab.Right:SetTexture(file)
	tab.Text:SetPoint("CENTER", 0, active and 2 or 0)
	tab.Text:SetFontObject(active and "GameFontHighlightSmall" or "GameFontNormalSmall")
	tab:SetFrameLevel(window:GetFrameLevel() + (active and 3 or 1))
	tab:ClearAllPoints()
	tab:SetPoint("TOPLEFT", window, "TOPLEFT", 12 + (tab.index - 1) * 70, active and -455 or -458)
end

function UI.SelectTab(name)
	DB().window.tab = name
	scroll = 0
	for _, tab in ipairs(window.Tabs) do TabTextures(tab, tab.name == name) end
	UI.Refresh()
end

local function MakeTab(index, name)
	local tab = CreateFrame("Button", nil, window)
	tab.index, tab.name = index, name
	tab:SetSize(84, 32)
	tab.Left = tab:CreateTexture(nil, "BACKGROUND")
	tab.Left:SetSize(20, 32)
	tab.Left:SetPoint("TOPLEFT")
	tab.Left:SetTexCoord(0, 0.15625, 0, 1)
	tab.Right = tab:CreateTexture(nil, "BACKGROUND")
	tab.Right:SetSize(20, 32)
	tab.Right:SetPoint("TOPRIGHT")
	tab.Right:SetTexCoord(0.84375, 1, 0, 1)
	tab.Middle = tab:CreateTexture(nil, "BACKGROUND")
	tab.Middle:SetPoint("TOPLEFT", tab.Left, "TOPRIGHT")
	tab.Middle:SetPoint("BOTTOMRIGHT", tab.Right, "BOTTOMLEFT")
	tab.Middle:SetTexCoord(0.15625, 0.84375, 0, 1)
	tab.Text = tab:CreateFontString(nil, "ARTWORK", "GameFontNormalSmall")
	tab.Text:SetText(name)
	tab:SetHighlightTexture("Interface\\PaperDollInfoFrame\\UI-Character-Tab-Highlight", "ADD")
	tab:SetScript("OnClick", function() UI.SelectTab(name) end)
	return tab
end

local function SavePosition()
	local point, _, relativePoint, x, y = window:GetPoint()
	local saved = DB().window
	saved.point, saved.relativePoint, saved.x, saved.y = point, relativePoint, x, y
end

local function MakeWindow()
	window = CreateFrame("Frame", "WowMCPMainFrame", UIParent)
	window:SetSize(348, 462)
	window:SetFrameStrata("MEDIUM")
	window:SetToplevel(true)
	window:SetMovable(true)
	window:SetClampedToScreen(true)
	window:EnableMouse(true)
	window:RegisterForDrag("LeftButton")
	window:SetScript("OnDragStart", function(self) if not DB().window.locked then self:StartMoving() end end)
	window:SetScript("OnDragStop", function(self) self:StopMovingOrSizing(); SavePosition() end)
	table.insert(UISpecialFrames, "WowMCPMainFrame") -- Escape closes it

	local saved = DB().window
	if saved.point then
		window:SetPoint(saved.point, UIParent, saved.relativePoint or saved.point, saved.x or 0, saved.y or 0)
	else
		window:SetPoint("TOPLEFT", UIParent, "TOPLEFT", 40, -110)
	end
	window:SetScale(DB().scale)

	for _, piece in ipairs({
		{ "TopLeft", 0, 0, 256 }, { "TopRight", 256, 0, 128 }, { "BotLeft", 0, -256, 256 }, { "BotRight", 256, -256, 128 },
	}) do
		local t = window:CreateTexture(nil, "BORDER")
		t:SetTexture("Interface\\QuestFrame\\UI-QuestLog-" .. piece[1])
		t:SetSize(piece[4], 256)
		t:SetPoint("TOPLEFT", piece[2], piece[3])
	end

	local portrait = window:CreateTexture(nil, "BACKGROUND")
	portrait:SetSize(58, 58)
	portrait:SetPoint("TOPLEFT", 7, -6)
	if SetPortraitToTexture then SetPortraitToTexture(portrait, ICON) else portrait:SetTexture(ICON) end

	local title = window:CreateFontString(nil, "ARTWORK", "GameFontNormal")
	title:SetPoint("TOP", 20, -18)
	title:SetText("WowMCP")

	local close = CreateFrame("Button", nil, window, "UIPanelCloseButton")
	close:SetPoint("TOPRIGHT", window, "TOPRIGHT", 3, -8)

	-- The black band: connection, the plan's current step, and a line to ask the AI.
	window.Dot = window:CreateTexture(nil, "ARTWORK")
	window.Dot:SetSize(14, 14)
	window.Dot:SetPoint("TOPLEFT", 22, -80)
	window.State = window:CreateFontString(nil, "ARTWORK", "GameFontNormalSmall")
	window.State:SetPoint("LEFT", window.Dot, "RIGHT", 4, 0)
	window.State:SetWidth(270)
	window.State:SetJustifyH("LEFT")
	window.State:SetWordWrap(false)
	window.Now = window:CreateFontString(nil, "ARTWORK", "GameFontNormalSmall")
	window.Now:SetPoint("TOPLEFT", 22, -100)
	window.Now:SetWidth(290)
	window.Now:SetJustifyH("LEFT")
	window.Now:SetWordWrap(false)

	local ask = CreateFrame("EditBox", "WowMCPAskBox", window, "InputBoxTemplate")
	ask:SetSize(284, 20)
	ask:SetPoint("TOPLEFT", 28, -120)
	ask:SetAutoFocus(false)
	ask:SetMaxLetters(255)
	local hint = ask:CreateFontString(nil, "ARTWORK", "GameFontDisableSmall")
	hint:SetPoint("LEFT", 2, 0)
	hint:SetText("Ask your AI…")
	ask:SetScript("OnTextChanged", function(self) hint:SetShown(self:GetText() == "") end)
	ask:SetScript("OnEscapePressed", function(self) self:ClearFocus() end)
	ask:SetScript("OnEnterPressed", function(self)
		local text = strtrim(self:GetText() or "")
		self:SetText("")
		self:ClearFocus()
		if text ~= "" and SlashCmdList.WOWMCPAI then SlashCmdList.WOWMCPAI(text) end
	end)
	window.Ask = ask

	-- The parchment: the open tab's lines. The wheel and the arrows scroll.
	local content = CreateFrame("Frame", nil, window)
	content:SetPoint("TOPLEFT", 30, -178)
	content:SetSize(278, 246)
	content:SetClipsChildren(true)
	content:EnableMouseWheel(true)
	content:SetScript("OnMouseWheel", function(_, delta) scroll = scroll - delta; UI.Refresh() end)
	window.Content = content
	for i, arrow in ipairs({ { "Up", -176, -1 }, { "Down", -404, 1 } }) do
		local b = CreateFrame("Button", nil, window)
		b:SetSize(22, 22)
		b:SetPoint("TOPLEFT", 319, arrow[2])
		b:SetNormalTexture("Interface\\Buttons\\UI-ScrollBar-Scroll" .. arrow[1] .. "Button-Up")
		b:SetPushedTexture("Interface\\Buttons\\UI-ScrollBar-Scroll" .. arrow[1] .. "Button-Down")
		b:SetHighlightTexture("Interface\\Buttons\\UI-ScrollBar-Scroll" .. arrow[1] .. "Button-Highlight", "ADD")
		b:SetScript("OnClick", function() scroll = scroll + arrow[3]; UI.Refresh() end)
	end

	local settings = CreateFrame("Button", nil, window, "UIPanelButtonTemplate")
	settings:SetSize(96, 22)
	settings:SetPoint("TOPLEFT", 22, -434)
	settings:SetText("Settings")
	settings:SetScript("OnClick", function() UI.OpenSettings() end)
	local closeButton = CreateFrame("Button", nil, window, "UIPanelButtonTemplate")
	closeButton:SetSize(80, 22)
	closeButton:SetPoint("TOPLEFT", 230, -434)
	closeButton:SetText(CLOSE or "Close")
	closeButton:SetScript("OnClick", function() window:Hide() end)

	window.Tabs = {}
	for i, name in ipairs(TABS) do window.Tabs[i] = MakeTab(i, name) end

	local since = 0
	window:SetScript("OnUpdate", function(_, elapsed)
		since = since + elapsed
		if since >= REFRESH then
			since = 0
			UI.Refresh()
		end
	end)
	window:SetScript("OnShow", function() UI.Refresh() end)
	window:Hide()
end

function UI.Toggle()
	if not window then MakeWindow() end
	if window:IsShown() then
		window:Hide()
	else
		window:Show()
		UI.SelectTab(DB().window.tab or "Goals")
	end
end

function UI.SetScale(scale)
	DB().scale = scale
	if window then window:SetScale(scale) end
end

---------------------------------------------------------------------------
-- Settings: Options > AddOns > WowMCP
---------------------------------------------------------------------------

-- The features that can be turned off, by their payload key, and what turning one off hides now.
local FEATURES = {
	{ key = "map", label = "Map markers", hide = function() ns.Map.Clear() end },
	{ key = "routes", label = "Routes", hide = function() ns.Routes.Stop() end },
	{ key = "areas", label = "Area circles", hide = function() ns.Areas.Command("clear") end },
	{ key = "glow", label = "Glow in windows", hide = function() ns.Glow.Command(nil, "off") end },
	{ key = "plan", label = "Plans", hide = function() ns.Plans.Hide() end },
	{ key = "goals", label = "Goals", hide = function() ns.Goals.Hide() end },
	{ key = "reminders", label = "Reminders" },
}

local panel, category

local function Check(parent, label, x, y, get, set)
	local cb = CreateFrame("CheckButton", nil, parent, "UICheckButtonTemplate")
	cb:SetSize(26, 26)
	cb:SetPoint("TOPLEFT", x, y)
	local text = cb:CreateFontString(nil, "ARTWORK", "GameFontHighlight")
	text:SetPoint("LEFT", cb, "RIGHT", 2, 1)
	text:SetText(label)
	cb.Label = text
	cb:SetScript("OnShow", function(self) self:SetChecked(get()) end)
	cb:SetScript("OnClick", function(self) set(self:GetChecked() and true or false) end)
	cb:SetChecked(get())
	return cb
end

local function Group(parent, label, y)
	local fs = parent:CreateFontString(nil, "ARTWORK", "GameFontNormal")
	fs:SetPoint("TOPLEFT", 16, y)
	fs:SetText(label)
	local line = parent:CreateTexture(nil, "ARTWORK")
	line:SetColorTexture(1, 0.82, 0, 0.25)
	line:SetHeight(1)
	line:SetPoint("TOPLEFT", 16, y - 16)
	line:SetPoint("RIGHT", parent, "RIGHT", -16, 0)
end

local function QuestieFound()
	local loaded = C_AddOns and C_AddOns.IsAddOnLoaded or IsAddOnLoaded
	return loaded and loaded("Questie") and true or false
end

local function MakePanel()
	panel = CreateFrame("Frame", "WowMCPSettingsPanel")
	panel.name = "WowMCP"

	local title = panel:CreateFontString(nil, "ARTWORK", "GameFontNormalLarge")
	title:SetPoint("TOPLEFT", 16, -16)
	title:SetText("WowMCP")
	local desc = panel:CreateFontString(nil, "ARTWORK", "GameFontHighlightSmall")
	desc:SetPoint("TOPLEFT", title, "BOTTOMLEFT", 0, -6)
	desc:SetText("Connects the game to your AI agents. Turn off anything you do not use.")

	Group(panel, "Connection", -64)
	local dot = panel:CreateTexture(nil, "ARTWORK")
	dot:SetSize(16, 16)
	dot:SetPoint("TOPLEFT", 20, -88)
	local state = panel:CreateFontString(nil, "ARTWORK", "GameFontHighlightSmall")
	state:SetPoint("LEFT", dot, "RIGHT", 6, 0)
	local function Show()
		local s, line, detail = UI.Connection()
		dot:SetTexture(DOTS[s])
		state:SetText((s == "on" and GREEN or s == "wait" and GOLD or RED) .. line .. "|r " .. GREY .. detail .. "|r")
	end
	local test = CreateFrame("Button", nil, panel, "UIPanelButtonTemplate")
	test:SetSize(130, 22)
	test:SetPoint("TOPLEFT", 18, -108)
	test:SetText("Test connection")
	test:SetScript("OnClick", function()
		Show()
		if SlashCmdList.WOWMCP then SlashCmdList.WOWMCP("status") end
	end)

	Group(panel, "Features", -144)
	for i, f in ipairs(FEATURES) do
		local column, row = (i - 1) % 2, math.floor((i - 1) / 2)
		Check(panel, f.label, 18 + column * 200, -166 - row * 26,
			function() return not DB().off[f.key] end,
			function(on)
				DB().off[f.key] = (not on) or nil
				if on then
					ns.Reapply(f.key)
				elseif f.hide then
					pcall(f.hide)
				end
			end)
	end

	local rowsUsed = math.ceil(#FEATURES / 2)
	local y = -166 - rowsUsed * 26 - 14
	Group(panel, "Optional integrations", y)
	local questie = panel:CreateFontString(nil, "ARTWORK", "GameFontHighlight")
	questie:SetPoint("TOPLEFT", 20, y - 26)
	local note = panel:CreateFontString(nil, "ARTWORK", "GameFontDisableSmall")
	note:SetPoint("TOPLEFT", questie, "BOTTOMLEFT", 0, -4)
	note:SetWidth(380)
	note:SetJustifyH("LEFT")
	note:SetText("Your AI looks up NPCs, quests and item sources in Questie's database when it's installed. WowMCP works without it.")

	y = y - 82
	Group(panel, "Appearance", y)
	Check(panel, "Show minimap button", 18, y - 22, function() return not DB().minimap.hidden end, UI.ShowButton)
	Check(panel, "Lock window position", 18, y - 48, function() return DB().window.locked and true or false end,
		function(on) DB().window.locked = on or nil end)

	local made, slider = pcall(CreateFrame, "Slider", "WowMCPScaleSlider", panel, "UISliderTemplate")
	if not made then slider = CreateFrame("Slider", "WowMCPScaleSlider", panel, "OptionsSliderTemplate") end
	slider:SetOrientation("HORIZONTAL")
	slider:SetSize(180, 17)
	slider:SetPoint("TOPLEFT", 240, y - 46)
	slider:SetMinMaxValues(0.8, 1.3)
	slider:SetValueStep(0.05)
	slider:SetObeyStepOnDrag(true)
	local label = panel:CreateFontString(nil, "ARTWORK", "GameFontNormalSmall")
	label:SetPoint("BOTTOMLEFT", slider, "TOPLEFT", 0, 4)
	local low = panel:CreateFontString(nil, "ARTWORK", "GameFontHighlightSmall")
	low:SetPoint("TOPLEFT", slider, "BOTTOMLEFT", 0, -2)
	low:SetText("80%")
	local high = panel:CreateFontString(nil, "ARTWORK", "GameFontHighlightSmall")
	high:SetPoint("TOPRIGHT", slider, "BOTTOMRIGHT", 0, -2)
	high:SetText("130%")
	slider:SetScript("OnValueChanged", function(_, value)
		value = math.floor(value * 20 + 0.5) / 20
		label:SetText("Window scale: " .. math.floor(value * 100 + 0.5) .. "%")
		UI.SetScale(value)
	end)

	panel:SetScript("OnShow", function()
		Show()
		questie:SetText("Questie: " .. (QuestieFound() and (GREEN .. "found|r") or (GREY .. "not installed|r")))
		slider:SetValue(DB().scale)
		label:SetText("Window scale: " .. math.floor(DB().scale * 100 + 0.5) .. "%")
	end)

	if Settings and Settings.RegisterCanvasLayoutCategory then
		category = Settings.RegisterCanvasLayoutCategory(panel, "WowMCP")
		Settings.RegisterAddOnCategory(category)
	elseif InterfaceOptions_AddCategory then
		InterfaceOptions_AddCategory(panel)
	end
end

function UI.OpenSettings()
	if category and Settings and Settings.OpenToCategory then
		Settings.OpenToCategory(category:GetID())
	elseif InterfaceOptionsFrame_OpenToCategory then
		InterfaceOptionsFrame_OpenToCategory(panel)
		InterfaceOptionsFrame_OpenToCategory(panel) -- the first call only opens the frame
	end
end

---------------------------------------------------------------------------
-- Start
---------------------------------------------------------------------------

ns.OnLogin(function()
	DB()
	if not (Minimap and GameTooltip and UIParent) then return end -- not the game's UI (the tests' stub client)
	local ok, err = pcall(MakeButton)
	if not ok then ns.Print("Couldn't make the minimap button: " .. tostring(err)) end
	ok, err = pcall(MakePanel)
	if not ok then ns.Print("Couldn't make the settings page: " .. tostring(err)) end
	-- Blizzard's addon menu by the minimap, where there is one.
	if AddonCompartmentFrame and AddonCompartmentFrame.RegisterAddon then
		pcall(AddonCompartmentFrame.RegisterAddon, AddonCompartmentFrame, {
			text = "WowMCP", icon = ICON, notCheckable = true, func = function() UI.Toggle() end,
		})
	end
	C_Timer.NewTicker(2, UpdateButton)
end)

ns.RegisterCommand("window", "/mcp window - open or close the WowMCP window (or click the minimap button)", function() UI.Toggle() end)
ns.RegisterCommand("settings", "/mcp settings - open WowMCP's settings (Options > AddOns > WowMCP)", function() UI.OpenSettings() end)
ns.RegisterCommand("minimap", "/mcp minimap [show, hide] - show or hide the minimap button", function(rest)
	rest = strtrim(tostring(rest or "")):lower()
	if rest == "hide" then
		UI.ShowButton(false)
		ns.Print("Minimap button hidden. /mcp minimap show brings it back.")
	else
		UI.ShowButton(true)
	end
end)
