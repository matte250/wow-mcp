-- WowMCP glow: the AI makes things in the game's own windows glow (payload key "glow").
--
-- A mark is an item, a spell or a named frame, with a colour. Wherever a marked item shows up in the
-- game's windows (bags, bank, a vendor's items, loot, quest rewards, the character's gear, action
-- bars, a trainer's list), and wherever a marked spell shows up (action bars, the spellbook, a trainer's list;
-- any rank of it), its button gets a glow like the game's
-- own proc glow: a pulsing halo with sparkling "ants" running around it. A named frame (e.g.
-- "MerchantRepairAllButton") glows itself.
--
-- The glow is a plain frame of ours laid over the button: a child of UIParent anchored to the button, never
-- a child of it, so it doesn't touch the button, its secure code or what other code finds among the
-- button's children. That makes it fine in combat and on protected buttons. It hides itself the moment its
-- button isn't visible. It only finds buttons in Blizzard's windows,
-- plus BetterBags' bags when that addon is loaded (an optional integration: if BetterBags changes,
-- its part quietly stops working). Buttons are found again on the events that change them and,
-- while anything is marked, every half second (vendor pages, scrolled loot, bags just opened).
--
-- Moving the mouse over a glowing button (or showing its tooltip some other way, e.g. a gamepad) means
-- "seen": that mark stops glowing everywhere until the AI sends new marks. That's kept per update, so a
-- /reload doesn't bring it back.

local _, ns = ...

local Glow = {}
ns.Glow = Glow

local COLORS = {
	gold = { 1, 0.82, 0.25 }, green = { 0.35, 1, 0.35 }, red = { 1, 0.3, 0.25 },
	blue = { 0.35, 0.7, 1 }, purple = { 0.8, 0.45, 1 },
}
local GLOW_TEXTURE = "Interface\\SpellActivationOverlay\\IconAlert"
local ANTS_TEXTURE = "Interface\\SpellActivationOverlay\\IconAlertAnts"
local ANT_FRAMES, ANT_STEP = 22, 0.02 -- the ants texture: 22 frames of 48 px in a 256 px sheet, 5 per row
local POLL = 0.5

-- A mark is { key, colour } (frames also { path }); items/spells: id -> mark; frames: a list.
local marks = { items = {}, spells = {}, frames = {} }
local count = 0      -- marks not dismissed
local dismissed = {} -- key -> true: hovered since the last update (WowMCPDB.glowDismissed.keys)
local hidden = false -- /mcp glow off, until the next update
local glows = {}     -- target frame -> our glow frame
local missing = {}   -- frame paths already reported as not found

---------------------------------------------------------------------------
-- The glow
---------------------------------------------------------------------------

local Dismiss

local function Hovered(target)
	if target.IsMouseOver and target:IsMouseOver() then return true end
	return GameTooltip:IsShown() and GameTooltip:GetOwner() == target
end

local function Animate(self, elapsed)
	if not (self.target:IsVisible()) then
		self:Hide()
		return
	end
	if self.mark and Hovered(self.target) then
		Dismiss(self.mark.key)
		return
	end
	self.t = self.t + elapsed
	local pulse = 0.65 + 0.2 * math.sin(self.t * 4) -- ADD blend over the icon: never at full strength
	self.halo:SetAlpha(pulse)
	for _, edge in ipairs(self.edges) do edge:SetAlpha(0.5 + 0.5 * pulse) end
	local n = math.floor(self.t / ANT_STEP) % ANT_FRAMES
	local col, row = n % 5, math.floor(n / 5)
	self.ants:SetTexCoord(col * 48 / 256, (col + 1) * 48 / 256, row * 48 / 256, (row + 1) * 48 / 256)
end

local function NewGlow(target)
	local g = CreateFrame("Frame", nil, UIParent)
	g.t, g.target = 0, target
	-- The halo and ants reach past the button by a fifth on each side, like the game's proc glow.
	local w, h = target:GetSize()
	w, h = (w and w > 0) and w or 36, (h and h > 0) and h or 36
	g:SetPoint("TOPLEFT", target, "TOPLEFT", -w * 0.2, h * 0.2)
	g:SetPoint("BOTTOMRIGHT", target, "BOTTOMRIGHT", w * 0.2, -h * 0.2)
	g.halo = g:CreateTexture(nil, "ARTWORK")
	g.halo:SetTexture(GLOW_TEXTURE)
	g.halo:SetTexCoord(0.0078125, 0.5078125, 0.27734375, 0.52734375)
	g.halo:SetBlendMode("ADD")
	g.halo:SetAllPoints(g)
	g.ants = g:CreateTexture(nil, "OVERLAY")
	g.ants:SetTexture(ANTS_TEXTURE)
	g.ants:SetBlendMode("ADD")
	g.ants:SetAllPoints(g)
	-- A thin frame on the button's own edge: plain colour, so something shows even if a texture is missing.
	g.edges = {}
	for i, side in ipairs({ { "TOPLEFT", "TOPRIGHT" }, { "BOTTOMLEFT", "BOTTOMRIGHT" }, { "TOPLEFT", "BOTTOMLEFT" },
	                        { "TOPRIGHT", "BOTTOMRIGHT" } }) do
		local edge = g:CreateTexture(nil, "OVERLAY")
		edge:SetPoint(side[1], target, side[1])
		edge:SetPoint(side[2], target, side[2])
		if i <= 2 then edge:SetHeight(2) else edge:SetWidth(2) end
		g.edges[i] = edge
	end
	g:SetScript("OnUpdate", Animate)
	return g
end

local function Paint(g, colour)
	if g.colour == colour then return end
	g.colour = colour
	local rgb = COLORS[colour] or COLORS.gold
	local r, gr, b = rgb[1], rgb[2], rgb[3]
	-- Gold is the textures' own colour; others are drawn from the textures made grey.
	local tint = colour ~= "gold"
	for _, t in ipairs({ g.halo, g.ants }) do
		t:SetDesaturated(tint)
		if tint then t:SetVertexColor(r, gr, b) else t:SetVertexColor(1, 1, 1) end
	end
	for _, edge in ipairs(g.edges) do edge:SetColorTexture(r, gr, b, 1) end
end

local function Show(target, mark)
	local g = glows[target]
	if not g then
		local ok, made = pcall(NewGlow, target)
		if not ok then return end
		g, glows[target] = made, made
	end
	g.mark = mark
	Paint(g, mark.colour)
	pcall(function()
		g:SetFrameStrata(target:GetFrameStrata())
		g:SetFrameLevel((target:GetFrameLevel() or 0) + 5)
		g:SetScale(target:GetEffectiveScale() / UIParent:GetEffectiveScale())
	end)
	g:Show()
end

---------------------------------------------------------------------------
-- Finding the buttons
---------------------------------------------------------------------------

local function ItemMark(id)
	-- pcall: restricted content can hand out secret values that can't be used as keys.
	local ok, colour = pcall(function() return type(id) == "number" and marks.items[id] or nil end)
	return ok and colour or nil
end

local spellNames = {} -- name -> colour, so any rank of a marked spell glows

local function SpellName(id)
	if C_Spell and C_Spell.GetSpellInfo then
		local info = C_Spell.GetSpellInfo(id)
		return info and info.name
	end
	return GetSpellInfo and (GetSpellInfo(id))
end

local function SpellMark(id, name)
	local ok, colour = pcall(function()
		return type(id) == "number" and marks.spells[id] or spellNames[name or SpellName(id)] or nil
	end)
	return ok and colour or nil
end

local function LinkID(link)
	return type(link) == "string" and tonumber(link:match("item:(%d+)")) or nil
end

local function Shown(frame)
	return frame and frame.IsVisible and frame:IsVisible()
end

local function BagItemID(bag, slot)
	if C_Container.GetContainerItemID then return C_Container.GetContainerItemID(bag, slot) end
	local info = C_Container.GetContainerItemInfo(bag, slot)
	return info and info.itemID
end

-- Each finder calls want(button, mark) for every marked thing it sees (mark nil: not marked).
local finders = {}

-- The game's bags and bank: container frames list their item buttons, or name them <frame>Item<n>.
function finders.Bags(want)
	local frames = {}
	if ContainerFrameUtil_EnumerateContainerFrames then
		pcall(function() for _, frame in ContainerFrameUtil_EnumerateContainerFrames() do frames[#frames + 1] = frame end end)
	end
	if #frames == 0 then
		for i = 1, NUM_CONTAINER_FRAMES or 13 do frames[#frames + 1] = _G["ContainerFrame" .. i] end
	end
	frames[#frames + 1] = ContainerFrameCombinedBags
	frames[#frames + 1] = BankPanel
	local seen = {}
	for _, frame in ipairs(frames) do
		if Shown(frame) and not seen[frame] then
			seen[frame] = true
			local function Visit(button)
				local bag = button.GetBagID and button:GetBagID()
					or button.GetBankTabID and button:GetBankTabID() or frame:GetID()
				local slot = button.GetContainerSlotID and button:GetContainerSlotID() or button:GetID()
				want(button, ItemMark(BagItemID(bag, slot)))
			end
			if frame.EnumerateValidItems then
				for _, button in frame:EnumerateValidItems() do pcall(Visit, button) end
			elseif frame.GetName and frame:GetName() then
				for i = 1, 40 do
					local button = _G[frame:GetName() .. "Item" .. i]
					if not button then break end
					pcall(Visit, button)
				end
			end
		end
	end
	-- The old bank window: its own slots are the bank container (-1).
	if Shown(BankFrame) then
		for i = 1, NUM_BANKGENERIC_SLOTS or 28 do
			local button = _G["BankFrameItem" .. i]
			if button then pcall(function() want(button, ItemMark(BagItemID(-1, button:GetID()))) end) end
		end
	end
end

-- The vendor's items on its current page (button IDs are the item's index at the vendor).
function finders.Merchant(want)
	if not Shown(MerchantFrame) or (MerchantFrame.selectedTab or 1) ~= 1 then return end
	for i = 1, MERCHANT_ITEMS_PER_PAGE or 10 do
		local button = _G["MerchantItem" .. i .. "ItemButton"]
		if Shown(button) then pcall(function() want(button, ItemMark(GetMerchantItemID(button:GetID()))) end) end
	end
end

-- Loot: the scrolling loot window, or the old one's LootButton<n>.
function finders.Loot(want)
	if not Shown(LootFrame) then return end
	local function Visit(button, slot)
		want(button, ItemMark(LinkID(GetLootSlotLink(slot))))
	end
	if LootFrame.ScrollBox and LootFrame.ScrollBox.ForEachFrame then
		LootFrame.ScrollBox:ForEachFrame(function(frame)
			pcall(function()
				local slot = frame.GetSlotIndex and frame:GetSlotIndex()
					or frame.GetElementData and frame:GetElementData().slotIndex
				Visit(frame.Item or frame, slot)
			end)
		end)
	else
		for i = 1, LOOTFRAME_NUMBUTTONS or 4 do
			local button = _G["LootButton" .. i]
			if Shown(button) then pcall(Visit, button, button.slot) end
		end
	end
end

-- Quest rewards, at a quest giver or in the quest log.
function finders.QuestRewards(want)
	if not Shown(QuestInfoRewardsFrame) then return end
	local buttons = QuestInfoRewardsFrame.RewardButtons
	if type(buttons) ~= "table" then
		buttons = {}
		for i = 1, 10 do buttons[i] = _G["QuestInfoRewardsFrameQuestInfoItem" .. i] end
	end
	local fromLog = QuestInfoFrame and QuestInfoFrame.questLog
	for _, button in pairs(buttons) do
		if Shown(button) and button.type and (button.objectType == nil or button.objectType == "item") then
			pcall(function()
				local link = fromLog and GetQuestLogItemLink(button.type, button:GetID())
					or GetQuestItemLink(button.type, button:GetID())
				want(button, ItemMark(LinkID(link)))
			end)
		end
	end
end

local GEAR_SLOTS = { "Head", "Neck", "Shoulder", "Back", "Chest", "Shirt", "Tabard", "Wrist", "Hands", "Waist",
                     "Legs", "Feet", "Finger0", "Finger1", "Trinket0", "Trinket1", "MainHand", "SecondaryHand",
                     "Ranged", "Ammo" }

function finders.Gear(want)
	if not Shown(CharacterFrame) then return end
	for _, name in ipairs(GEAR_SLOTS) do
		local button = _G["Character" .. name .. "Slot"]
		if Shown(button) then pcall(function() want(button, ItemMark(GetInventoryItemID("player", button:GetID()))) end) end
	end
end

local BARS = { "ActionButton", "MultiBarBottomLeftButton", "MultiBarBottomRightButton", "MultiBarRightButton",
               "MultiBarLeftButton", "MultiBar5Button", "MultiBar6Button", "MultiBar7Button" }

-- Spells and items on the action bars.
function finders.ActionBars(want)
	for _, bar in ipairs(BARS) do
		for i = 1, 12 do
			local button = _G[bar .. i]
			if Shown(button) then
				pcall(function()
					local action = button.action or (button.GetAttribute and button:GetAttribute("action"))
					if not action then return end
					local kind, id = GetActionInfo(action)
					if kind == "spell" then
						want(button, SpellMark(id))
					elseif kind == "item" then
						want(button, ItemMark(id))
					end
				end)
			end
		end
	end
end

-- A trainer's list: spells (any rank of a marked one) and recipes' items. Its scrolling list, or the old
-- window's ClassTrainerSkill<n> (button IDs are service indexes).
function finders.Trainer(want)
	if not Shown(ClassTrainerFrame) or not (C_TooltipInfo and C_TooltipInfo.GetTrainerService) then return end
	local function Visit(button, index)
		local data = C_TooltipInfo.GetTrainerService(index)
		if type(data) ~= "table" or not data.id then return end
		if data.type == Enum.TooltipDataType.Item then
			want(button, ItemMark(data.id))
		else
			want(button, SpellMark(data.id, (GetTrainerServiceInfo(index))))
		end
	end
	local scroll = ClassTrainerFrame.ScrollBox
	if scroll and scroll.ForEachFrame then
		scroll:ForEachFrame(function(frame)
			pcall(function()
				local data = frame.GetElementData and frame:GetElementData()
				local index = frame.index or type(data) == "number" and data
					or type(data) == "table" and (data.skillIndex or data.index) or nil
				if index then Visit(frame, index) end
			end)
		end)
	else
		for i = 1, CLASS_TRAINER_SKILLS_DISPLAYED or 11 do
			local button = _G["ClassTrainerSkill" .. i]
			if Shown(button) then pcall(Visit, button, button:GetID()) end
		end
	end
end

-- The spellbook: the paged one (PlayerSpellsFrame), or the old SpellButton<n>.
function finders.SpellBook(want)
	local book = PlayerSpellsFrame and PlayerSpellsFrame.SpellBookFrame
	local paged = book and book.PagedSpellsFrame
	if Shown(paged) and paged.ForEachElementFrame then
		paged:ForEachElementFrame(function(frame)
			pcall(function()
				local info = frame.spellBookItemInfo
				local id = type(info) == "table" and (info.spellID or info.actionID)
				if id then want(frame.Button or frame, SpellMark(id, info.name)) end
			end)
		end)
	end
	if Shown(SpellBookFrame) then
		for i = 1, SPELLS_PER_PAGE or 12 do
			local button = _G["SpellButton" .. i]
			if Shown(button) then
				pcall(function()
					local slot = SpellBook_GetSpellBookSlot(button)
					if not slot then return end
					local id
					if C_SpellBook and C_SpellBook.GetSpellBookItemInfo then
						local info = C_SpellBook.GetSpellBookItemInfo(slot, SpellBookFrame.bookType)
						id = info and (info.spellID or info.actionID)
					else
						id = select(2, GetSpellBookItemInfo(slot, SpellBookFrame.bookType))
					end
					want(button, SpellMark(id))
				end)
			end
		end
	end
end

-- BetterBags (optional): its item buttons by slot key "<bag>_<slot>", drawn by item.frame.
function finders.BetterBags(want)
	if not (LibStub and C_AddOns and C_AddOns.IsAddOnLoaded and C_AddOns.IsAddOnLoaded("BetterBags")) then return end
	local ok, itemFrame = pcall(function()
		return LibStub("AceAddon-3.0"):GetAddon("BetterBags", true):GetModule("ItemFrame", true)
	end)
	if not ok or type(itemFrame) ~= "table" or type(itemFrame.buttonsBySlotkey) ~= "table" then return end
	for slotkey, item in pairs(itemFrame.buttonsBySlotkey) do
		local bag, slot = tostring(slotkey):match("^(%-?%d+)_(%d+)$")
		local frame = type(item) == "table" and item.frame
		if bag and Shown(frame) then
			pcall(function() want(frame, ItemMark(BagItemID(tonumber(bag), tonumber(slot)))) end)
		end
	end
end

-- A frame by its global name, with optional keys after dots: "MerchantFrame.SellAllJunkButton".
local function Resolve(path)
	local frame
	for key in path:gmatch("[^%.]+") do
		if frame == nil then frame = _G[key] elseif type(frame) == "table" then frame = frame[key] else return nil end
	end
	return type(frame) == "table" and frame.GetObjectType and frame or nil
end

function finders.Frames(want)
	for _, mark in ipairs(marks.frames) do
		local frame = Resolve(mark.path)
		if frame then
			if Shown(frame) then want(frame, mark) end
		elseif not missing[mark.path] then
			missing[mark.path] = true
			ns.Print("Can't find '" .. ns.Escape(mark.path) .. "' in the game's UI, so it can't glow.")
		end
	end
end

local ORDER = { "Bags", "BetterBags", "Merchant", "Loot", "QuestRewards", "Gear", "Trainer", "SpellBook", "ActionBars",
                "Frames" }

function Glow.Refresh()
	local wanted = {}
	if count > 0 and not hidden then
		local function want(frame, mark)
			if frame and mark and not dismissed[mark.key] then wanted[frame] = mark end
		end
		for _, name in ipairs(ORDER) do
			pcall(finders[name], want) -- a window that isn't like we expect only loses its own glow
		end
	end
	for target, g in pairs(glows) do
		if not wanted[target] then g:Hide() end
	end
	for target, mark in pairs(wanted) do Show(target, mark) end
end

-- Hovered: this mark stops glowing everywhere until the next update.
function Dismiss(key)
	if dismissed[key] then return end
	dismissed[key] = true
	count = count - 1
	Glow.Refresh()
end

-- What glows right now, for tests and /mcp glow: { [frame] = colour }.
function Glow.Shown()
	local shown = {}
	for target, g in pairs(glows) do
		if g:IsShown() then shown[target] = g.colour end
	end
	return shown
end

---------------------------------------------------------------------------
-- Updates, events, command
---------------------------------------------------------------------------

local driver = CreateFrame("Frame")
local sinceRefresh, dirty = 0, false

local function Wake()
	dirty = true
	driver:SetShown(count > 0 and not hidden or next(Glow.Shown()) ~= nil)
end

driver:SetScript("OnUpdate", function(self, elapsed)
	sinceRefresh = sinceRefresh + elapsed
	if dirty or sinceRefresh >= POLL then
		dirty, sinceRefresh = false, 0
		Glow.Refresh()
		if count == 0 or hidden then self:Hide() end
	end
end)
driver:Hide()

for _, event in ipairs({ "BAG_UPDATE_DELAYED", "MERCHANT_SHOW", "MERCHANT_UPDATE", "LOOT_OPENED", "LOOT_SLOT_CLEARED",
                         "QUEST_DETAIL", "QUEST_COMPLETE", "PLAYER_EQUIPMENT_CHANGED", "ACTIONBAR_SLOT_CHANGED",
                         "BANKFRAME_OPENED", "TRAINER_SHOW", "TRAINER_UPDATE", "SPELLS_CHANGED" }) do
	pcall(driver.RegisterEvent, driver, event)
end
driver:SetScript("OnEvent", Wake)

local function Describe()
	local items, spells = 0, 0
	for _ in pairs(marks.items) do items = items + 1 end
	for _ in pairs(marks.spells) do spells = spells + 1 end
	local parts = {}
	if items > 0 then parts[#parts + 1] = items .. " item(s)" end
	if spells > 0 then parts[#parts + 1] = spells .. " spell(s)" end
	if #marks.frames > 0 then parts[#parts + 1] = #marks.frames .. " UI element(s)" end
	return table.concat(parts, ", ")
end

local function Receive(payload)
	if type(payload) ~= "table" then return end
	wipe(marks.items); wipe(marks.spells); wipe(marks.frames); wipe(missing); wipe(spellNames)
	count, hidden = 0, false
	-- What was hovered stays dismissed across /reload, until the next update.
	local kept = WowMCPDB.glowDismissed
	if type(kept) ~= "table" or kept.id ~= payload.id then
		kept = { id = payload.id, keys = {} }
		WowMCPDB.glowDismissed = kept
	end
	dismissed = kept.keys
	for _, mark in ipairs(type(payload.marks) == "table" and payload.marks or {}) do
		if type(mark) == "table" then
			local colour = COLORS[mark.color] and mark.color or "gold"
			local key
			if type(mark.item) == "number" then
				key = "item:" .. mark.item
				marks.items[mark.item] = { key = key, colour = colour }
			elseif type(mark.spell) == "number" then
				key = "spell:" .. mark.spell
				marks.spells[mark.spell] = { key = key, colour = colour }
				local name = SpellName(mark.spell)
				if name then spellNames[name] = marks.spells[mark.spell] end
			elseif type(mark.frame) == "string" then
				key = "frame:" .. mark.frame
				table.insert(marks.frames, { key = key, path = mark.frame, colour = colour })
			end
			if key and not dismissed[key] then count = count + 1 end
		end
	end
	Wake()
	-- Announce each update once; a /reload restores the glow quietly.
	if payload.id == WowMCPDB.glowId then return end
	WowMCPDB.glowId = payload.id
	if count == 0 then
		ns.Print("Glow cleared.")
	else
		ns.Print("Your AI marked " .. Describe() .. ": they glow wherever they show up in your bags, at vendors, "
			.. "in loot, quest rewards, your gear, at trainers, in your spellbook and action bars, until you hover "
			.. "them. /mcp glow off hides it all.")
	end
end

function Glow.Command(_, rest)
	if rest == "off" then
		hidden = true
		Glow.Refresh()
		ns.Print("Glow hidden until your AI marks something new.")
	elseif rest == "on" then
		-- Also what was hovered: everything marked glows again.
		hidden = false
		for _ in pairs(dismissed) do count = count + 1 end
		wipe(dismissed)
		Wake()
		ns.Print(count > 0 and "Glow shown again." or "Nothing is marked.")
	elseif count == 0 then
		ns.Print("Nothing is marked.")
	else
		ns.Print("Marked: " .. Describe() .. (hidden and " (hidden: /mcp glow on shows it)" or "") .. ".")
	end
end

ns.RegisterReceiver("glow", Receive)
ns.RegisterCommand("glow", "/mcp glow [off, on] - what your AI made glow in the game's windows; hide it, or show it all again", Glow.Command)

-- Bags opened by key or click show their buttons before any event says so.
for _, name in ipairs({ "ToggleBag", "OpenBag", "ToggleBackpack", "OpenAllBags", "ToggleAllBags", "MerchantFrame_Update",
                        "LootFrame_Update", "QuestInfo_Display", "ClassTrainerFrame_Update" }) do
	if type(_G[name]) == "function" then hooksecurefunc(name, Wake) end
end
