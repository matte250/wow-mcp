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
--
-- There can be several plans, each under a tab above the window with its name and an icon for its
-- kind (questing, the player's class, a profession, or general). One is open at a time: that's the
-- plan the rest of this file works with, and the only one checked. The others keep their progress
-- (WowMCPDB.plans) and catch up when their tab is opened. The x on a tab closes its plan: archived
-- (the archive button at the end of the tabs brings it back, with its progress) or removed.

local _, ns = ...

local Plans = {}
ns.Plans = Plans

local CHECK_INTERVAL = 1 -- seconds; positions have no events, and the window shows distances
local DEFAULT_YARDS = 12
local MAX_ROWS = 8       -- steps shown at once; the mouse wheel scrolls through the rest
local WIDTH, PAD, BOX, GAP, HEADER = 300, 8, 18, 3, 20
-- The player can drag the window's corner: wider or narrower, more or fewer steps at once.
local MIN_WIDTH, MAX_WIDTH, MIN_ROWS, MOST_ROWS, ROW_DRAG = 220, 600, 2, 25, 22
local TAB_HEIGHT, TAB_GAP, TAB_ICON, TAB_PAD, TAB_MAX = 20, 2, 14, 4, 120
local TAB_NAME = 20      -- characters of a tab's name
local TAB_CLOSE = 10     -- the x on a tab
local SHELF_WIDTH, SHELF_LIST, SHELF_ROW = 34, 230, 18 -- the archive button, and its list of archived plans
-- The models of a step's NPCs, beside its tooltip: how many at once, their size, the room for a name.
local MAX_MODELS, MODEL_WIDTH, MODEL_HEIGHT, MODEL_GAP, MODEL_NAME = 3, 130, 170, 4, 16
local SOUNDS = { step = { "IG_QUEST_LIST_COMPLETE", 878 }, plan = { "READY_CHECK", 8960 } } -- SOUNDKIT names, IDs
local TICK = "|TInterface\\Buttons\\UI-CheckBox-Check:0|t"
local GREEN, GREY, GOLD, WHITE, LIGHT, TODO = "|cff33ff33", "|cff808080", "|cffffd100", "|cffffffff", "|cffd9d9d9", "|cffffcc66"
-- Skill line IDs of the professions and secondary skills, for lines under a collapsed header.
local SKILL_IDS = { 164, 165, 171, 182, 186, 197, 202, 333, 393, 129, 185, 356 }

local plans = {}  -- every plan the AI sent, in tab order: { id, tab, kind, profession, title, steps }
local plan        -- the one that's open: the payload being shown
local steps = {}  -- parsed: { n, text, places, loop, areas, conditions = { { kind, value, ..., met, text } } }
local wanted = {} -- item IDs the plan counts: their names may arrive later
local session = { turnedIn = {} } -- what events said before the game's own lists catch up
local problem, reported -- the last error while checking, and the last one printed
local window
local hovered     -- the row whose tooltip is showing
local first       -- the first step shown in the window; nil follows the current step
local pending     -- a check is scheduled

-- WowMCPDB.plan: progress of the open plan: { id, done = { [n] = "auto" | "click" },
-- undone = { [n] = true } (un-ticked by hand: not ticked again until its conditions have failed),
-- current, complete, hidden, cleared (removed by the player), archived (put away by the player),
-- name (the tab's name, if the player renamed it) }. A plan
-- with another id starts over. WowMCPDB.plans has the same for every plan, by id.
local function State()
	return WowMCPDB.plan
end

local function States()
	WowMCPDB.plans = WowMCPDB.plans or {}
	return WowMCPDB.plans
end

local function StateOf(p)
	local states = States()
	local state = states[p.id]
	if not state then
		state = { id = p.id }
		states[p.id] = state
	end
	state.done, state.undone = state.done or {}, state.undone or {}
	return state
end

-- The plans that have a tab: all but the ones the player removed or archived.
local function Tabs()
	local list = {}
	for _, p in ipairs(plans) do
		local state = StateOf(p)
		if not state.cleared and not state.archived then list[#list + 1] = p end
	end
	return list
end

-- The plans the player archived, oldest first.
local function Archived()
	local list = {}
	for _, p in ipairs(plans) do
		local state = StateOf(p)
		if state.archived and not state.cleared then list[#list + 1] = p end
	end
	return list
end

local function Active()
	local state = State()
	return plan ~= nil and #steps > 0 and state ~= nil and not state.cleared and not state.archived
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
-- Tabs: a plan's kind, icon and name
---------------------------------------------------------------------------

local ICONS = "Interface\\Icons\\"
local KIND_ICONS = {
	questing = "Interface\\GossipFrame\\AvailableQuestIcon",
	class = ICONS .. "INV_Misc_Book_09",
	profession = ICONS .. "INV_Misc_Gear_01",
	general = ICONS .. "INV_Misc_Note_01",
}
local KIND_NAMES = { questing = "Questing", class = "Class guide", profession = "Profession", general = "General" }
-- By normalized name, for professions the character doesn't have (the game gives the icon of one they have).
local PROFESSION_ICONS = {
	alchemy = "Trade_Alchemy", blacksmithing = "Trade_BlackSmithing", cooking = "INV_Misc_Food_15",
	enchanting = "Trade_Engraving", engineering = "Trade_Engineering", firstaid = "Spell_Holy_SealOfSacrifice",
	fishing = "Trade_Fishing", herbalism = "Trade_Herbalism", inscription = "INV_Inscription_Tradeskill01",
	jewelcrafting = "INV_Misc_Gem_01", leatherworking = "Trade_LeatherWorking", mining = "Trade_Mining",
	skinning = "INV_Misc_Pelt_Wolf_01", tailoring = "Trade_Tailoring",
}

-- A plan's kind and, for a profession plan, its profession. The AI says; a plan without a kind
-- (sent before there were tabs) is told from what its steps wait for.
local function KindOf(p)
	if KIND_ICONS[p.kind] then return p.kind, p.profession end
	local quests
	for _, raw in ipairs(type(p.steps) == "table" and p.steps or {}) do
		for _, c in ipairs(type(raw) == "table" and type(raw.conditions) == "table" and raw.conditions or {}) do
			if type(c) == "table" then
				if c.skill then return "profession", tostring(c.skill) end
				if c.recipe then return "profession", nil end
				quests = quests or c.questAccepted or c.questComplete or c.questObjective or c.questTurnedIn
			end
		end
	end
	return quests and "questing" or "general", nil
end

local function TabIcon(p)
	local kind, profession = KindOf(p)
	if kind == "class" then
		local _, class = UnitClass("player")
		if class then return ICONS .. "ClassIcon_" .. class end
	elseif kind == "profession" and profession then
		if C_Spell and C_Spell.GetSpellTexture then
			local ok, icon = pcall(C_Spell.GetSpellTexture, profession) -- a profession the character has
			if ok and icon then return icon end
		end
		local known = PROFESSION_ICONS[Normalize(profession)]
		if known then return ICONS .. known end
	end
	return KIND_ICONS[kind]
end

-- The tab's name as the AI sent it: what show_plan and clear_plan know the plan by.
local function SentTab(p)
	return type(p.tab) == "string" and p.tab ~= "" and p.tab or "Plan"
end

-- The name on the tab: the player's, if they named it (/mcp plan name).
local function TabName(p)
	local name = StateOf(p).name
	return type(name) == "string" and name ~= "" and name or SentTab(p)
end

-- What the tab's tooltip and /mcp plan tabs say about a plan's kind.
local function KindText(p)
	local kind, profession = KindOf(p)
	if kind == "class" then return (UnitClass("player")) or KIND_NAMES.class end
	return kind == "profession" and profession or KIND_NAMES[kind]
end

---------------------------------------------------------------------------
-- The window
---------------------------------------------------------------------------

local function WindowSaved()
	WowMCPDB.planWindow = WowMCPDB.planWindow or {}
	return WowMCPDB.planWindow
end

-- The window's width and how many steps it shows at once: the player's, if they resized it.
local function Width()
	local width = tonumber(WindowSaved().width)
	return width and math.max(MIN_WIDTH, math.min(MAX_WIDTH, math.floor(width + 0.5))) or WIDTH
end

local function MaxRows()
	local rows = tonumber(WindowSaved().rows)
	return rows and math.max(MIN_ROWS, math.min(MOST_ROWS, math.floor(rows + 0.5))) or MAX_ROWS
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

---------------------------------------------------------------------------
-- NPC models: a step can name the characters and mobs it's about (step.npcs). While the mouse is
-- over the step, and only then, they're shown beside its tooltip. Nothing is fetched: the game
-- draws a creature from its ID.
---------------------------------------------------------------------------

local models = {} -- the boxes, made when first needed and shared by all steps: { Model, Name, npc }

-- A model method the client may not have, or that may fail for a creature it doesn't know.
local function Call(model, method, ...)
	if not model[method] then return false end
	return pcall(model[method], model, ...)
end

-- Whether the client says the model has nothing to draw. Clients that can't say: not empty.
local function Empty(model)
	local ok, file = Call(model, "GetModelFileID")
	return ok and (file == nil or file == 0)
end

-- The boxes sit in one frame that's kept on screen as a whole: boxes kept on screen one by one
-- would pile up on each other at the screen's edge.
local holder

local function Holder()
	if not holder then
		holder = CreateFrame("Frame", nil, UIParent)
		holder:SetFrameStrata("TOOLTIP")
		holder:SetClampedToScreen(true)
		holder:Hide()
	end
	return holder
end

-- A frame's edge or size in UIParent's units, or nil if the client can't say (yet).
local function Measure(frame, method)
	local ok, value = pcall(frame[method] or error, frame)
	if not ok or type(value) ~= "number" then return nil end
	local okFrame, scale = pcall(frame.GetEffectiveScale or error, frame)
	local okParent, parentScale = pcall(UIParent.GetEffectiveScale or error, UIParent)
	if okFrame and okParent and type(scale) == "number" and type(parentScale) == "number" and parentScale > 0 then
		value = value * scale / parentScale
	end
	return value
end

-- The boxes with something to see, beside the tooltip on the side of the screen with more room,
-- nearest it first; in rows when one row doesn't fit there. A box that's waiting for its model is
-- shown too (a hidden model may never load), but can't be seen and takes no place: it waits after
-- the others (a frame that's nowhere may not load its model either).
local function PlaceModels()
	local seen, waiting = {}, {}
	for _, box in ipairs(models) do
		if box:IsShown() then table.insert(box:GetAlpha() > 0 and seen or waiting, box) end
	end
	local left, right = Measure(GameTooltip, "GetLeft"), Measure(GameTooltip, "GetRight")
	local screen = Measure(UIParent, "GetWidth")
	local stepX, stepY = MODEL_WIDTH + MODEL_GAP, MODEL_HEIGHT + MODEL_NAME + MODEL_GAP
	local count = math.max(#seen, 1)
	-- The right if they fit there in a row, else the left if they fit there, else the roomier side.
	local onRight, room = true, nil
	if left and right and screen then
		local roomRight, roomLeft, need = screen - right - MODEL_GAP, left - MODEL_GAP, count * stepX - MODEL_GAP
		onRight = roomRight >= need or (roomLeft < need and roomRight >= roomLeft)
		room = onRight and roomRight or roomLeft
	end
	local perRow = room and math.max(1, math.min(count, math.floor((room + MODEL_GAP) / stepX))) or count
	local h = Holder()
	h:SetSize(perRow * stepX - MODEL_GAP, math.ceil(count / perRow) * stepY - MODEL_GAP)
	h:ClearAllPoints()
	if onRight then
		h:SetPoint("TOPLEFT", GameTooltip, "TOPRIGHT", MODEL_GAP, 0)
	else
		h:SetPoint("TOPRIGHT", GameTooltip, "TOPLEFT", -MODEL_GAP, 0)
	end
	h:Show()
	local function place(box, slot)
		local column, row = slot % perRow, math.floor(slot / perRow)
		if not onRight then column = perRow - 1 - column end
		box:ClearAllPoints()
		box:SetPoint("TOPLEFT", h, "TOPLEFT", column * stepX, -row * stepY)
	end
	for i, box in ipairs(seen) do place(box, i - 1) end
	for _, box in ipairs(waiting) do place(box, #seen) end
end

local function CreateModel()
	local box = CreateFrame("Frame", nil, Holder())
	box:SetSize(MODEL_WIDTH, MODEL_HEIGHT + MODEL_NAME)
	box:SetFrameStrata("TOOLTIP")
	box:Hide()
	box.Back = box:CreateTexture(nil, "BACKGROUND")
	box.Back:SetAllPoints()
	box.Back:SetColorTexture(0, 0, 0, 0.8)
	box.Name = box:CreateFontString(nil, "ARTWORK", "GameFontHighlightSmall")
	box.Name:SetPoint("BOTTOM", box, "BOTTOM", 0, 3)
	box.Name:SetWidth(MODEL_WIDTH - 6)
	box.Name:SetWordWrap(false)
	local ok, model = pcall(CreateFrame, "PlayerModel", nil, box)
	if not ok or not model then return nil end -- a client without model frames: the tooltip names the NPCs
	box.Model = model
	model:SetSize(MODEL_WIDTH, MODEL_HEIGHT)
	model:SetPoint("TOP", box, "TOP", 0, 0)
	-- A creature the client had to load first is there a moment after it was asked for: its box
	-- waited, unseen. Not after the mouse left the step.
	pcall(model.SetScript, model, "OnModelLoaded", function()
		if not box.npc or box:GetAlpha() > 0 then return end
		box:SetAlpha(1)
		PlaceModels()
	end)
	return box
end

-- By its ID; by its display ID if that shows nothing (or this client can't). False if there's
-- nothing to see (yet), so an NPC the client doesn't know leaves no empty box.
local function ShowCreature(model, npc)
	Call(model, "ClearModel")
	local asked = Call(model, "SetCreature", npc.id)
	if npc.display and (not asked or Empty(model)) then Call(model, "SetDisplayInfo", npc.display) end
	return not Empty(model)
end

local function HideModels()
	for _, box in ipairs(models) do
		box.npc = nil
		box:Hide()
	end
	if holder then holder:Hide() end
end

local function SameNpc(a, b)
	return a and b and a.id == b.id and a.display == b.display
end

local function ShowModels(npcs)
	local wanted = math.min(#npcs, MAX_MODELS)
	for i = wanted + 1, #models do
		models[i].npc = nil
		models[i]:Hide()
	end
	for i = 1, wanted do
		local box = models[i] or CreateModel()
		if not box then break end
		models[i] = box
		local npc = npcs[i]
		-- A creature that's already on show keeps going: asking for it again restarts its animation.
		if not (box:IsShown() and SameNpc(box.npc, npc)) then
			box.npc = npc
			box.Name:SetText(npc.name and ns.Escape(npc.name) or "")
			box:Show() -- first, and it stays shown while the step is hovered: a hidden model may not load
			box:SetAlpha(ShowCreature(box.Model, npc) and 1 or 0) -- nothing to see: unseen until OnModelLoaded
		end
	end
	PlaceModels()
end

local function RowTooltip(row)
	row.Hover:Show()
	hovered = row
	row.tipFor = row.n
	local step = steps[row.n]
	if not step then return HideModels() end
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
	-- Named here too: a model the client can't draw shouldn't take the name with it.
	for _, npc in ipairs(step.npcs) do
		if npc.name then GameTooltip:AddLine("NPC: " .. ns.Escape(npc.name), 0.7, 0.7, 0.7, true) end
	end
	if done then
		GameTooltip:AddLine("Done. Right-click or click the box to mark it not done.", 0.4, 0.8, 1, true)
	else
		GameTooltip:AddLine("Click: make this the current step. Right-click or click the box: mark it done.", 0.4, 0.8, 1, true)
	end
	GameTooltip:Show()
	ShowModels(step.npcs) -- after the tooltip, which they sit beside
end

-- The mouse left the step, or the step went from under it.
local function RowLeave(row)
	row.Hover:Hide()
	if hovered == row then hovered = nil end
	GameTooltip:Hide()
	HideModels()
end

local function CreateRow(f)
	local row = CreateFrame("Frame", nil, f)
	row:EnableMouse(true)
	row:EnableMouseWheel(true)
	row:SetScript("OnMouseWheel", function(_, delta) Plans.Scroll(delta) end)
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

	row.Text = row:CreateFontString(nil, "ARTWORK", "GameFontHighlightSmall")
	row.Text:SetPoint("TOPLEFT", row, "TOPLEFT", BOX, -1)
	row.Progress = row:CreateFontString(nil, "ARTWORK", "GameFontHighlightSmall")
	row.Progress:SetPoint("TOPLEFT", row.Text, "BOTTOMLEFT", 6, -2)
	for _, text in ipairs({ row.Text, row.Progress }) do
		text:SetJustifyH("LEFT")
		text:SetWordWrap(true)
	end
	row.Text:SetMaxLines(3)
	row.Progress:SetMaxLines(8)

	row:SetScript("OnMouseUp", function(self, button)
		if button == "RightButton" then Plans.Toggle(self.n) else Plans.GoTo(self.n, true) end
	end)
	row.Box:SetScript("OnClick", function() Plans.Toggle(row.n) end)
	row:SetScript("OnEnter", RowTooltip)
	row:SetScript("OnLeave", RowLeave)
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

local function CreateTab(f)
	local tab = CreateFrame("Frame", nil, f)
	tab:EnableMouse(true)
	tab:SetHeight(TAB_HEIGHT)
	tab.Back = tab:CreateTexture(nil, "BACKGROUND")
	tab.Back:SetAllPoints()
	tab.Icon = tab:CreateTexture(nil, "ARTWORK")
	tab.Icon:SetSize(TAB_ICON, TAB_ICON)
	tab.Icon:SetPoint("LEFT", tab, "LEFT", TAB_PAD, 0)
	tab.Name = tab:CreateFontString(nil, "ARTWORK", "GameFontHighlightSmall")
	tab.Name:SetPoint("LEFT", tab.Icon, "RIGHT", 3, 0)
	tab.Name:SetJustifyH("LEFT")
	tab.Name:SetWordWrap(false)
	tab.Close = CreateFrame("Frame", nil, tab)
	tab.Close:SetSize(TAB_CLOSE, TAB_CLOSE)
	tab.Close:SetPoint("RIGHT", tab, "RIGHT", -3, 0)
	tab.Close:EnableMouse(true)
	tab.Close.X = tab.Close:CreateTexture(nil, "ARTWORK")
	tab.Close.X:SetAllPoints()
	tab.Close.X:SetTexture("Interface\\Buttons\\UI-StopButton")
	tab.Close:SetScript("OnMouseUp", function() Plans.AskClose(tab.id) end)
	tab.Close:SetScript("OnEnter", function(self)
		GameTooltip:SetOwner(self, "ANCHOR_TOP")
		GameTooltip:SetText("Close this plan: archive it or remove it", 1, 1, 1)
		GameTooltip:Show()
	end)
	tab.Close:SetScript("OnLeave", function() GameTooltip:Hide() end)
	tab:SetScript("OnMouseUp", function(self) Plans.Open(nil, self.id) end)
	tab:SetScript("OnEnter", function(self)
		local p = self.plan
		if not p then return end
		GameTooltip:SetOwner(self, "ANCHOR_TOP")
		GameTooltip:SetText(ns.Escape(TabName(p)), 1, 0.82, 0)
		GameTooltip:AddLine(ns.ItemText(p.title or "Plan"), 1, 1, 1, true)
		GameTooltip:AddLine(ns.Escape(KindText(p)), 0.7, 0.7, 0.7)
		if not self.open then GameTooltip:AddLine("Click: show this plan.", 0.4, 0.8, 1, true) end
		GameTooltip:Show()
	end)
	tab:SetScript("OnLeave", function() GameTooltip:Hide() end)
	return tab
end

-- The archive button: at the right end of the tabs while there are archived plans. Clicking it
-- lists them; clicking one brings it back.
local function CreateShelf(f)
	local shelf = CreateFrame("Frame", nil, f)
	shelf:EnableMouse(true)
	shelf:SetSize(SHELF_WIDTH, TAB_HEIGHT)
	shelf:SetPoint("BOTTOMRIGHT", f, "TOPRIGHT", 0, 1)
	shelf.Back = shelf:CreateTexture(nil, "BACKGROUND")
	shelf.Back:SetAllPoints()
	shelf.Back:SetColorTexture(0, 0, 0, 0.4)
	shelf.Icon = shelf:CreateTexture(nil, "ARTWORK")
	shelf.Icon:SetSize(TAB_ICON, TAB_ICON)
	shelf.Icon:SetPoint("LEFT", shelf, "LEFT", TAB_PAD, 0)
	shelf.Icon:SetTexture(ICONS .. "INV_Box_02")
	shelf.Count = shelf:CreateFontString(nil, "ARTWORK", "GameFontHighlightSmall")
	shelf.Count:SetPoint("LEFT", shelf.Icon, "RIGHT", 3, 0)
	shelf:SetScript("OnMouseUp", function() Plans.ToggleArchive() end)
	shelf:SetScript("OnEnter", function(self)
		GameTooltip:SetOwner(self, "ANCHOR_TOP")
		GameTooltip:SetText("Archived plans", 1, 0.82, 0)
		GameTooltip:AddLine("Click to list them, then click one to bring it back.", 0.4, 0.8, 1, true)
		GameTooltip:Show()
	end)
	shelf:SetScript("OnLeave", function() GameTooltip:Hide() end)

	shelf.List = CreateFrame("Frame", nil, f)
	shelf.List:SetFrameStrata("DIALOG")
	shelf.List:EnableMouse(true)
	shelf.List:SetWidth(SHELF_LIST)
	shelf.List:SetPoint("TOPRIGHT", f, "TOPRIGHT", 0, 0)
	shelf.List.Back = shelf.List:CreateTexture(nil, "BACKGROUND")
	shelf.List.Back:SetAllPoints()
	shelf.List.Back:SetColorTexture(0, 0, 0, 0.92)
	shelf.List.Rows = {}
	shelf.List:Hide()
	return shelf
end

local function CreateShelfRow(list)
	local row = CreateFrame("Frame", nil, list)
	row:EnableMouse(true)
	row:SetSize(SHELF_LIST - 2 * TAB_PAD, SHELF_ROW)
	row.Hover = row:CreateTexture(nil, "BACKGROUND")
	row.Hover:SetAllPoints()
	row.Hover:SetColorTexture(1, 1, 1, 0.1)
	row.Hover:Hide()
	row.Icon = row:CreateTexture(nil, "ARTWORK")
	row.Icon:SetSize(TAB_ICON, TAB_ICON)
	row.Icon:SetPoint("LEFT", row, "LEFT", 0, 0)
	row.Name = row:CreateFontString(nil, "ARTWORK", "GameFontHighlightSmall")
	row.Name:SetPoint("LEFT", row.Icon, "RIGHT", 4, 0)
	row.Name:SetWidth(SHELF_LIST - 2 * TAB_PAD - TAB_ICON - 4)
	row.Name:SetJustifyH("LEFT")
	row.Name:SetWordWrap(false)
	row:SetScript("OnMouseUp", function(self, button)
		if button == "RightButton" then Plans.Remove(self.id) else Plans.Restore(nil, self.id) end
	end)
	row:SetScript("OnEnter", function(self)
		self.Hover:Show()
		GameTooltip:SetOwner(self, "ANCHOR_RIGHT")
		GameTooltip:SetText(ns.ItemText(self.plan and self.plan.title or "Plan"), 1, 1, 1, 1, true)
		GameTooltip:AddLine("Click: bring this plan back. Right-click: remove it for good.", 0.4, 0.8, 1, true)
		GameTooltip:Show()
	end)
	row:SetScript("OnLeave", function(self)
		self.Hover:Hide()
		GameTooltip:Hide()
	end)
	return row
end

local function LayoutShelf(f, archived)
	if #archived == 0 then
		if f.Shelf then
			f.Shelf:Hide()
			f.Shelf.List:Hide()
		end
		f.shelfOpen = nil
		return
	end
	f.Shelf = f.Shelf or CreateShelf(f)
	local shelf = f.Shelf
	shelf:Show()
	shelf.Count:SetText(GREY .. #archived .. "|r")
	shelf.Back:SetColorTexture(0, 0, 0, f.shelfOpen and 0.75 or 0.4)
	local list = shelf.List
	for _, row in ipairs(list.Rows) do row:Hide() end
	list:SetShown(f.shelfOpen and true or false)
	if not f.shelfOpen then return end
	for i, p in ipairs(archived) do
		local row = list.Rows[i] or CreateShelfRow(list)
		list.Rows[i] = row
		row.id, row.plan = p.id, p
		row:ClearAllPoints()
		row:SetPoint("TOPLEFT", list, "TOPLEFT", TAB_PAD, -TAB_PAD - (i - 1) * SHELF_ROW)
		row.Icon:SetTexture(TabIcon(p))
		row.Name:SetText(GOLD .. ns.Escape(TabName(p)) .. "|r  " .. ns.ItemText(p.title or "Plan"))
		row:Show()
	end
	list:SetHeight(#archived * SHELF_ROW + 2 * TAB_PAD)
end

-- The tabs sit on top of the window, one per plan, sharing its width (less the archive button).
local function LayoutTabs(f)
	local tabs, archived = Tabs(), Archived()
	for _, tab in ipairs(f.Tabs) do tab:Hide() end
	LayoutShelf(f, archived)
	if #tabs == 0 then return end
	local room = Width() - (#archived > 0 and SHELF_WIDTH + TAB_GAP or 0)
	local width = math.min(TAB_MAX, math.floor((room - (#tabs - 1) * TAB_GAP) / #tabs))
	for i, p in ipairs(tabs) do
		local tab = f.Tabs[i] or CreateTab(f)
		f.Tabs[i] = tab
		local open = plan ~= nil and p.id == plan.id
		tab.id, tab.plan, tab.open = p.id, p, open
		tab:SetWidth(width)
		tab:ClearAllPoints()
		tab:SetPoint("BOTTOMLEFT", f, "TOPLEFT", (i - 1) * (width + TAB_GAP), 1)
		tab.Back:SetColorTexture(0, 0, 0, open and 0.75 or 0.4)
		tab.Icon:SetTexture(TabIcon(p))
		tab.Name:SetWidth(math.max(1, width - TAB_ICON - 2 * TAB_PAD - 3 - TAB_CLOSE - 3))
		tab.Name:SetText((open and GOLD or GREY) .. ns.Escape(TabName(p)) .. "|r")
		tab:Show()
	end
end

local function CreateWindow()
	local f = CreateFrame("Frame", "WowMCPPlanFrame", UIParent)
	f:SetSize(Width(), 60)
	f:SetFrameStrata("MEDIUM")
	f:SetClampedToScreen(true)
	if f.SetClampRectInsets then f:SetClampRectInsets(0, 0, TAB_HEIGHT + 1, 0) end -- the tabs stay on screen too
	f.Tabs = {}
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
	f.Title:SetWidth(Width() - 2 * PAD - 80) -- the rest of the header: count, collapse and close
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
	-- The drag of the corner ends when the button is let go, or the window goes while it's held
	-- (a hidden frame isn't told the button was let go).
	local function StopResizing()
		f.Grip:SetScript("OnUpdate", nil)
		f.Grip.Corner:SetTexture("Interface\\ChatFrame\\UI-ChatIM-SizeGrabber-Up")
	end
	f:SetScript("OnHide", function()
		HideModels() -- a step that's hidden under the mouse isn't told the mouse left
		StopResizing()
	end)

	-- The corner to drag, as on the chat window: sideways for the width, up and down for the steps shown.
	f.Grip = CreateFrame("Frame", nil, f)
	f.Grip:SetSize(16, 16)
	f.Grip:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", 0, 0)
	f.Grip:EnableMouse(true)
	f.Grip.Corner = f.Grip:CreateTexture(nil, "OVERLAY")
	f.Grip.Corner:SetAllPoints()
	f.Grip.Corner:SetTexture("Interface\\ChatFrame\\UI-ChatIM-SizeGrabber-Up")
	f.Grip:SetScript("OnMouseDown", function(self, button)
		if button and button ~= "LeftButton" then return end
		if not (GetCursorPosition and f.GetEffectiveScale) then return end
		-- Held by its top left corner, so it grows right and down from where it is.
		local left, top = f:GetLeft(), f:GetTop()
		if left and top then
			f:ClearAllPoints()
			f:SetPoint("TOPLEFT", UIParent, "BOTTOMLEFT", left, top)
			SavePosition(f)
		end
		local scale = f:GetEffectiveScale()
		local x0, y0 = GetCursorPosition()
		local width, rows = Width(), MaxRows()
		self.Corner:SetTexture("Interface\\ChatFrame\\UI-ChatIM-SizeGrabber-Down")
		self:SetScript("OnUpdate", function()
			-- Let go where the grip couldn't tell (the window was hidden meanwhile): the drag is over.
			if IsMouseButtonDown and not IsMouseButtonDown("LeftButton") then return StopResizing() end
			local x, y = GetCursorPosition()
			Plans.Resize(width + (x - x0) / scale, rows + (y0 - y) / scale / ROW_DRAG)
		end)
	end)
	f.Grip:SetScript("OnMouseUp", StopResizing)
	f.Grip:SetScript("OnHide", StopResizing)
	f.Grip:SetScript("OnEnter", function(self)
		GameTooltip:SetOwner(self, "ANCHOR_RIGHT")
		GameTooltip:SetText("Drag to resize", 1, 1, 1)
		GameTooltip:AddLine("Sideways: wider or narrower. Up and down: fewer or more steps at once.", 0.4, 0.8, 1, true)
		GameTooltip:Show()
	end)
	f.Grip:SetScript("OnLeave", function() GameTooltip:Hide() end)
	return f
end

-- The mouse wheel over the window (or a step) scrolls through the steps.
function Plans.Scroll(delta)
	if not (window and window.from) or WindowSaved().collapsed then return end
	first = math.max(1, math.min(window.from - delta, #steps - math.min(MaxRows(), #steps) + 1))
	Layout()
end

local function FillRow(row, step, current, done)
	local width = Width() - 2 * PAD
	row:SetWidth(width)
	row.Text:SetWidth(width - BOX)
	row.Progress:SetWidth(width - BOX - 6)
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
		-- No plan open, but some archived: the window stays, small, so the archive button is there.
		HideModels() -- no steps on show to hover
		if Active() or #Archived() == 0 or WindowSaved().shelfHidden then
			if window then window:Hide() end
			return
		end
		window = window or CreateWindow()
		local f = window
		f:Show()
		f:SetWidth(Width())
		f.Title:SetWidth(Width() - 2 * PAD - 80)
		LayoutTabs(f)
		f.from = nil
		f.Title:SetText(GREY .. "No plan open|r")
		f.Count:SetText("")
		for _, row in ipairs(f.Rows) do row:Hide() end
		f.Earlier:Hide()
		f.Later:SetText("Your archived plans are behind the box above.")
		f.Later:ClearAllPoints()
		f.Later:SetPoint("TOPLEFT", f, "TOPLEFT", PAD, -(PAD + HEADER))
		f.Later:Show()
		f:SetHeight(PAD + HEADER + 12 + PAD)
		return
	end
	window = window or CreateWindow()
	local f = window
	f:Show()
	f:SetWidth(Width())
	f.Title:SetWidth(Width() - 2 * PAD - 80)
	LayoutTabs(f)
	local collapsed = WindowSaved().collapsed
	f.Title:SetText(ns.ItemText(plan.title or "Plan"))
	f.Count:SetText(state.current and string.format("%d/%d", state.current, #steps) or GREEN .. "Done|r")
	f.Collapse:SetNormalTexture(collapsed and "Interface\\Buttons\\UI-PlusButton-Up" or "Interface\\Buttons\\UI-MinusButton-Up")

	local from, to
	if collapsed then
		from, to = state.current, state.current
	else
		local count = math.min(MaxRows(), #steps)
		from = math.max(1, math.min(first or (state.current or #steps) - 2, #steps - count + 1))
		to = from + count - 1
	end
	f.from = from
	-- Rows stay shown while they're refilled: a row hidden under the mouse is told the mouse left,
	-- and shown again that it came back, which would close its tooltip and restart its NPC models.

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
	for j = i + 1, #f.Rows do f.Rows[j]:Hide() end
	local below = to and #steps - to or 0
	f.Later:SetText(collapsed and not state.current and GREEN .. "All steps done.|r"
		or string.format("%d more step%s below", below, below > 1 and "s" or ""))
	Note(f.Later, (below > 0 and not collapsed) or (collapsed and not state.current))
	f:SetHeight(y + PAD - GAP)
	if hovered and not hovered:IsShown() then HideModels() end -- its step went from under the mouse
	-- Another step moved under the mouse (a new current step, scrolling): its tooltip and NPCs.
	if hovered and hovered:IsShown() and hovered.tipFor ~= hovered.n then RowTooltip(hovered) end
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
	if not Active() and #Archived() > 0 then -- just the archive button
		WindowSaved().shelfHidden = nil
		Layout()
		return
	end
	if NoPlan() then return end
	State().hidden = nil
	first = nil
	Guide(true)
	Plans.Check()
end

function Plans.Hide()
	if not Active() and #Archived() > 0 then
		WindowSaved().shelfHidden = true
		Layout()
		ns.Print("Hidden. /mcp plan show brings the archive button back; /mcp plan archived lists your archived plans.")
		return
	end
	if NoPlan() then return end
	State().hidden = true
	Guide()
	Layout()
	ns.Print("Plan hidden; it keeps track of your progress. /mcp plan show brings it back.")
end

-- The window's size: its width, and how many steps it shows at once (nil leaves one as it is,
-- "reset" goes back to the usual size).
function Plans.Resize(width, rows)
	local saved = WindowSaved()
	if width == "reset" then
		saved.width, saved.rows = nil, nil
	else
		local wide, many = Width(), MaxRows()
		if tonumber(width) then saved.width = tonumber(width) end
		if tonumber(rows) then saved.rows = tonumber(rows) end
		saved.width, saved.rows = Width(), MaxRows() -- within what's allowed
		if saved.width == wide and saved.rows == many then return end
	end
	first = nil
	Layout()
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

local function Find(id)
	for _, p in ipairs(plans) do
		if p.id == id then return p end
	end
end

-- Closes a plan: archived (it can be brought back, with its progress) or removed. If it was the
-- open one, another plan's tab opens, if there is one.
local function Close(p, archive)
	local state = StateOf(p)
	local name = TabName(p)
	local wasArchived = state.archived
	if archive then
		state.archived = true
	else
		state.cleared, state.archived = true, nil
	end
	WindowSaved().shelfHidden = nil
	local wasOpen = plan ~= nil and p.id == plan.id
	local other = wasOpen and Tabs()[1]
	if archive then
		ns.Print(string.format("Plan archived: |cffffffff%s|r. The box at the end of the tabs, or /mcp plan restore, brings it back.",
			ns.Escape(name)))
	elseif other or not wasOpen or wasArchived then
		ns.Print(string.format("Plan removed: |cffffffff%s|r.", ns.Escape(name)))
	else
		ns.Print("Plan removed. Ask your AI for a new one!")
	end
	if other then
		Plans.Open(nil, other.id)
		return
	end
	if wasOpen then Guide() end
	Layout()
end

-- Removes the open plan and its tab.
function Plans.Clear()
	if NoPlan() then return end
	Close(plan, false)
end

-- A plan asked for by an id that's no longer there (the AI replaced or removed it while the
-- player was being asked): nothing is done, least of all to another plan.
local function Gone()
	ns.Print("That plan is gone: your AI replaced or removed it.")
end

-- Removes a plan by id (the open one without): from the x on its tab, or from the archive.
function Plans.Remove(id)
	if id == nil then return Plans.Clear() end
	local p = Find(id)
	if not p then return Gone() end
	Close(p, false)
end

-- Archives a plan by id (the open one without): its tab goes, its progress is kept.
function Plans.Archive(id)
	local p
	if id == nil then
		if NoPlan() then return end
		p = plan
	else
		p = Find(id)
		if not p then return Gone() end
	end
	if StateOf(p).archived then return end
	Close(p, true)
end

local asking -- the id of the plan the popup last asked about

-- The x on a tab: asks whether to archive the plan or remove it.
function Plans.AskClose(id)
	local p = Find(id)
	if not p then return end
	if not (StaticPopupDialogs and StaticPopup_Show) then return Plans.Archive(id) end
	StaticPopupDialogs.WOWMCP_CLOSE_PLAN = StaticPopupDialogs.WOWMCP_CLOSE_PLAN or {
		text = "Close the plan \"%s\"?\n\nAn archived plan keeps its progress; the box at the end of the tabs brings it back.",
		button1 = "Archive", button2 = CANCEL or "Cancel", button3 = "Remove",
		OnAccept = function(_, data) Plans.Archive(data) end,
		OnAlt = function(_, data) Plans.Remove(data) end,
		timeout = 0, whileDead = true, hideOnEscape = true,
	}
	asking = id
	StaticPopup_Show("WOWMCP_CLOSE_PLAN", TabName(p), nil, id)
end

-- The archive button: shows or hides the list of archived plans.
function Plans.ToggleArchive()
	if not window then return end
	window.shelfOpen = not window.shelfOpen or nil
	Layout()
end

function Plans.ListArchived()
	local archived = Archived()
	if #archived == 0 then
		ns.Print("No archived plans. The x on a plan's tab, or /mcp plan archive, archives one.")
		return
	end
	WindowSaved().shelfHidden = nil
	Layout()
	for i, p in ipairs(archived) do
		ns.Print(string.format("%d. |cffffffff%s|r (%s): %s", i, ns.Escape(TabName(p)), ns.Escape(KindText(p)),
			ns.Escape(p.title or "Plan")))
	end
	ns.Print("/mcp plan restore <number or name> brings one back.")
end

-- Brings an archived plan back (by its number in the archive, its name, or its id) and opens it.
function Plans.Restore(which, id)
	local found
	for i, p in ipairs(Archived()) do
		local match
		if id then
			match = p.id == id
		else
			match = i == tonumber(which) or (type(which) == "string" and Same(TabName(p), which))
		end
		if match then
			found = p
			break
		end
	end
	if not found then
		ns.Print("No such archived plan. /mcp plan archived lists them.")
		return
	end
	StateOf(found).archived = nil
	WindowSaved().shelfHidden = nil
	if window then window.shelfOpen = nil end
	Plans.Open(nil, found.id, true)
end

-- Tabs: open another plan (by its tab's number or name), list them, name the open one.
local Open

function Plans.Open(which, id, again)
	local found
	for i, p in ipairs(Tabs()) do
		local match
		if id then
			match = p.id == id
		else
			match = i == tonumber(which) or (type(which) == "string" and Same(TabName(p), which))
		end
		if match then
			found = p
			break
		end
	end
	if not found then
		ns.Print("No such plan. /mcp plan tabs lists them.")
		return
	end
	local state = StateOf(found)
	if plan and found.id == plan.id and not state.hidden and not again then return end
	Open(found)
	state.hidden = nil
	first = nil
	local ok, err = pcall(Evaluate, true) -- what got done while another tab was open ticks off quietly
	if not ok then Report(err) end
	Guide(true)
	Layout()
	ns.Print(string.format("Plan: |cffffffff%s|r (%s).", ns.Escape(TabName(found)),
		state.current and string.format("step %d/%d", state.current, #steps) or "complete"))
end

function Plans.ListTabs()
	local tabs = Tabs()
	if #tabs == 0 then
		ns.Print("No plan. Ask your AI for one!")
		return
	end
	for i, p in ipairs(tabs) do
		local open = plan ~= nil and p.id == plan.id
		ns.Print(string.format("%s%d. |cffffffff%s|r (%s): %s", open and "|cff33ff33>|r " or "", i, ns.Escape(TabName(p)),
			ns.Escape(KindText(p)), ns.Escape(p.title or "Plan")))
	end
	ns.Print("/mcp plan tab <number or name> shows another; /mcp plan name <text> names the one showing.")
end

function Plans.Rename(name)
	if NoPlan() then return end
	name = strtrim(tostring(name or "")):sub(1, TAB_NAME)
	State().name = name ~= "" and name or nil
	Layout()
	ns.Print(string.format("This plan's tab is now called |cffffffff%s|r.", ns.Escape(TabName(plan))))
end

---------------------------------------------------------------------------
-- Payload
---------------------------------------------------------------------------

-- The NPCs a step names: { { id, name, display } }, without what isn't one.
local function ParseNpcs(raw)
	local list = {}
	for _, r in ipairs(type(raw) == "table" and raw or {}) do
		local id = type(r) == "table" and tonumber(r.id)
		if id and id > 0 then
			list[#list + 1] = {
				id = math.floor(id), display = tonumber(r.display),
				name = type(r.name) == "string" and r.name ~= "" and r.name or nil,
			}
		end
	end
	return list
end

local function ParseStep(raw, n, missing)
	local step = {
		n = n, text = tostring(raw.text or ("Step " .. n)), loop = raw.loop == true, conditions = {},
		places = type(raw.places) == "table" and raw.places or {},
		areas = type(raw.areas) == "table" and raw.areas or {},
		npcs = ParseNpcs(raw.npcs),
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

-- Makes a plan the open one: the plan the checks, the window and the navigator work with.
-- Returns the zones of its places that the map doesn't know.
function Open(p)
	WowMCPDB.plan = StateOf(p)
	plan = p
	wipe(steps)
	wipe(wanted)
	local missing = {}
	for _, raw in ipairs(type(p.steps) == "table" and p.steps or {}) do
		if type(raw) == "table" then table.insert(steps, ParseStep(raw, #steps + 1, missing)) end
	end
	return missing
end

-- The payload has every plan: { id, list = { plan, ... } }; before there were tabs it was one
-- plan, { id, title, steps }. A plan that wasn't there before is announced and opened. The ones
-- we've already announced (e.g. restored by a /reload, or sent again with another feature's
-- update) are picked up where they were, quietly, with the same one open.
function Plans.Receive(payload)
	local states = States()
	local was = WowMCPDB.plan -- the open plan's progress, as saved
	if type(was) == "table" and was.id then states[was.id] = was end
	local seen = WowMCPDB.planSeen or (type(was) == "table" and was.id)
	WowMCPDB.planSeen = payload.id

	local list = {}
	for _, p in ipairs(type(payload.list) == "table" and payload.list or { payload }) do
		if type(p) == "table" and p.id and type(p.steps) == "table" and #p.steps > 0 then list[#list + 1] = p end
	end
	local newest, listed = nil, {}
	for _, p in ipairs(list) do
		listed[p.id] = true
		if not states[p.id] and (not newest or p.id > newest.id) then newest = p end
	end
	for id in pairs(states) do
		if not listed[id] then states[id] = nil end -- plans the AI replaced or removed
	end
	plans = list
	for _, p in ipairs(list) do StateOf(p) end
	-- The plan the player is being asked about is gone: so is the question.
	if asking and not listed[asking] then
		if StaticPopup_Hide then pcall(StaticPopup_Hide, "WOWMCP_CLOSE_PLAN") end
		asking = nil
	end

	-- A new plan opens; else the one that was open stays, or the first that has a tab.
	local open = newest
	local wasOpen = plan and plan.id or (type(was) == "table" and was.id)
	for _, p in ipairs(not open and list or {}) do
		if p.id == wasOpen then open = p end
	end
	if not open or (not newest and (states[open.id].cleared or states[open.id].archived)) then open = Tabs()[1] or open end
	if not open then
		plan = nil
		wipe(steps)
		wipe(wanted)
		WowMCPDB.plan = { id = payload.id, done = {}, undone = {} }
		Guide()
		Layout()
		if seen ~= payload.id then ns.Print("Plan cleared.") end
		return
	end

	local isNew = open == newest
	local missing = Open(open)
	local state = State()
	if not Active() then
		Guide()
		Layout()
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

-- For get_plan_progress: every plan's tab, with its progress as of when it was last open. `tab` is
-- the tab's name as the AI sent it (what its tools know the plan by), `name` the player's own for it.
local function TabList()
	local list = {}
	for _, p in ipairs(plans) do
		local state = StateOf(p)
		local done = 0
		for _ in pairs(state.done) do done = done + 1 end
		if not state.cleared then
			list[#list + 1] = {
				id = p.id, tab = SentTab(p), name = state.name, kind = (KindOf(p)), title = p.title,
				steps = #p.steps, done = done,
				complete = state.complete or false, archived = state.archived or nil,
				shown = plan ~= nil and p.id == plan.id and not state.hidden and not state.archived,
			}
		end
	end
	return list
end

-- For get_plan_progress: the ids of the plans the player removed, so the server stops counting
-- and sending them.
local function Removed()
	local list = {}
	for _, p in ipairs(plans) do
		if StateOf(p).cleared then list[#list + 1] = p.id end
	end
	return #list > 0 and list or nil
end

-- For get_plan_progress: each step's state and the live progress of the ones not done. `seen` is
-- the id of the last payload received, so the server can tell when its newest hasn't arrived.
local function Progress()
	if not Active() then
		-- No plan open: the player archived what's left, or removed it.
		local archived = #Archived() > 0 or nil
		return {
			none = true, cleared = not archived and plan ~= nil and #steps > 0 or nil, archived = archived,
			tabs = TabList(), removed = Removed(), seen = WowMCPDB.planSeen,
		}
	end
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
		tab = SentTab(plan), name = state.name, kind = (KindOf(plan)), tabs = TabList(),
		removed = Removed(), seen = WowMCPDB.planSeen,
	}
end

---------------------------------------------------------------------------

local SUBCOMMANDS = {
	[""] = Plans.List, list = Plans.List, show = Plans.Show, hide = Plans.Hide, next = Plans.Next, prev = Plans.Prev,
	reset = Plans.Reset, clear = Plans.Clear, tabs = Plans.ListTabs,
	archive = function() Plans.Archive() end, archived = Plans.ListArchived, restore = Plans.ListArchived,
}

local USAGE = "/mcp plan [show, hide, next, prev, <n>, reset, clear, tabs, tab <n or name>, name <text>, archive, "
	.. "archived, restore <n or name>, size <width> [steps]] - the plan from your AI: list its steps, show or hide "
	.. "its checklist, tick off the current step, go back, go to step n, start over, remove it; with several plans: "
	.. "list their tabs, show another, name the tab of the one showing; archive the one showing, list the archived "
	.. "ones, bring one back; the window's size (or drag its corner)"

function Plans.Command(rest)
	local typed = strtrim(tostring(rest or ""))
	rest = typed:lower()
	local run = SUBCOMMANDS[rest]
	local word, more = typed:match("^(%a+)%s+(.+)$")
	word = word and word:lower()
	if word == "tab" then
		Plans.Open(tonumber(more) or more)
	elseif word == "name" then
		Plans.Rename(more)
	elseif word == "restore" then
		Plans.Restore(tonumber(more) or more)
	elseif word == "size" then
		local width, rows = more:match("^(%d+)%s*(%d*)$")
		if more:lower() == "reset" then
			Plans.Resize("reset")
		elseif width then
			Plans.Resize(tonumber(width), tonumber(rows))
		else
			ns.Print("/mcp plan size <width> [steps shown], e.g. /mcp plan size 400 12; /mcp plan size reset")
			return
		end
		ns.Print(string.format("The plan window is %d wide and shows up to %d steps.", Width(), MaxRows()))
	elseif run then
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

---------------------------------------------------------------------------
-- For Goals.lua and Auction.lua: goals' targets are conditions checked by the code above, a step
-- can wait for a goal ({ goal = "<id>" }), and the items the plan still needs.
---------------------------------------------------------------------------

table.insert(KINDS, "goal")

function CHECKS.goal(c)
	if not ns.Goals then return false, "Goal " .. tostring(c.value) .. " (restart the game to load goals)" end
	return ns.Goals.Status(c.value)
end

Plans.ParseCondition = ParseCondition -- raw condition -> parsed, for CheckCondition
Plans.Progress = Progress -- what get_plan_progress is told

-- Whether a parsed condition holds, and its progress line (position conditions count as current).
function Plans.CheckCondition(c)
	return CHECKS[c.kind](c, true)
end

-- The "have" items of the steps not done yet that the player still lacks: { { item, need, name } }.
function Plans.Items()
	local list = {}
	if not Active() then return list end
	local done = State().done
	for _, step in ipairs(steps) do
		for _, c in ipairs(done[step.n] and {} or step.conditions) do
			local need = c.kind == "have" and c.count - math.floor(ItemCount(c.value, c.bank)) or 0
			if need > 0 then list[#list + 1] = { item = c.value, need = need, name = c.name } end
		end
	end
	return list
end
