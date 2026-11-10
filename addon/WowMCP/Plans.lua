-- WowMCP plans: step-by-step guides from the AI (payload key "plan", show_plan tool), shown as a
-- checklist window that ticks each step off by itself as the player does it and moves on.
--
-- A step is done when all its conditions hold: items in the bags (or bank), quests accepted,
-- progressed or turned in, a level, skill rank or recipe, being at a place or in a zone. A step
-- without conditions is ticked off by hand. Item, quest, level, skill and recipe conditions are
-- checked for every step, so a step can finish early; position conditions only count for the
-- current step. Done stays done: crafting uses up the farmed items, the farming step stays ticked.
-- The current step's places drive the route navigator (Routes.Guide) and its areas are
-- highlighted (Areas.Guide). Progress is kept in WowMCPDB.plan, so a /reload picks up where it was.

local _, ns = ...

local Plans = {}
ns.Plans = Plans

local CHECK_INTERVAL = 1 -- seconds; positions have no events, and the window shows distances
local DEFAULT_YARDS = 12
local MAX_ROWS = 8       -- steps shown at once; the mouse wheel scrolls through the rest
local WIDTH, PAD, BOX, GAP, HEADER = 300, 8, 18, 3, 20
local SOUNDS = { step = { "IG_QUEST_LIST_COMPLETE", 878 }, plan = { "READY_CHECK", 8960 } } -- SOUNDKIT names, IDs
local TICK = "|TInterface\\Buttons\\UI-CheckBox-Check:0|t"
local GREEN, GREY, GOLD, WHITE, LIGHT, TODO = "|cff33ff33", "|cff808080", "|cffffd100", "|cffffffff", "|cffd9d9d9", "|cffffcc66"
-- Skill line IDs of the professions and secondary skills, for lines under a collapsed header.
local SKILL_IDS = { 164, 165, 171, 182, 186, 197, 202, 333, 393, 129, 185, 356 }

local plan        -- the payload being shown
local steps = {}  -- parsed: { n, text, places, loop, areas, conditions = { { kind, value, ..., met, text } } }
local wanted = {} -- item IDs the plan counts: their names may arrive later
local session = { turnedIn = {} } -- what events said before the game's own lists catch up
local problem, reported -- the last error while checking, and the last one printed
local window
local hovered     -- the row whose tooltip is showing
local first       -- the first step shown in the window; nil follows the current step
local pending     -- a check is scheduled

-- WowMCPDB.plan: progress of the plan with this id: { id, done = { [n] = "auto" | "click" },
-- undone = { [n] = true } (un-ticked by hand: not ticked again until its conditions have failed),
-- current, complete, hidden, cleared }. A plan with another id starts over.
local function State()
	return WowMCPDB.plan
end

local function Active()
	local state = State()
	return plan ~= nil and #steps > 0 and state ~= nil and not state.cleared
end

-- Errors are kept for get_plan_progress and said once in chat, so they can't go unnoticed.
local function Report(err)
	problem = tostring(err)
	if problem ~= reported then
		reported = problem
		ns.Print("|cffff6060Plan error:|r " .. ns.Escape(problem))
	end
end

local function Play(kit)
	pcall(PlaySound, SOUNDKIT and SOUNDKIT[kit[1]] or kit[2], "Master")
end

local function Normalize(name)
	name = tostring(name or ""):lower():gsub("^the%s+", "")
	return (name:gsub("[^%w]", ""))
end

local function Same(a, b)
	return a ~= nil and b ~= nil and Normalize(a) ~= "" and Normalize(a) == Normalize(b)
end

---------------------------------------------------------------------------
-- What the game knows
---------------------------------------------------------------------------

-- Nil until the client has loaded the item; asking starts loading it.
local function ItemName(c)
	local name = C_Item.GetItemNameByID and C_Item.GetItemNameByID(c.value)
	if not name and C_Item.GetItemInfo then name = C_Item.GetItemInfo(c.value) end
	if not name and C_Item.RequestLoadItemDataByID then pcall(C_Item.RequestLoadItemDataByID, c.value) end
	return name or c.name or ("item " .. c.value)
end

local function ItemCount(itemID, bank)
	if C_Item.GetItemCount then return C_Item.GetItemCount(itemID, bank, false, bank, bank) or 0 end
	return GetItemCount(itemID, bank) or 0
end

local function TurnedIn(questID)
	return session.turnedIn[questID] or C_QuestLog.IsQuestFlaggedCompleted(questID)
end

local function OnQuest(questID)
	return C_QuestLog.IsOnQuest(questID)
end

local function QuestTitle(c)
	local ok, title = pcall(C_QuestLog.GetTitleForQuestID, c.value)
	return ok and title and title ~= "" and title or c.name or ("quest " .. c.value)
end

-- Knowledge.lua's cache: profession name -> { rank, recipes = { { id, name } } }, as the
-- profession windows last showed them.
local function Professions()
	local db = WowMCPDB.knowledge
	local mine = db and db.recipes and db.recipes[UnitName("player") .. "-" .. GetRealmName()]
	return type(mine) == "table" and mine or {}
end

local function SkillRank(name)
	local best
	local function Consider(info)
		if type(info) == "table" and not info.isHeader and Same(info.name, name) then
			best = math.max(best or 0, info.rank or 0)
		end
	end
	for i = 1, C_SkillInfo.GetNumSkillLines() do Consider(C_SkillInfo.GetSkillLineInfo(i)) end
	for _, skillID in ipairs(SKILL_IDS) do Consider(C_SkillInfo.GetSkillLineInfoByID(skillID)) end
	for profession, info in pairs(Professions()) do
		if Same(profession, name) and type(info) == "table" then best = math.max(best or 0, info.rank or 0) end
	end
	return best
end

-- Whether a recipe Knowledge.lua lists is the one asked for: by spell ID if both have one, else by name.
local function IsRecipe(recipe, id, name)
	if type(recipe) ~= "table" then return false end
	if id and recipe.id then return recipe.id == id end
	return Same(recipe.name, name)
end

-- A recipe by spell ID or name: known if Knowledge.lua saw it in a profession window or saw the
-- player learn it (since login, or since a profession window last listed it), or the spellbook or
-- the open profession window says so. Also its name.
local function Recipe(c)
	local id = type(c.value) == "number" and c.value or nil
	local name = not id and c.value or nil
	if id and C_Spell and C_Spell.GetSpellName then
		local ok, spell = pcall(C_Spell.GetSpellName, id)
		name = ok and spell or nil
	end
	for _, profession in pairs(Professions()) do
		for _, recipe in ipairs(type(profession) == "table" and profession.recipes or {}) do
			if IsRecipe(recipe, id, name) then return true, recipe.name end
		end
	end
	for _, recipe in ipairs(ns.Knowledge.Learned()) do
		if IsRecipe(recipe, id, name) then return true, recipe.name or name end
	end
	if id then
		if C_SpellBook and C_SpellBook.IsSpellKnown then
			local ok, known = pcall(C_SpellBook.IsSpellKnown, id)
			if ok and known then return true, name end
		end
		if C_TradeSkillUI and C_TradeSkillUI.GetRecipeInfo then
			local ok, info = pcall(C_TradeSkillUI.GetRecipeInfo, id)
			if ok and type(info) == "table" and info.learned then return true, info.name or name end
		end
	end
	return false, name
end

-- In the zone by name, or on its map or a map inside it (a city in a zone, a dungeon).
local function InZone(c)
	if Same(GetRealZoneText(), c.value) or Same(GetZoneText(), c.value) then return true end
	local mapID = c.mapID and C_Map.GetBestMapForUnit("player")
	for _ = 1, 10 do
		if not mapID or mapID == 0 then return false end
		if mapID == c.mapID then return true end
		local info = C_Map.GetMapInfo(mapID)
		mapID = info and info.parentMapID
	end
	return false
end

---------------------------------------------------------------------------
-- Conditions: each check returns whether it holds and a line of progress (plain text; the
-- window escapes and colours it). current: whether the step is the current one.
---------------------------------------------------------------------------

local KINDS = { "have", "at", "zone", "subzone", "questAccepted", "questTurnedIn", "questComplete",
	"questObjective", "level", "skill", "recipe", "manual" }
local NUMBERED = { have = true, questAccepted = true, questTurnedIn = true, questComplete = true, questObjective = true, level = true }
local POSITION = { at = true, zone = true, subzone = true }

local CHECKS = {}

-- An ID that the game says is another item than the one named is the AI's mistake: the name counts
-- then (the game counts items the player holds by name), and the step says so.
function CHECKS.have(c)
	local name = ItemName(c)
	local wrong = c.name and not Same(name, c.name)
	local have = math.floor(ItemCount(wrong and c.name or c.value, c.bank))
	local text = string.format("%s %d/%d%s", wrong and c.name or name, math.min(have, c.count), c.count, c.bank and " (with bank)" or "")
	if wrong then text = text .. string.format(" (item %d is %s in this game; counted by name)", c.value, name) end
	return have >= c.count, text
end

function CHECKS.at(c, current)
	if not c.loc then return false, "Go to " .. tostring(c.zone) .. " (zone not found)" end
	local where = string.format("%s %.1f, %.1f", c.loc.zone, c.loc.x * 100, c.loc.y * 100)
	local yards = current and ns.Map.Distance(c.loc)
	if yards and yards <= c.yards then return true, "At " .. where end
	return false, "Go to " .. where .. (yards and string.format(" (%d yd)", math.floor(yards + 0.5)) or "")
end

function CHECKS.zone(c, current)
	local here = current and InZone(c)
	return here, (here and "In " or "Go to ") .. c.value
end

function CHECKS.subzone(c, current)
	local here = current and (Same(GetSubZoneText(), c.value) or Same(GetMinimapZoneText(), c.value))
	return here, (here and "In " or "Go to ") .. c.value
end

function CHECKS.questAccepted(c)
	local met = OnQuest(c.value) or TurnedIn(c.value)
	return met, (met and "Accepted: " or "Accept: ") .. QuestTitle(c)
end

function CHECKS.questTurnedIn(c)
	local met = TurnedIn(c.value)
	return met, (met and "Turned in: " or "Turn in: ") .. QuestTitle(c)
end

function CHECKS.questComplete(c)
	if TurnedIn(c.value) then return true, "Done: " .. QuestTitle(c) end
	if not OnQuest(c.value) then return false, "Complete: " .. QuestTitle(c) .. " (not in your quest log)" end
	local lines = { "Complete: " .. QuestTitle(c) }
	for _, objective in ipairs(C_QuestLog.GetQuestObjectives(c.value) or {}) do
		if objective.text and objective.text ~= "" then lines[#lines + 1] = "  " .. objective.text end
	end
	return C_QuestLog.IsComplete(c.value), table.concat(lines, "\n")
end

function CHECKS.questObjective(c)
	if TurnedIn(c.value) then return true, "Done: " .. QuestTitle(c) end
	local onQuest = OnQuest(c.value)
	local objective = onQuest and (C_QuestLog.GetQuestObjectives(c.value) or {})[c.index]
	local text = objective and objective.text ~= "" and objective.text
		or string.format("%s, objective %d%s", QuestTitle(c), c.index, onQuest and "" or " (not in your quest log)")
	return onQuest and ((objective and objective.finished) or C_QuestLog.IsComplete(c.value)), text
end

function CHECKS.level(c)
	local level = math.floor(math.max(UnitLevel("player") or 0, session.level or 0))
	return level >= c.value, string.format("Level %d/%d", math.min(level, c.value), c.value)
end

function CHECKS.skill(c)
	local rank = SkillRank(c.value)
	if not rank then return false, string.format("%s 0/%d (not learned)", c.value, c.rank) end
	rank = math.floor(rank)
	return rank >= c.rank, string.format("%s %d/%d", c.value, math.min(rank, c.rank), c.rank)
end

function CHECKS.recipe(c)
	local known, name = Recipe(c)
	return known, (known and "Known: " or "Learn: ") .. tostring(name or c.name or c.value)
end

function CHECKS.manual()
	return false, "Tick the box when done"
end

function CHECKS.unknown()
	return false, "A condition this addon doesn't know (install the latest wow-mcp)"
end

local function ParseCondition(raw)
	local c = { kind = "unknown" }
	if type(raw) ~= "table" then return c end
	for _, kind in ipairs(KINDS) do
		if raw[kind] ~= nil then
			c.kind, c.value = kind, raw[kind]
			break
		end
	end
	c.name = raw.name and tostring(raw.name) or nil
	local kind = c.kind
	if NUMBERED[kind] then
		c.value = tonumber(c.value)
		if not c.value then c.kind = "unknown" end
		c.count = math.max(1, math.floor(tonumber(raw.count) or 1))
		c.bank = raw.bank == true
		c.index = math.max(1, math.floor(tonumber(raw.index) or 1))
		if kind == "have" and c.value then wanted[c.value] = true end
	elseif kind == "at" then
		local at = type(raw.at) == "table" and raw.at or {}
		c.zone = at.zone
		c.loc = type(at.x) == "number" and type(at.y) == "number" and ns.Map.Locate(at.zone, at.x, at.y, at.mapID) or nil
		c.yards = tonumber(raw.yards) or DEFAULT_YARDS
	elseif kind == "zone" or kind == "subzone" or kind == "skill" then
		c.value = tostring(c.value)
		c.rank = math.max(1, math.floor(tonumber(raw.rank) or 1))
		local info = kind == "zone" and ns.Map.Resolve(c.value)
		c.mapID = info and info.mapID
	elseif kind == "recipe" then
		c.value = tonumber(c.value) or tostring(c.value)
	end
	return c
end

local function Checkable(step)
	for _, c in ipairs(step.conditions) do
		if c.kind ~= "manual" and c.kind ~= "unknown" then return true end
	end
	return false
end

-- Checks a step's conditions, keeping each one's progress; true when it can tick itself off.
local function CheckStep(step, current)
	local all = true
	for _, c in ipairs(step.conditions) do
		local ok, met, text = pcall(CHECKS[c.kind], c, current)
		if not ok then
			Report(met) -- e.g. a secret value; the other conditions and steps go on
			met, text = false, "Couldn't check this here"
		end
		c.met, c.text = met and true or false, tostring(text or "")
		all = all and c.met
	end
	return all and Checkable(step)
end

---------------------------------------------------------------------------
-- Progress
---------------------------------------------------------------------------

local Guide, Layout

-- Escaped, with its item tokens for ns.Print or ns.RenderItems to show.
local function StepText(n)
	return ns.Escape(steps[n].text)
end

-- After the current step is done: the next step not done after it, else the first one not done;
-- nil when all are done.
local function FixCurrent()
	local state = State()
	local current = state.current
	if current and steps[current] and not state.done[current] then return end
	for n = (current or 0) + 1, #steps do
		if not state.done[n] then
			state.current = n
			return
		end
	end
	for n = 1, #steps do
		if not state.done[n] then
			state.current = n
			return
		end
	end
	state.current = nil
end

-- Says what got done and moves the navigator on (unless quiet or hidden: a quiet caller guides
-- the player itself).
local function Announce(finished, before, quiet)
	local state = State()
	local loud = not quiet and not state.hidden
	table.sort(finished)
	if loud then
		for _, n in ipairs(finished) do
			local line = string.format("%s Step %d/%d done: %s", TICK, n, #steps, StepText(n))
			if n == before and state.current then
				line = line .. string.format(" |cffaaaaaaNext: %d. %s|r", state.current, StepText(state.current))
			end
			ns.Print(line)
		end
	end
	if state.current then
		state.complete = nil
		if loud and #finished > 0 then Play(SOUNDS.step) end
	elseif not state.complete then
		state.complete = true
		if not state.hidden then -- also when quiet: a new plan that's already done says so
			ns.Print(GREEN .. "Plan complete:|r " .. ns.Escape(plan.title or "your plan"))
			Play(SOUNDS.plan)
		end
	end
	if state.current ~= before then
		first = nil
		if not quiet then Guide() end
	end
end

-- Ticks off the steps whose conditions hold, moves the current step on and redraws. quiet: no
-- chat or sound (a new plan's steps that were already done).
local function Evaluate(quiet)
	if not Active() then return end
	local state = State()
	local before = state.current
	local finished = {}
	local current
	repeat
		current = state.current
		for _, step in ipairs(steps) do
			if not state.done[step.n] then
				local holds = CheckStep(step, step.n == current)
				if state.undone[step.n] then
					if not holds then state.undone[step.n] = nil end
				elseif holds then
					state.done[step.n] = "auto"
					table.insert(finished, step.n)
				end
			end
		end
		FixCurrent()
		-- A new current step: check its position conditions too.
	until state.current == current or not state.current
	if #finished > 0 or state.current ~= before or (not state.current) ~= (state.complete or false) then
		Announce(finished, before, quiet)
	end
	Layout()
end

function Plans.Check()
	local ok, err = pcall(Evaluate)
	if not ok then Report(err) end
end

local function Soon()
	if pending then return end
	pending = true
	C_Timer.After(0.2, function()
		pending = false
		Plans.Check()
	end)
end

local function OnArrive()
	Plans.Check()
end

-- The navigator and map follow the current step's places, and its areas are highlighted.
-- front: the player (or a new plan) asked for this step, so it goes over a route from show_route.
function Guide(front)
	local state = State()
	local step = Active() and not state.hidden and state.current and steps[state.current]
	if not step then
		ns.Routes.Unguide()
		ns.Areas.Unguide()
		return
	end
	local key = plan.id .. ":" .. step.n
	local _, replaced = ns.Routes.Guide(step.places, {
		key = key, title = string.format("Step %d/%d", step.n, #steps), loop = step.loop, onArrive = OnArrive, front = front,
	})
	if replaced then ns.Print("Your route is hidden while the plan guides you; /mcp route show brings it back.") end
	ns.Areas.Guide(step.areas, key, string.format("Plan step %d", step.n))
end

---------------------------------------------------------------------------
-- The window
---------------------------------------------------------------------------

local function WindowSaved()
	WowMCPDB.planWindow = WowMCPDB.planWindow or {}
	return WowMCPDB.planWindow
end

local function SavePosition(f)
	local point, _, relativePoint, x, y = f:GetPoint(1)
	local saved = WindowSaved()
	saved.point, saved.relativePoint, saved.x, saved.y = point, relativePoint, x, y
end

-- A condition's progress line, escaped; an item's starts with its icon.
local function ConditionText(c)
	return (c.kind == "have" and ns.ItemIcon(c.value) or "") .. ns.Escape(c.text or "")
end

local function ProgressText(step)
	local state = State()
	if #step.conditions == 0 then return GREY .. "Tick the box when done|r" end
	local lines = {}
	for _, c in ipairs(step.conditions) do
		local color = c.met and GREEN or TODO
		local text = ConditionText(c):gsub("\n", "|r\n" .. color) -- each line keeps the colour
		lines[#lines + 1] = color .. text .. "|r"
	end
	if state.undone[step.n] and Checkable(step) then lines[#lines + 1] = GREY .. "Un-ticked by hand: tick the box when done|r" end
	return table.concat(lines, "\n")
end

local function RowTooltip(row)
	row.Hover:Show()
	hovered = row
	local step = steps[row.n]
	if not step then return end
	local done = State().done[step.n]
	GameTooltip:SetOwner(row, "ANCHOR_RIGHT")
	GameTooltip:SetText(ns.RenderItems(string.format("%d. %s", step.n, StepText(step.n))), 1, 0.82, 0, 1, true)
	for i, p in ipairs(step.places) do
		if type(p) == "table" then
			GameTooltip:AddLine(string.format("%s%s - %s %.1f, %.1f", #step.places > 1 and (i .. ". ") or "", ns.ItemText(p.label or "Place"),
				ns.Escape(p.zone), tonumber(p.x) or 0, tonumber(p.y) or 0), 0.7, 0.7, 0.7)
			if p.note then GameTooltip:AddLine(ns.ItemText(p.note), 1, 1, 1, true) end
		end
	end
	for _, c in ipairs(done and {} or step.conditions) do
		GameTooltip:AddLine(ConditionText(c), c.met and 0.2 or 1, c.met and 1 or 0.8, c.met and 0.2 or 0.4, true)
	end
	if done then
		GameTooltip:AddLine("Done. Right-click or click the box to mark it not done.", 0.4, 0.8, 1, true)
	else
		GameTooltip:AddLine("Click: make this the current step. Right-click or click the box: mark it done.", 0.4, 0.8, 1, true)
	end
	GameTooltip:Show()
end

local function CreateRow(f)
	local row = CreateFrame("Frame", nil, f)
	row:EnableMouse(true)
	row:EnableMouseWheel(true)
	row:SetScript("OnMouseWheel", function(_, delta) Plans.Scroll(delta) end)
	row:SetWidth(WIDTH - 2 * PAD)
	row.Current = row:CreateTexture(nil, "BACKGROUND")
	row.Current:SetAllPoints()
	row.Current:SetColorTexture(1, 0.82, 0, 0.14)
	row.Hover = row:CreateTexture(nil, "BACKGROUND")
	row.Hover:SetAllPoints()
	row.Hover:SetColorTexture(1, 1, 1, 0.07)
	row.Hover:Hide()

	row.Box = CreateFrame("Button", nil, row)
	row.Box:SetSize(BOX, BOX)
	row.Box:SetPoint("TOPLEFT", row, "TOPLEFT", -2, 2)
	row.Box:SetNormalTexture("Interface\\Buttons\\UI-CheckBox-Up")
	row.Box:SetHighlightTexture("Interface\\Buttons\\UI-CheckBox-Highlight", "ADD")
	row.Check = row.Box:CreateTexture(nil, "OVERLAY")
	row.Check:SetAllPoints()
	row.Check:SetTexture("Interface\\Buttons\\UI-CheckBox-Check")

	local width = WIDTH - 2 * PAD - BOX
	row.Text = row:CreateFontString(nil, "ARTWORK", "GameFontHighlightSmall")
	row.Text:SetPoint("TOPLEFT", row, "TOPLEFT", BOX, -1)
	row.Progress = row:CreateFontString(nil, "ARTWORK", "GameFontHighlightSmall")
	row.Progress:SetPoint("TOPLEFT", row.Text, "BOTTOMLEFT", 6, -2)
	for _, text in ipairs({ row.Text, row.Progress }) do
		text:SetWidth(width)
		text:SetJustifyH("LEFT")
		text:SetWordWrap(true)
	end
	row.Progress:SetWidth(width - 6)
	row.Text:SetMaxLines(3)
	row.Progress:SetMaxLines(8)

	row:SetScript("OnMouseUp", function(self, button)
		if button == "RightButton" then Plans.Toggle(self.n) else Plans.GoTo(self.n, true) end
	end)
	row.Box:SetScript("OnClick", function() Plans.Toggle(row.n) end)
	row:SetScript("OnEnter", RowTooltip)
	row:SetScript("OnLeave", function(self)
		self.Hover:Hide()
		hovered = nil
		GameTooltip:Hide()
	end)
	return row
end

local function HeaderButton(f, size, template, onClick, tooltip)
	local ok, b = pcall(CreateFrame, "Button", nil, f, template)
	if not ok then
		b = CreateFrame("Button", nil, f)
		b:SetNormalTexture("Interface\\Buttons\\UI-Panel-MinimizeButton-Up")
	end
	b:SetSize(size, size)
	b:SetScript("OnClick", onClick)
	b:SetScript("OnEnter", function(self)
		GameTooltip:SetOwner(self, "ANCHOR_TOP")
		GameTooltip:SetText(tooltip(), 1, 1, 1)
		GameTooltip:Show()
	end)
	b:SetScript("OnLeave", function() GameTooltip:Hide() end)
	return b
end

local function CreateWindow()
	local f = CreateFrame("Frame", "WowMCPPlanFrame", UIParent)
	f:SetSize(WIDTH, 60)
	f:SetFrameStrata("MEDIUM")
	f:SetClampedToScreen(true)
	f:SetMovable(true)
	f:SetDontSavePosition(true) -- we keep the position ourselves, in WowMCPDB
	f:EnableMouse(true)
	f:EnableMouseWheel(true)
	f:RegisterForDrag("LeftButton")
	local saved = WindowSaved()
	if saved.point then
		f:SetPoint(saved.point, UIParent, saved.relativePoint, saved.x, saved.y)
	else
		f:SetPoint("TOPLEFT", UIParent, "TOPLEFT", 24, -220)
	end

	local bg = f:CreateTexture(nil, "BACKGROUND")
	bg:SetAllPoints()
	bg:SetColorTexture(0, 0, 0, 0.55)

	-- The game's red close button; a plus/minus for collapsing, like the quest log's headers.
	f.Close = HeaderButton(f, 20, "UIPanelCloseButtonNoScripts", function() Plans.Hide() end,
		function() return "Hide the plan (/mcp plan show brings it back)" end)
	f.Close:SetPoint("TOPRIGHT", f, "TOPRIGHT", -2, -2)
	f.Collapse = HeaderButton(f, 14, nil, function() Plans.Collapse() end,
		function() return WindowSaved().collapsed and "Show all steps" or "Show only the current step" end)
	f.Collapse:SetHighlightTexture("Interface\\Buttons\\UI-PlusButton-Hilight", "ADD")
	f.Collapse:SetPoint("RIGHT", f.Close, "LEFT", -2, 0)
	f.Count = f:CreateFontString(nil, "ARTWORK", "GameFontHighlightSmall")
	f.Count:SetPoint("RIGHT", f.Collapse, "LEFT", -5, 0)
	f.Title = f:CreateFontString(nil, "ARTWORK", "GameFontNormal")
	f.Title:SetPoint("TOPLEFT", f, "TOPLEFT", PAD, -PAD + 2)
	f.Title:SetWidth(WIDTH - 2 * PAD - 80) -- the rest of the header: count, collapse and close
	f.Title:SetJustifyH("LEFT")
	f.Title:SetWordWrap(false)
	f.Earlier = f:CreateFontString(nil, "ARTWORK", "GameFontDisableSmall")
	f.Later = f:CreateFontString(nil, "ARTWORK", "GameFontDisableSmall")
	f.Rows = {}

	f:SetScript("OnDragStart", f.StartMoving)
	f:SetScript("OnDragStop", function(self)
		self:StopMovingOrSizing()
		SavePosition(self)
	end)
	f:SetScript("OnMouseWheel", function(_, delta) Plans.Scroll(delta) end)
	return f
end

-- The mouse wheel over the window (or a step) scrolls through the steps.
function Plans.Scroll(delta)
	if not (window and window.from) or WindowSaved().collapsed then return end
	first = math.max(1, math.min(window.from - delta, #steps - math.min(MAX_ROWS, #steps) + 1))
	Layout()
end

local function FillRow(row, step, current, done)
	row.n = step.n
	row.Check:SetShown(done)
	row.Current:SetShown(current)
	local color = done and GREY or current and WHITE or LIGHT
	row.Text:SetText(ns.RenderItems(string.format("%s%d.|r %s%s|r", current and GOLD or color, step.n, color, StepText(step.n))))
	local progress = current and not done and ProgressText(step)
	row.Progress:SetText(progress or "")
	row.Progress:SetShown(progress and true or false)
	local height = row.Text:GetStringHeight() + 3
	if progress then height = height + row.Progress:GetStringHeight() + 2 end
	row:SetHeight(math.max(BOX - 2, height))
	return math.max(BOX - 2, height)
end

-- Shows the window with the steps around the current one (or only the current one, collapsed).
function Layout()
	local state = State()
	if not (Active() and not state.hidden) then
		if window then window:Hide() end
		return
	end
	window = window or CreateWindow()
	local f = window
	f:Show()
	local collapsed = WindowSaved().collapsed
	f.Title:SetText(ns.ItemText(plan.title or "Plan"))
	f.Count:SetText(state.current and string.format("%d/%d", state.current, #steps) or GREEN .. "Done|r")
	f.Collapse:SetNormalTexture(collapsed and "Interface\\Buttons\\UI-PlusButton-Up" or "Interface\\Buttons\\UI-MinusButton-Up")

	local from, to
	if collapsed then
		from, to = state.current, state.current
	else
		local count = math.min(MAX_ROWS, #steps)
		from = math.max(1, math.min(first or (state.current or #steps) - 2, #steps - count + 1))
		to = from + count - 1
	end
	f.from = from
	for _, row in ipairs(f.Rows) do row:Hide() end

	local y = PAD + HEADER
	local function Note(text, shown)
		text:SetShown(shown)
		if not shown then return end
		text:ClearAllPoints()
		text:SetPoint("TOPLEFT", f, "TOPLEFT", PAD + BOX, -y)
		y = y + 12
	end
	f.Earlier:SetText(from and from > 1 and string.format("%d step%s above", from - 1, from > 2 and "s" or "") or "")
	Note(f.Earlier, from ~= nil and from > 1 and not collapsed)
	local i = 0
	for n = from or 1, from and to or 0 do
		i = i + 1
		local row = f.Rows[i] or CreateRow(f)
		f.Rows[i] = row
		row:ClearAllPoints()
		row:SetPoint("TOPLEFT", f, "TOPLEFT", PAD, -y)
		y = y + FillRow(row, steps[n], n == state.current, state.done[n] ~= nil) + GAP
		row:Show()
	end
	local below = to and #steps - to or 0
	f.Later:SetText(collapsed and not state.current and GREEN .. "All steps done.|r"
		or string.format("%d more step%s below", below, below > 1 and "s" or ""))
	Note(f.Later, (below > 0 and not collapsed) or (collapsed and not state.current))
	f:SetHeight(y + PAD - GAP)
end

---------------------------------------------------------------------------
-- What the player does
---------------------------------------------------------------------------

local function NoPlan()
	if Active() then return false end
	ns.Print("No plan. Ask your AI for one!")
	return true
end

local function Untick(n)
	local state = State()
	state.done[n] = nil
	state.undone[n] = true
	state.complete = nil
end

-- Tick step n off by hand.
local function Tick(n)
	local state = State()
	local before = state.current
	state.done[n], state.undone[n] = "click", nil
	FixCurrent()
	Announce({ n }, before, false)
	Plans.Check()
end

-- Make step n the current one. A done step is un-ticked first, unless it was a click on it in
-- the window (that only explains, so a misclick can't undo a step).
function Plans.GoTo(n, click)
	if NoPlan() then return end
	local state = State()
	if not steps[n] then
		ns.Print("No step " .. tostring(n) .. ". Type /mcp plan to list them.")
		return
	end
	if state.done[n] then
		if click then return end
		Untick(n)
	end
	state.current, state.hidden = n, nil
	first = nil
	Guide(true)
	if not click then ns.Print(string.format("Step %d/%d: %s", n, #steps, StepText(n))) end
	Plans.Check()
end

-- Mark step n done or not done by hand.
function Plans.Toggle(n)
	if NoPlan() or not steps[n] then return end
	local state = State()
	if not state.done[n] then return Tick(n) end
	Untick(n)
	state.current = n
	first = nil
	Guide(true)
	ns.Print(string.format("Step %d/%d marked not done: %s", n, #steps, StepText(n)))
	Plans.Check()
end

function Plans.Next()
	if NoPlan() then return end
	local current = State().current
	if not current then
		ns.Print("The plan is complete.")
		return
	end
	Tick(current)
end

function Plans.Prev()
	if NoPlan() then return end
	local n = (State().current or #steps + 1) - 1
	if n < 1 then
		ns.Print("Already at the first step.")
		return
	end
	Plans.GoTo(n)
end

function Plans.List()
	if NoPlan() then return end
	local state = State()
	ns.Print(string.format("|cffffffff%s|r (%s)%s", ns.Escape(plan.title or "Plan"),
		state.current and string.format("step %d/%d", state.current, #steps) or "complete",
		state.hidden and " - hidden, /mcp plan show brings it back" or ""))
	for _, step in ipairs(steps) do
		local mark = state.done[step.n] and (TICK .. " ") or (step.n == state.current and "|cff33ff33>|r ") or ""
		ns.Print(string.format("%s%d. %s", mark, step.n, StepText(step.n)))
	end
end

function Plans.Show()
	if NoPlan() then return end
	State().hidden = nil
	first = nil
	Guide(true)
	Plans.Check()
end

function Plans.Hide()
	if NoPlan() then return end
	State().hidden = true
	Guide()
	Layout()
	ns.Print("Plan hidden; it keeps track of your progress. /mcp plan show brings it back.")
end

function Plans.Collapse()
	local saved = WindowSaved()
	saved.collapsed = not saved.collapsed or nil
	Layout()
end

function Plans.Reset()
	if NoPlan() then return end
	local state = State()
	state.done, state.undone, state.current, state.complete, state.hidden = {}, {}, nil, nil, nil
	first = nil
	ns.Print("Plan progress reset.")
	local ok, err = pcall(Evaluate, true) -- steps that are done in the game tick off again, quietly
	if not ok then Report(err) end
	Guide(true)
end

function Plans.Clear()
	if NoPlan() then return end
	State().cleared = true
	Guide()
	Layout()
	ns.Print("Plan removed. Ask your AI for a new one!")
end

---------------------------------------------------------------------------
-- Payload
---------------------------------------------------------------------------

local function ParseStep(raw, n, missing)
	local step = {
		n = n, text = tostring(raw.text or ("Step " .. n)), loop = raw.loop == true, conditions = {},
		places = type(raw.places) == "table" and raw.places or {},
		areas = type(raw.areas) == "table" and raw.areas or {},
	}
	for _, list in ipairs({ step.places, step.areas }) do
		for _, r in ipairs(list) do
			if type(r) ~= "table" or not ns.Map.Resolve(r.zone, r.mapID) then missing[ns.Escape(type(r) == "table" and r.zone or "?")] = true end
		end
	end
	for _, raw in ipairs(type(raw.conditions) == "table" and raw.conditions or {}) do
		local c = ParseCondition(raw)
		if c.kind == "at" and not c.loc then missing[ns.Escape(c.zone or "?")] = true end
		table.insert(step.conditions, c)
	end
	return step
end

-- A plan we've already announced (e.g. restored by a /reload, or sent again with another
-- feature's update) is picked up where it was, quietly.
function Plans.Receive(payload)
	local state = WowMCPDB.plan
	local isNew = not state or state.id ~= payload.id
	if isNew then
		state = { id = payload.id }
		WowMCPDB.plan = state
	end
	state.done, state.undone = state.done or {}, state.undone or {}
	plan = payload
	wipe(steps)
	wipe(wanted)
	local missing = {}
	for _, raw in ipairs(type(payload.steps) == "table" and payload.steps or {}) do
		if type(raw) == "table" then table.insert(steps, ParseStep(raw, #steps + 1, missing)) end
	end
	if not Active() then
		Guide()
		Layout()
		if isNew and #steps == 0 then ns.Print("Plan cleared.") end
		return
	end
	if isNew then
		first = nil
		ns.Print(string.format("New plan: |cffffffff%s|r (%d steps). /mcp plan lists them.", ns.Escape(plan.title or "Plan"), #steps))
		local zones = {}
		for zone in pairs(missing) do zones[#zones + 1] = zone end
		table.sort(zones)
		if #zones > 0 then ns.Print("|cffff6060Couldn't find these zones on the map:|r " .. table.concat(zones, ", ")) end
	end
	local ok, err = pcall(Evaluate, isNew) -- a new plan's steps that are already done tick off quietly
	if not ok then Report(err) end
	if isNew then
		local already = {}
		for n in pairs(state.done) do already[#already + 1] = n end
		table.sort(already)
		if #already > 0 and state.current then
			ns.Print(string.format("Already done: step%s %s.", #already > 1 and "s" or "", table.concat(already, ", ")))
		end
	end
	Guide(isNew)
	Layout()
end

-- For get_plan_progress: each step's state and the live progress of the ones not done.
local function Progress()
	if not Active() then return { none = true, cleared = plan ~= nil and #steps > 0 or nil } end
	Plans.Check()
	local state = State()
	local list = {}
	for _, step in ipairs(steps) do
		local entry = { done = state.done[step.n] ~= nil, byHand = state.done[step.n] == "click" or nil }
		if not entry.done then
			entry.needsClick = not Checkable(step) or state.undone[step.n] or nil
			entry.progress = {}
			for _, c in ipairs(step.conditions) do
				table.insert(entry.progress, { kind = c.kind, text = c.text or "", met = c.met or false })
			end
		end
		list[#list + 1] = entry
	end
	return {
		id = plan.id, title = plan.title, current = state.current, complete = state.complete or false,
		hidden = state.hidden or false, steps = list, problem = problem,
	}
end

---------------------------------------------------------------------------

local SUBCOMMANDS = {
	[""] = Plans.List, list = Plans.List, show = Plans.Show, hide = Plans.Hide, next = Plans.Next, prev = Plans.Prev,
	reset = Plans.Reset, clear = Plans.Clear,
}

local USAGE = "/mcp plan [show, hide, next, prev, <n>, reset, clear] - the plan from your AI: list its steps, "
	.. "show or hide its checklist, tick off the current step, go back, go to step n, start over, remove it"

function Plans.Command(rest)
	rest = strtrim(tostring(rest or "")):lower()
	local run = SUBCOMMANDS[rest]
	if run then
		run()
	elseif tonumber(rest) then
		Plans.GoTo(math.floor(tonumber(rest)))
	else
		ns.Print(USAGE)
	end
end

ns.RegisterReceiver("plan", function(payload)
	if type(payload) ~= "table" then return end
	local ok, err = pcall(Plans.Receive, payload)
	if not ok then Report(err) end
end)
ns.RegisterSection(ns.Kinds.STATE, "plan", Progress)
ns.RegisterCommand("plan", USAGE, Plans.Command)
ns.OnLogin(function()
	C_Timer.NewTicker(CHECK_INTERVAL, Plans.Check)
end)
-- Items named in steps that were still loading: show their names.
ns.OnItemsLoaded(function()
	if not plan then return end
	Soon()
	if hovered and hovered:IsShown() and GameTooltip:GetOwner() == hovered then RowTooltip(hovered) end
end)

-- What can tick a step off. Events only make it quicker: the ticker checks every second anyway.
local events = CreateFrame("Frame")
for _, event in ipairs({
	"BAG_UPDATE_DELAYED", "PLAYERBANKSLOTS_CHANGED", "BANKFRAME_OPENED",
	"QUEST_ACCEPTED", "QUEST_TURNED_IN", "QUEST_REMOVED", "QUEST_LOG_UPDATE", "UNIT_QUEST_LOG_CHANGED",
	"PLAYER_LEVEL_UP", "SKILL_LINES_CHANGED", "TRADE_SKILL_LIST_UPDATE",
	-- Learning: Knowledge.lua keeps what these say (the chat line may be the only one on Forever).
	"NEW_RECIPE_LEARNED", "LEARNED_SPELL_IN_SKILL_LINE", "CHAT_MSG_SYSTEM",
	"ZONE_CHANGED", "ZONE_CHANGED_INDOORS", "ZONE_CHANGED_NEW_AREA", "PLAYER_ENTERING_WORLD",
	"GET_ITEM_INFO_RECEIVED", "ITEM_DATA_LOAD_RESULT",
}) do
	pcall(events.RegisterEvent, events, event)
end
events:SetScript("OnEvent", function(_, event, arg1)
	if event == "QUEST_TURNED_IN" and arg1 then
		session.turnedIn[arg1] = true -- the completed-quest flag can lag behind the event
	elseif event == "PLAYER_LEVEL_UP" then
		session.level = tonumber(arg1) -- UnitLevel can still say the old level
	elseif (event == "GET_ITEM_INFO_RECEIVED" or event == "ITEM_DATA_LOAD_RESULT") and not wanted[arg1] then
		return
	end
	if plan then Soon() end
end)
