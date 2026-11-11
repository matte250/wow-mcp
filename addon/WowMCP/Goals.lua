-- WowMCP goals: long-running goals from the AI (payload key "goals", set_goals tool), e.g. "6 Medium
-- Hide" or "Leatherworking 125". There's one guided plan at a time, but any number of goals, each
-- tracking itself in the background and pointing out chances to make progress:
--   * entering a zone or area where it's found ("Beasts here skin into Medium Hide (2/6)"),
--   * a line on the tooltip of the mobs the AI listed ("Skin for Medium Hide: goal 2/6"),
--   * a vendor or the auction house selling what a goal (or the plan) still needs,
--   * items as they come in ("Medium Hide 4/6"), and completion with a sound.
--
-- A goal's target is a plan condition ({ have = 4232, count = 6 }, { skill = ..., rank = ... }),
-- checked by Plans.lua's own code, and a plan step can wait for a goal with { goal = "<id>" }.
-- Done stays done, like plan steps: crafting uses up the hides, the goal stays ticked.
--
-- Noise: each goal pings at most once per visit to a zone (an area counts as its zone), merchant
-- window or auction house; nothing in combat (queued for after it, dropped if stale by then) or
-- in instances; muted goals stay quiet. Done, muted and cleared goals and the window are kept in
-- WowMCPDB, so a /reload changes nothing.

local _, ns = ...

local Goals = {}
ns.Goals = Goals

local CHECK_INTERVAL = 1  -- seconds; positions have no events
local BATCH = 1           -- changes this close together go in one chat line
local STALE = 60          -- a chance pointed out during a fight is dropped if the fight took longer
local STALE_PROGRESS = 300
local MAX_ROWS = 8
local WIDTH, PAD, ROW, HEADER, DOCK_GAP, PROGRESS_WIDTH = 300, 8, 16, 18, 2, 64
local SOUNDS = { done = { "UI_AUTO_QUEST_COMPLETE", 23404 }, ping = { "MAP_PING", 3175 } } -- SOUNDKIT names, IDs
local TICK = "|TInterface\\Buttons\\UI-CheckBox-Check:0|t"
local GREEN, GREY, WHITE, TODO, BLUE = "|cff33ff33", "|cff808080", "|cffffffff", "|cffffcc66", "|cff66ccff"
local PREFIX = BLUE .. "Goal:|r "

local goals = {}  -- parsed, in payload order: { id, rev, title, note, target, item, zones, areas, mobs, met, text, have, of }
local byId = {}
local received    -- the payload being shown
local pending = { done = {}, progress = {}, pings = {} } -- what to tell the player at the next flush
local scheduled, checking
local merchant    -- the merchant window open now: { visit }
local merchantVisits = 0
local problem, reported
local window

-- WowMCPDB.goals: { id = payload announced, state = { [goal id] = { rev, done = unix time, muted,
-- cleared } }, zone = this zone visit, pinged = { [goal id] = true } during it }. A goal the AI
-- changed (another rev) starts over, but stays muted.
local function DB()
	local db = WowMCPDB.goals or {}
	WowMCPDB.goals = db
	db.state = db.state or {}
	db.pinged = db.pinged or {}
	return db
end

local function Entry(goal)
	return DB().state[goal.id]
end

local function Report(err)
	problem = tostring(err)
	if problem ~= reported then
		reported = problem
		ns.Print("|cffff6060Goal error:|r " .. ns.Escape(problem))
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

local function ItemName(itemID, given)
	local ok, name = pcall(C_Item.GetItemNameByID, itemID)
	return ok and type(name) == "string" and name or given
end

-- Item markup in the AI's text ({item:4232}) is Core.lua's when it has ns.ItemText (ns.Print then
-- shows it in chat itself); until then the item's name stands in.
local function Names(escaped)
	return (escaped:gsub("{item:(%d+):?([^{}|\n]*)}", function(id, given)
		return ns.Escape(ItemName(tonumber(id), given ~= "" and given or nil) or ("item " .. id))
	end))
end

local function ChatText(text)
	local escaped = ns.Escape(text)
	return ns.ItemText and escaped or Names(escaped)
end

local function FrameText(text)
	if ns.ItemText then return ns.ItemText(text) end
	return Names(ns.Escape(text))
end

-- An item in the addon's own lines; given: a name to show while the client is still loading it.
local function ItemRef(itemID, forChat, given)
	if ns.ItemText then
		local name = given and tostring(given):gsub("[{}|\n]", "") or ""
		local token = "{item:" .. itemID .. (name ~= "" and (":" .. name) or "") .. "}"
		if forChat then return token end
		return ns.RenderItems and ns.RenderItems(token) or ns.ItemText(token)
	end
	return ns.Escape(ItemName(itemID, given) or ("item " .. itemID))
end

local function Money(copper)
	copper = math.floor((tonumber(copper) or 0) + 0.5)
	local g, s, c = math.floor(copper / 10000), math.floor(copper / 100) % 100, copper % 100
	local parts = {}
	if g > 0 then parts[#parts + 1] = g .. "|cffffd700g|r" end
	if s > 0 then parts[#parts + 1] = s .. "|cffc7c7cfs|r" end
	if c > 0 or #parts == 0 then parts[#parts + 1] = c .. "|cffeda55fc|r" end
	return table.concat(parts, " ")
end
Goals.Money = Money

local function Instance()
	local ok, inside = pcall(IsInInstance)
	return ok and inside and true or false
end

-- Nothing is said in a fight or a dungeon; it waits (see Flush).
local function Quiet()
	return InCombatLockdown() or Instance()
end

---------------------------------------------------------------------------
-- Progress: the target checked by Plans.lua, and its count when it has one ("Medium Hide 2/6").
---------------------------------------------------------------------------

local function Counts(text)
	local have, of = tostring(text or ""):match("(%d+)/(%d+)")
	return tonumber(have), tonumber(of)
end

local function CheckGoal(goal)
	local ok, met, text = pcall(ns.Plans.CheckCondition, goal.target)
	if not ok then
		Report(met) -- e.g. a secret value; the other goals go on
		met, text = false, "Couldn't check this here"
	end
	goal.met, goal.text = met and true or false, tostring(text or "")
	goal.have, goal.of = Counts(goal.text)
end

local function Need(goal)
	if goal.text == nil then CheckGoal(goal) end
	return goal.have and goal.of and math.max(0, goal.of - goal.have) or nil
end

local function Short(goal)
	if goal.have and goal.of then return goal.have .. "/" .. goal.of end
	return ns.Escape(goal.text or "")
end

-- Goals that point things out: not done, muted or cleared.
local function Pingable(goal)
	local entry = Entry(goal)
	return entry ~= nil and not (entry.done or entry.muted or entry.cleared)
end

-- Goals in the window and the list: all but the ones cleared in game.
local function Listed()
	local list = {}
	for _, goal in ipairs(goals) do
		local entry = Entry(goal)
		if entry and not entry.cleared then list[#list + 1] = goal end
	end
	return list
end

---------------------------------------------------------------------------
-- Telling the player: queued, then said together, unless in a fight or a dungeon.
---------------------------------------------------------------------------

local Flush

local function Soon()
	if scheduled then return end
	scheduled = true
	C_Timer.After(BATCH, function()
		scheduled = false
		Flush()
	end)
end

-- still(): whether the chance is still there after a fight (still in the area, at the vendor).
local function Queue(text, still)
	table.insert(pending.pings, { text = text, at = GetTime(), still = still })
	Soon()
end

function Flush()
	if not (#pending.done > 0 or next(pending.progress) or #pending.pings > 0) then return end
	if Quiet() then
		pending.waiting = true -- PLAYER_REGEN_ENABLED, leaving the instance or the ticker try again
		return
	end
	pending.waiting = nil
	local now, sound = GetTime(), nil
	-- Completions matter most and are never stale.
	for _, goal in ipairs(pending.done) do
		ns.Print(GREEN .. "Goal complete:|r " .. ChatText(goal.title))
		local entry = Entry(goal)
		if not (entry and entry.muted) then sound = SOUNDS.done end
	end
	local parts = {}
	for _, goal in ipairs(goals) do
		local at = pending.progress[goal.id]
		if at and now - at <= STALE_PROGRESS and Pingable(goal) then parts[#parts + 1] = ns.Escape(goal.text) end
	end
	if #parts > 0 then ns.Print((#parts > 1 and BLUE .. "Goals:|r " or PREFIX) .. table.concat(parts, ", ")) end
	for _, ping in ipairs(pending.pings) do
		local ok, still = pcall(function() return not ping.still or ping.still() end)
		if now - ping.at <= STALE and ok and still then
			ns.Print(ping.text)
			sound = sound or SOUNDS.ping
		end
	end
	pending = { done = {}, progress = {}, pings = {} }
	if sound then Play(sound) end
end

---------------------------------------------------------------------------
-- Chances: zones and areas, vendors, the auction house
---------------------------------------------------------------------------

local widths = {}

-- A map's width in yards, to turn an area's radius (map percent) into yards (as Areas.lua does).
local function Width(mapID)
	if widths[mapID] == nil then
		local ok1, c1, left = pcall(C_Map.GetWorldPosFromMapPos, mapID, CreateVector2D(0, 0.5))
		local ok2, c2, right = pcall(C_Map.GetWorldPosFromMapPos, mapID, CreateVector2D(1, 0.5))
		widths[mapID] = false
		if ok1 and ok2 and c1 and c1 == c2 and left and right then
			widths[mapID] = math.sqrt((right.x - left.x) ^ 2 + (right.y - left.y) ^ 2)
		end
	end
	return widths[mapID] or nil
end

local function PlayerMap()
	local ok, mapID = pcall(C_Map.GetBestMapForUnit, "player")
	return ok and mapID or nil
end

-- In the zone by name, or on its map or a map inside it (a city in a zone).
local function InZone(z)
	if Same(GetRealZoneText(), z.zone) or Same(GetZoneText(), z.zone) then return true end
	local mapID = z.mapID and PlayerMap()
	for _ = 1, 10 do
		if not mapID or mapID == 0 then return false end
		if mapID == z.mapID then return true end
		local info = C_Map.GetMapInfo(mapID)
		mapID = info and info.parentMapID
	end
	return false
end

local function InArea(a)
	local yards = a.yards and ns.Map.Distance(a)
	return yards ~= nil and yards <= a.yards
end

-- What to point out here for a goal, and how to tell it's still true later. A zone with areas of
-- the goal waits for the player to reach one, which says more.
local function Opportunity(goal)
	local mapID = PlayerMap()
	local areaHere = false
	for _, a in ipairs(goal.areas) do
		if mapID and ns.Map.Project(a, mapID) then
			areaHere = true
			if InArea(a) then return ChatText(a.label), function() return InArea(a) end end
		end
	end
	if areaHere then return end
	for _, z in ipairs(goal.zones) do
		if InZone(z) then return ChatText(z.note or goal.note or goal.title), function() return InZone(z) end end
	end
end

local function CheckPlaces()
	local db = DB()
	local zone = Normalize(GetRealZoneText())
	if zone == "" then return end -- a loading screen
	if zone ~= db.zone then
		db.zone = zone
		wipe(db.pinged)
	end
	if Instance() then return end
	for _, goal in ipairs(goals) do
		if Pingable(goal) and not db.pinged[goal.id] then
			local text, still = Opportunity(goal)
			if text then
				db.pinged[goal.id] = true
				Queue(PREFIX .. text .. " " .. GREY .. "(" .. Short(goal) .. ")|r", still)
			end
		end
	end
end

local function AtMerchant(visit)
	return function() return merchant ~= nil and merchant.visit == visit end
end

-- What the merchant sells that a goal or the plan still needs, by item ID.
local function CheckMerchant()
	if not merchant or Instance() then return end
	local wanted = {} -- item ID -> { need, whose, name }
	for _, goal in ipairs(goals) do
		local need = goal.item and Pingable(goal) and Need(goal)
		if need and need > 0 and not wanted[goal.item] then
			wanted[goal.item] = { need = need, whose = "goal", name = goal.target.name }
		end
	end
	for _, w in ipairs(ns.Plans.Items()) do
		if not wanted[w.item] then wanted[w.item] = { need = w.need, whose = "plan", name = w.name } end
	end
	local vendor = ns.Escape(UnitName("npc") or "This vendor")
	local seen = {}
	for i = 1, GetMerchantNumItems() do
		local itemID = GetMerchantItemID(i)
		local want = itemID and not seen[itemID] and wanted[itemID]
		if want then
			seen[itemID] = true
			-- The merchant's own name for it while the client is still loading the item.
			local ok, info = pcall(C_MerchantFrame.GetItemInfo, i)
			local name = ok and type(info) == "table" and info.name or want.name
			Queue(string.format("%s sells %s, your %s needs %d.", vendor, ItemRef(itemID, true, name), want.whose, want.need),
				AtMerchant(merchant.visit))
		end
	end
end

local function PriceText(itemID, need, price, name)
	local text = string.format("%s %d listed from %s", ItemRef(itemID, true, name), price.listed, Money(price.price))
	local cost, got = ns.Auction.Cost(price, need)
	if got >= need then text = text .. string.format(", %d for %s", need, Money(cost)) end
	return text
end

-- Auction.lua calls this once per auction house visit, when it has the prices of what the goals and
-- the plan need: item ID -> cached price ({ price, listed, tiers }).
function Goals.AuctionPrices(prices)
	if Instance() then return end
	local seen, mine, plan = {}, {}, {}
	for _, goal in ipairs(goals) do
		local need = goal.item and Pingable(goal) and Need(goal)
		local price = need and need > 0 and not seen[goal.item] and prices[goal.item]
		if price and (price.listed or 0) > 0 and price.price then
			seen[goal.item] = true
			mine[#mine + 1] = PriceText(goal.item, need, price, goal.target.name)
		end
	end
	for _, w in ipairs(ns.Plans.Items()) do
		local price = not seen[w.item] and prices[w.item]
		if price and (price.listed or 0) > 0 and price.price then
			seen[w.item] = true
			plan[#plan + 1] = PriceText(w.item, w.need, price, w.name)
		end
	end
	local still = function() return ns.Auction.IsOpen() end
	if #mine > 0 then
		Queue(string.format("%d of your goal items %s listed here: %s.", #mine, #mine > 1 and "are" or "is",
			table.concat(mine, "; ")), still)
	end
	if #plan > 0 then Queue("For your plan: " .. table.concat(plan, "; ") .. ".", still) end
end

---------------------------------------------------------------------------
-- Mob tooltips: hovering a mob a goal lists (by name or NPC ID) adds a line.
---------------------------------------------------------------------------

local function NpcID(guid)
	return type(guid) == "string" and tonumber(guid:match("^%a+%-.-%-.-%-.-%-.-%-(%d+)%-")) or nil
end

local function MobName(data)
	local line = type(data.lines) == "table" and data.lines[1]
	local name = type(line) == "table" and line.leftText or nil
	if type(name) ~= "string" and data.guid and UnitTokenFromGUID then
		local unit = UnitTokenFromGUID(data.guid)
		name = unit and UnitName(unit)
	end
	return name
end

local function MobLine(goal, mob)
	local what = goal.item and ItemRef(goal.item, false, goal.target.name) or FrameText(goal.title)
	if mob.note then
		local note = FrameText(mob.note):gsub("^%l", string.upper)
		return string.format("%s%s for %s:|r goal %s", BLUE, note, what, Short(goal))
	end
	return string.format("%sGoal:|r %s %s", BLUE, FrameText(goal.title), Short(goal))
end

local function AddMobLines(tooltip, data)
	if #goals == 0 or type(data) ~= "table" then return end
	-- pcall: in restricted content a unit's name or GUID can be a secret value.
	pcall(function()
		local name = MobName(data)
		local key = type(name) == "string" and Normalize(name) or nil
		local npc = NpcID(data.guid)
		for _, goal in ipairs(goals) do
			if Pingable(goal) then
				for _, mob in ipairs(goal.mobs) do
					if (npc and mob.npc == npc) or (key and key ~= "" and mob.key == key) then
						tooltip:AddLine(MobLine(goal, mob), 0.4, 0.8, 1)
						break
					end
				end
			end
		end
	end)
end

---------------------------------------------------------------------------
-- Checking
---------------------------------------------------------------------------

local Layout

local function Evaluate()
	local finished = false
	for _, goal in ipairs(goals) do
		local entry = Entry(goal)
		if entry and not entry.cleared then
			local before = goal.have
			CheckGoal(goal)
			if not entry.done and goal.met then
				entry.done = time()
				finished = true
				table.insert(pending.done, goal)
				Soon()
			elseif goal.item and Pingable(goal) and before and goal.have and goal.have > before then
				-- Items only: the game itself announces skill-ups and quest progress.
				pending.progress[goal.id] = pending.progress[goal.id] or GetTime()
				Soon()
			end
		end
	end
	-- A plan step waiting for this goal ticks off (not from inside the plan's own check).
	if finished then C_Timer.After(0, function() ns.Plans.Check() end) end
	Layout()
end

function Goals.Check()
	if checking then return end
	checking = true
	local ok, err = pcall(Evaluate)
	checking = false
	if not ok then Report(err) end
end

local function Tick()
	Goals.Check()
	local ok, err = pcall(CheckPlaces)
	if not ok then Report(err) end
	if pending.waiting then Flush() end
end

-- For Plans.lua's { goal = id } condition: done (sticky) and a line of progress.
function Goals.Status(id)
	if not received and ns.run.payload and type(ns.run.payload.goals) == "table" then
		Goals.Receive(ns.run.payload.goals) -- the plan's payload can come first
	end
	local goal = byId[id]
	if not goal then return false, "Goal " .. tostring(id) .. " (not set; ask your AI to set it)" end
	local entry = Entry(goal)
	if entry.done then return true, "Goal done: " .. goal.title end
	if entry.cleared then return false, "Goal removed: " .. goal.title end
	if goal.text == nil then CheckGoal(goal) end
	return false, "Goal: " .. goal.title .. " - " .. goal.text
end

-- For Auction.lua: what the goals still need, { { item, need, goal } }; muted goals too.
function Goals.Items()
	local list = {}
	for _, goal in ipairs(goals) do
		local entry = Entry(goal)
		if goal.item and entry and not (entry.done or entry.cleared) then
			local need = Need(goal)
			if need and need > 0 then list[#list + 1] = { item = goal.item, need = need, goal = goal.id } end
		end
	end
	return list
end

---------------------------------------------------------------------------
-- The window: a compact list under the plan window, or on its own.
---------------------------------------------------------------------------

local function WindowSaved()
	WowMCPDB.goalsWindow = WowMCPDB.goalsWindow or {}
	return WowMCPDB.goalsWindow
end

local function PlanWindow()
	local plan = WowMCPPlanFrame
	return plan and plan:IsShown() and plan or nil
end

-- Docked under the plan window while that's shown (dragging moves the plan window), else where the
-- player put it.
local function Anchor(f)
	local plan = PlanWindow()
	if f.anchored and f.dockedTo == plan then return end
	f:ClearAllPoints()
	if plan then
		f:SetPoint("TOPLEFT", plan, "BOTTOMLEFT", 0, -DOCK_GAP)
	else
		local saved = WindowSaved()
		if saved.point then
			f:SetPoint(saved.point, UIParent, saved.relativePoint, saved.x, saved.y)
		else
			f:SetPoint("TOPLEFT", UIParent, "TOPLEFT", 24, -150)
		end
	end
	f.anchored, f.dockedTo = true, plan
end

local function SavePosition(f)
	local point, _, relativePoint, x, y = f:GetPoint(1)
	local saved = WindowSaved()
	saved.point, saved.relativePoint, saved.x, saved.y = point, relativePoint, x, y
end

local function Line(text, r, g, b)
	GameTooltip:AddLine(text, r or 0.8, g or 0.8, b or 0.8, true)
end

local function RowTooltip(row)
	row.Hover:Show()
	local goal = row.goal
	local entry = goal and Entry(goal)
	if not entry then return end
	GameTooltip:SetOwner(row, "ANCHOR_RIGHT")
	GameTooltip:SetText(FrameText(goal.title), 1, 0.82, 0, 1, true)
	if entry.done then
		Line("Done " .. date("%H:%M", entry.done) .. ". /mcp goals clear removes it.", 0.2, 1, 0.2)
	else
		Line(ns.Escape(goal.text or ""), goal.met and 0.2 or 1, goal.met and 1 or 0.8, goal.met and 0.2 or 0.4)
	end
	if goal.note then Line(FrameText(goal.note), 1, 1, 1) end
	for _, z in ipairs(goal.zones) do Line("In " .. ns.Escape(z.zone) .. (z.note and (": " .. FrameText(z.note)) or "")) end
	for _, a in ipairs(goal.areas) do
		Line(string.format("%s - %s %.1f, %.1f", FrameText(a.label), ns.Escape(a.zone), a.x * 100, a.y * 100))
	end
	for _, mob in ipairs(goal.mobs) do
		Line(ns.Escape(mob.name or ("NPC " .. tostring(mob.npc))) .. (mob.note and (" (" .. FrameText(mob.note) .. ")") or ""))
	end
	local price = goal.item and ns.Auction and ns.Auction.Price(goal.item)
	if price and price.price then
		Line(string.format("Auction house: %s each, %d listed (%s)", Money(price.price), price.listed or 0,
			date("%d %b %H:%M", price.at)))
	end
	if goal.item then Line("Vendors and the auction house that sell it are pointed out.", 0.6, 0.6, 0.6) end
	if entry.muted then Line("Muted: it doesn't point anything out.", 0.6, 0.6, 0.6) end
	Line(entry.muted and "Right-click: unmute" or "Right-click: mute", 0.4, 0.8, 1)
	GameTooltip:Show()
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

local function CreateRow(f)
	local row = CreateFrame("Frame", nil, f)
	row:EnableMouse(true)
	row:SetSize(WIDTH - 2 * PAD, ROW)
	row.Hover = row:CreateTexture(nil, "BACKGROUND")
	row.Hover:SetAllPoints()
	row.Hover:SetColorTexture(1, 1, 1, 0.07)
	row.Hover:Hide()
	row.Check = row:CreateTexture(nil, "OVERLAY")
	row.Check:SetSize(14, 14)
	row.Check:SetPoint("LEFT", row, "LEFT", -2, 0)
	row.Check:SetTexture("Interface\\Buttons\\UI-CheckBox-Check")
	row.Text = row:CreateFontString(nil, "ARTWORK", "GameFontHighlightSmall")
	row.Text:SetPoint("LEFT", row, "LEFT", 14, 0)
	row.Text:SetWidth(WIDTH - 2 * PAD - 14 - PROGRESS_WIDTH)
	row.Text:SetJustifyH("LEFT")
	row.Text:SetWordWrap(false)
	row.Progress = row:CreateFontString(nil, "ARTWORK", "GameFontHighlightSmall")
	row.Progress:SetPoint("RIGHT", row, "RIGHT", 0, 0)
	row.Progress:SetWidth(PROGRESS_WIDTH)
	row.Progress:SetJustifyH("RIGHT")
	row.Progress:SetWordWrap(false)
	row:SetScript("OnMouseUp", function(self, button)
		if button == "RightButton" and self.goal then Goals.Mute(self.goal.id, not Entry(self.goal).muted) end
	end)
	row:SetScript("OnEnter", RowTooltip)
	row:SetScript("OnLeave", function(self)
		self.Hover:Hide()
		GameTooltip:Hide()
	end)
	return row
end

local function CreateWindow()
	local f = CreateFrame("Frame", "WowMCPGoalsFrame", UIParent)
	f:SetSize(WIDTH, HEADER + PAD)
	f:SetFrameStrata("MEDIUM")
	f:SetClampedToScreen(true)
	f:SetMovable(true)
	f:SetDontSavePosition(true) -- kept in WowMCPDB, with the docking
	f:EnableMouse(true)
	f:RegisterForDrag("LeftButton")
	local bg = f:CreateTexture(nil, "BACKGROUND")
	bg:SetAllPoints()
	bg:SetColorTexture(0, 0, 0, 0.55)

	f.Close = HeaderButton(f, 20, "UIPanelCloseButtonNoScripts", function() Goals.Hide() end,
		function() return "Hide the goals (/mcp goals show brings them back)" end)
	f.Close:SetPoint("TOPRIGHT", f, "TOPRIGHT", -2, 0)
	f.Collapse = HeaderButton(f, 14, nil, function() Goals.Collapse() end,
		function() return WindowSaved().collapsed and "Show the goals" or "Show only the header" end)
	f.Collapse:SetHighlightTexture("Interface\\Buttons\\UI-PlusButton-Hilight", "ADD")
	f.Collapse:SetPoint("RIGHT", f.Close, "LEFT", -2, 0)
	f.Count = f:CreateFontString(nil, "ARTWORK", "GameFontHighlightSmall")
	f.Count:SetPoint("RIGHT", f.Collapse, "LEFT", -5, 0)
	f.Title = f:CreateFontString(nil, "ARTWORK", "GameFontNormal")
	f.Title:SetPoint("TOPLEFT", f, "TOPLEFT", PAD, -PAD + 4)
	f.Title:SetText("Goals")
	f.More = f:CreateFontString(nil, "ARTWORK", "GameFontDisableSmall")
	f.Rows = {}

	f:SetScript("OnDragStart", function(self)
		if self.dockedTo then self.dockedTo:StartMoving() else self:StartMoving() end
	end)
	f:SetScript("OnDragStop", function(self)
		local plan = self.dockedTo
		if plan then
			-- The plan window moved; it saves its own position.
			local stop = plan:GetScript("OnDragStop")
			if stop then stop(plan) else plan:StopMovingOrSizing() end
		else
			self:StopMovingOrSizing()
			SavePosition(self)
		end
	end)
	return f
end

local function FillRow(row, goal)
	local entry = Entry(goal)
	row.goal = goal
	row.Check:SetShown(entry.done ~= nil)
	local dim = entry.done or entry.muted
	row.Text:SetText((dim and GREY or WHITE) .. FrameText(goal.title) .. "|r"
		.. (entry.muted and not entry.done and (GREY .. " (muted)|r") or ""))
	if entry.done then
		row.Progress:SetText(GREEN .. "Done|r")
	elseif goal.have and goal.of then
		row.Progress:SetText((dim and GREY or TODO) .. Short(goal) .. "|r")
	else
		row.Progress:SetText(GREY .. "to do|r") -- the line is in the tooltip
	end
end

function Layout()
	local list = Listed()
	local saved = WindowSaved()
	if #list == 0 or saved.hidden then
		if window then window:Hide() end
		return
	end
	window = window or CreateWindow()
	local f = window
	Anchor(f)
	f:Show()
	local done = 0
	for _, goal in ipairs(list) do
		if Entry(goal).done then done = done + 1 end
	end
	f.Count:SetText(string.format("%d/%d done", done, #list))
	f.Collapse:SetNormalTexture(saved.collapsed and "Interface\\Buttons\\UI-PlusButton-Up"
		or "Interface\\Buttons\\UI-MinusButton-Up")
	for _, row in ipairs(f.Rows) do row:Hide() end
	local y = PAD + HEADER
	local shown = saved.collapsed and 0 or math.min(#list, MAX_ROWS)
	for i = 1, shown do
		local row = f.Rows[i] or CreateRow(f)
		f.Rows[i] = row
		FillRow(row, list[i])
		row:ClearAllPoints()
		row:SetPoint("TOPLEFT", f, "TOPLEFT", PAD, -y)
		row:Show()
		y = y + ROW
	end
	local more = #list - shown
	f.More:SetShown(more > 0 and not saved.collapsed)
	if more > 0 and not saved.collapsed then
		f.More:SetText(string.format("%d more: /mcp goals", more))
		f.More:ClearAllPoints()
		f.More:SetPoint("TOPLEFT", f, "TOPLEFT", PAD + 14, -y)
		y = y + 12
	end
	f:SetHeight(y + PAD / 2)
end

---------------------------------------------------------------------------
-- What the player does
---------------------------------------------------------------------------

-- A goal by its number in the list (as /mcp goals shows it) or its id.
local function Find(arg)
	local list = Listed()
	local n = tonumber(arg)
	if n then return list[math.floor(n)] end
	local goal = byId[arg]
	return goal and not Entry(goal).cleared and goal or nil
end

function Goals.List()
	local list = Listed()
	if #list == 0 then
		ns.Print("No goals. Ask your AI for some!")
		return
	end
	ns.Print(string.format("|cffffffffGoals|r (%d)%s", #list,
		WindowSaved().hidden and " - hidden, /mcp goals show brings them back" or ""))
	for i, goal in ipairs(list) do
		local entry = Entry(goal)
		if goal.text == nil then CheckGoal(goal) end
		ns.Print(string.format("%s%d. %s - %s%s", entry.done and (TICK .. " ") or "", i, ChatText(goal.title),
			entry.done and "done" or ns.Escape(goal.text), entry.muted and GREY .. " (muted)|r" or ""))
	end
end

function Goals.Mute(arg, on)
	local goal = Find(arg)
	if not goal then
		ns.Print("No goal " .. ns.Escape(arg) .. ". /mcp goals lists them.")
		return
	end
	Entry(goal).muted = on or nil
	Layout()
	ns.Print(string.format("%s goal: %s%s", on and "Muted" or "Unmuted", ChatText(goal.title),
		on and " (no pings or tooltip lines until you unmute it)" or ""))
end

function Goals.Show()
	WindowSaved().hidden = nil
	Layout()
	if #Listed() == 0 then ns.Print("No goals. Ask your AI for some!") end
end

function Goals.Hide()
	WindowSaved().hidden = true
	Layout()
	ns.Print("Goals hidden; they keep track of your progress. /mcp goals show brings them back.")
end

function Goals.Collapse()
	local saved = WindowSaved()
	saved.collapsed = not saved.collapsed or nil
	Layout()
end

-- No argument: the done goals; "all"; or one goal.
function Goals.Clear(arg)
	local list = {}
	if arg == "" or arg == "all" then
		for _, goal in ipairs(Listed()) do
			if arg == "all" or Entry(goal).done then list[#list + 1] = goal end
		end
		if #list == 0 then
			ns.Print(arg == "all" and "No goals." or "No done goals to clear (/mcp goals clear all removes all of them).")
			return
		end
	else
		local goal = Find(arg)
		if not goal then
			ns.Print("No goal " .. ns.Escape(arg) .. ". /mcp goals lists them.")
			return
		end
		list[1] = goal
	end
	for _, goal in ipairs(list) do Entry(goal).cleared = true end
	Layout()
	if #list == 1 then
		ns.Print("Goal removed: " .. ChatText(list[1].title))
	else
		ns.Print(string.format("Removed %d goals.", #list))
	end
end

local USAGE = "/mcp goals [show, hide, mute <n>, unmute <n>, clear [<n>, all]] - your AI's background goals: list them, "
	.. "show or hide their window, mute or unmute goal n, remove the done ones (or goal n, or all)"

function Goals.Command(rest)
	local word, arg = strtrim(tostring(rest or "")):lower():match("^(%S*)%s*(.-)$")
	if word == "" or word == "list" then
		Goals.List()
	elseif word == "show" then
		Goals.Show()
	elseif word == "hide" then
		Goals.Hide()
	elseif (word == "mute" or word == "unmute") and arg ~= "" then
		Goals.Mute(arg, word == "mute")
	elseif word == "clear" then
		Goals.Clear(arg)
	else
		ns.Print(USAGE)
	end
end

---------------------------------------------------------------------------
-- Payload
---------------------------------------------------------------------------

local function ParseGoal(raw, missing)
	if type(raw) ~= "table" or type(raw.id) ~= "string" or raw.id == "" then return end
	local goal = {
		id = raw.id, rev = tonumber(raw.rev) or 0, title = tostring(raw.title or raw.id),
		note = raw.note and tostring(raw.note) or nil, zones = {}, areas = {}, mobs = {},
	}
	goal.target = ns.Plans.ParseCondition(raw.target)
	if goal.target.kind == "goal" then goal.target.kind = "unknown" end -- a goal can't wait for a goal
	if goal.target.kind == "have" then goal.item = goal.target.value end
	for _, z in ipairs(type(raw.zones) == "table" and raw.zones or {}) do
		if type(z) == "table" and z.zone then
			local info = ns.Map.Resolve(tostring(z.zone))
			if not info then missing[ns.Escape(z.zone)] = true end
			table.insert(goal.zones, { zone = tostring(z.zone), note = z.note and tostring(z.note) or nil, mapID = info and info.mapID })
		end
	end
	for i, r in ipairs(type(raw.areas) == "table" and raw.areas or {}) do
		local a = type(r) == "table" and type(r.x) == "number" and type(r.y) == "number"
			and ns.Map.Locate(r.zone, r.x, r.y, r.mapID)
		if a then
			a.label = tostring(r.label or ("Area " .. i))
			local width = Width(a.mapID)
			a.yards = width and math.min(math.max(tonumber(r.radius) or 3, 0.1), 50) / 100 * width
			table.insert(goal.areas, a)
		else
			missing[ns.Escape(type(r) == "table" and r.zone or "?")] = true
		end
	end
	for _, m in ipairs(type(raw.mobs) == "table" and raw.mobs or {}) do
		if type(m) == "table" and (m.name or m.npc) then
			table.insert(goal.mobs, {
				name = m.name and tostring(m.name), key = m.name and Normalize(m.name), npc = tonumber(m.npc),
				note = m.note and tostring(m.note) or nil,
			})
		end
	end
	return goal
end

-- The same payload again (a /reload, or another feature's update) changes nothing and says nothing.
function Goals.Receive(payload)
	if payload == received then return end
	received = payload
	local db = DB()
	local isNew = db.id ~= payload.id
	db.id = payload.id
	local old = byId
	goals, byId = {}, {}
	local fresh, missing = {}, {}
	for _, raw in ipairs(type(payload.goals) == "table" and payload.goals or {}) do
		local goal = ParseGoal(raw, missing)
		if goal and not byId[goal.id] then
			table.insert(goals, goal)
			byId[goal.id] = goal
			local entry = db.state[goal.id]
			if not entry or entry.rev ~= goal.rev then
				entry = { rev = goal.rev, muted = entry and entry.muted or nil }
				db.state[goal.id] = entry
				table.insert(fresh, goal)
			end
			local before = old[goal.id]
			if before and before.rev == goal.rev then goal.have = before.have end -- so progress isn't said twice
		end
	end
	for id in pairs(db.state) do
		if not byId[id] then db.state[id] = nil end
	end
	-- A new goal that's already done is done without a fanfare.
	for _, goal in ipairs(fresh) do
		CheckGoal(goal)
		if goal.met then db.state[goal.id].done = time() end
	end
	if isNew then
		if #goals == 0 then
			ns.Print("Goals cleared.")
		else
			local opened
			for _, goal in ipairs(fresh) do
				if db.state[goal.id].done then
					ns.Print(TICK .. " Goal already done: " .. ChatText(goal.title))
				else
					ns.Print("New goal: |cffffffff" .. ChatText(goal.title) .. "|r " .. GREY .. "(" .. ns.Escape(goal.text) .. ")|r")
					opened = true
				end
			end
			-- New goals to work on are shown, even if the list was hidden or collapsed.
			if opened then
				local saved = WindowSaved()
				saved.hidden, saved.collapsed = nil, nil
				Layout()
			end
			local zones = {}
			for zone in pairs(missing) do zones[#zones + 1] = zone end
			table.sort(zones)
			if #zones > 0 then ns.Print("|cffff6060Couldn't find these zones on the map:|r " .. table.concat(zones, ", ")) end
		end
	end
	Goals.Check()
	C_Timer.After(0, function() ns.Plans.Check() end)
end

-- For get_goals: each goal's state and live progress.
local function Progress()
	Goals.Check()
	local list = {}
	for _, goal in ipairs(goals) do
		local entry = Entry(goal) or {}
		list[#list + 1] = {
			id = goal.id, rev = goal.rev, met = goal.met or false, done = entry.done, muted = entry.muted or nil,
			cleared = entry.cleared or nil, progress = goal.text or "",
		}
	end
	return { id = DB().id, goals = list, hidden = WindowSaved().hidden or nil, problem = problem }
end

---------------------------------------------------------------------------

ns.RegisterReceiver("goals", function(payload)
	if type(payload) ~= "table" then return end
	local ok, err = pcall(Goals.Receive, payload)
	if not ok then Report(err) end
end)
ns.RegisterSection(ns.Kinds.STATE, "goals", Progress)
ns.RegisterCommand("goals", USAGE, Goals.Command)
ns.OnLogin(function()
	C_Timer.NewTicker(CHECK_INTERVAL, Tick)
	if TooltipDataProcessor and TooltipDataProcessor.AddTooltipPostCall and Enum.TooltipDataType then
		TooltipDataProcessor.AddTooltipPostCall(Enum.TooltipDataType.Unit, AddMobLines)
	end
end)

-- Events only make it quicker: the ticker checks every second anyway.
local soon = false
local function CheckSoon()
	if soon then return end
	soon = true
	C_Timer.After(0.3, function()
		soon = false
		Goals.Check()
	end)
end

local events = CreateFrame("Frame")
for _, event in ipairs({
	"BAG_UPDATE_DELAYED", "PLAYERBANKSLOTS_CHANGED", "QUEST_TURNED_IN", "QUEST_LOG_UPDATE", "PLAYER_LEVEL_UP",
	"SKILL_LINES_CHANGED", "NEW_RECIPE_LEARNED", "ZONE_CHANGED", "ZONE_CHANGED_INDOORS", "ZONE_CHANGED_NEW_AREA",
	"PLAYER_ENTERING_WORLD", "PLAYER_REGEN_ENABLED", "MERCHANT_SHOW", "MERCHANT_CLOSED",
}) do
	pcall(events.RegisterEvent, events, event)
end
events:SetScript("OnEvent", function(_, event, arg1)
	if event == "MERCHANT_SHOW" then
		merchantVisits = merchantVisits + 1
		merchant = { visit = merchantVisits }
		-- A moment later, when the client has the merchant's items.
		C_Timer.After(0.5, function()
			local ok, err = pcall(CheckMerchant)
			if not ok then Report(err) end
		end)
	elseif event == "MERCHANT_CLOSED" then
		merchant = nil
	elseif event == "PLAYER_REGEN_ENABLED" then
		Flush()
	elseif event:find("^ZONE_CHANGED") or event == "PLAYER_ENTERING_WORLD" then
		if event == "PLAYER_ENTERING_WORLD" and arg1 then DB().zone = nil end -- a new session: say it again
		local ok, err = pcall(CheckPlaces)
		if not ok then Report(err) end
		if pending.waiting then Flush() end -- e.g. out of a dungeon
	else
		CheckSoon()
	end
end)
