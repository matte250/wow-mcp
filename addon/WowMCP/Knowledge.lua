-- WowMCP knowledge: recipes known and vendors seen, cached when those windows open (request kind KNOWLEDGE).
--
-- The game only lists recipes while a profession window is open, so each profession is cached
-- per character whenever its window opens or changes. Vendors are cached for all characters
-- when their window opens, with the player's position then: newest first, at most MAX_VENDORS.
-- Both live in WowMCPDB.knowledge.

local _, ns = ...

local MAX_VENDORS = 20
local MAX_COSTS = 3 -- other currencies per item, as MerchantFrame reads them
local BASIC_REAGENT = Enum.CraftingReagentType and Enum.CraftingReagentType.Basic or 1
local COLORS = { [0] = "orange", [1] = "yellow", [2] = "green", [3] = "gray" } -- Enum.TradeskillRelativeDifficulty

local problems = {} -- why the last caching failed, per section
local merchant      -- the vendor window open now: { where = player position when it opened }

local function DB()
	local db = WowMCPDB.knowledge or {}
	WowMCPDB.knowledge = db
	db.recipes = db.recipes or {} -- character -> profession name -> profession
	db.vendors = db.vendors or {}
	return db
end

local function CharacterKey()
	return UnitName("player") .. "-" .. GetRealmName()
end

local function Percent(v)
	return math.floor(v * 1000 + 0.5) / 10
end

local function ItemName(itemID)
	return itemID and C_Item.GetItemNameByID(itemID)
end

local function Try(section, fn)
	local ok, err = pcall(fn)
	problems[section] = not ok and tostring(err) or nil
end

-- Calls fn once, delay seconds after the first of a burst of calls.
local function Debounced(delay, fn)
	local pending = false
	return function()
		if pending then return end
		pending = true
		C_Timer.After(delay, function()
			pending = false
			fn()
		end)
	end
end

---------------------------------------------------------------------------
-- Recipes
---------------------------------------------------------------------------

-- The open profession, unless it isn't the character's own (a linked or guild list, or an NPC crafting).
local function OwnProfession()
	if C_TradeSkillUI.IsTradeSkillLinked() or C_TradeSkillUI.IsTradeSkillGuild()
		or C_TradeSkillUI.IsTradeSkillGuildMember() or C_TradeSkillUI.IsNPCCrafting() then
		return
	end
	local info = C_TradeSkillUI.GetChildProfessionInfo()
	if not info or (info.professionID or 0) == 0 then info = C_TradeSkillUI.GetBaseProfessionInfo() end
	if info and (info.professionID or 0) ~= 0 then return info end
end

local function Reagents(recipeID)
	local list = {}
	local schematic = C_TradeSkillUI.GetRecipeSchematic(recipeID, false)
	for _, slot in ipairs(schematic and schematic.reagentSlotSchematics or {}) do
		local reagent = slot.reagentType == BASIC_REAGENT and slot.reagents[1]
		if reagent and reagent.itemID then
			list[#list + 1] = { item = reagent.itemID, count = slot.quantityRequired, name = ItemName(reagent.itemID) }
		end
	end
	return list
end

local function ScanRecipes()
	if C_TradeSkillUI.IsDataSourceChanging() then return end -- a list update follows
	local profession = OwnProfession()
	if not profession then return end
	-- The filtered list is only what the window shows (search, "has materials", ...).
	local ids = C_TradeSkillUI.GetAllRecipeIDs and C_TradeSkillUI.GetAllRecipeIDs() or C_TradeSkillUI.GetFilteredRecipeIDs()
	local recipes = {}
	for _, recipeID in ipairs(ids or {}) do
		local info = C_TradeSkillUI.GetRecipeInfo(recipeID)
		if info and info.learned then
			recipes[#recipes + 1] = {
				id = recipeID, name = info.name, color = COLORS[info.relativeDifficulty], reagents = Reagents(recipeID),
			}
		end
	end
	if #recipes == 0 then return end -- not loaded yet; a list update follows
	table.sort(recipes, function(a, b) return a.name < b.name end)
	local parent = profession.parentProfessionName
	local name = parent and parent ~= "" and parent or profession.professionName
	local db = DB().recipes
	local mine = db[CharacterKey()] or {}
	db[CharacterKey()] = mine
	mine[name] = { name = name, rank = profession.skillLevel, maxRank = profession.maxSkillLevel, at = time(), recipes = recipes }
end

local function Recipes()
	local list = {}
	for _, profession in pairs(DB().recipes[CharacterKey()] or {}) do
		-- Item names the game hadn't loaded while the window was open may be there now.
		for _, recipe in ipairs(profession.recipes) do
			for _, reagent in ipairs(recipe.reagents) do reagent.name = reagent.name or ItemName(reagent.item) end
		end
		list[#list + 1] = profession
	end
	table.sort(list, function(a, b) return a.name < b.name end)
	return { professions = list, problem = problems.recipes }
end

---------------------------------------------------------------------------
-- Vendors
---------------------------------------------------------------------------

local function Where()
	local mapID = C_Map.GetBestMapForUnit("player")
	local info = mapID and C_Map.GetMapInfo(mapID)
	local where = { mapID = mapID, zone = info and info.name or GetRealZoneText() }
	local subzone = GetSubZoneText()
	if subzone and subzone ~= "" then where.subzone = subzone end
	local pos = mapID and C_Map.GetPlayerMapPosition(mapID, "player")
	if pos then
		local x, y = pos.x, pos.y
		where.x, where.y = Percent(x), Percent(y)
	end
	return where
end

local function NpcID()
	local ok, id = pcall(function() return tonumber(UnitGUID("npc"):match("^%a+%-.-%-.-%-.-%-.-%-(%d+)%-")) end)
	return ok and id or nil
end

-- What an item costs besides money, e.g. "2 Honor Token".
local function OtherCost(index)
	local parts = {}
	for i = 1, MAX_COSTS do
		local texture, value, link, currencyName = GetMerchantItemCostItem(index, i)
		local name = currencyName or (link and link:match("%[(.-)%]"))
		if texture and name then parts[#parts + 1] = tostring(value) .. " " .. name end
	end
	return #parts > 0 and table.concat(parts, ", ") or nil
end

local function MerchantItems()
	local items = {}
	for i = 1, GetMerchantNumItems() do
		local info = C_MerchantFrame.GetItemInfo(i)
		if info then
			local item = { id = GetMerchantItemID(i), name = info.name, price = info.price }
			if info.stackCount > 1 then item.stack = info.stackCount end
			if info.numAvailable >= 0 then item.stock = info.numAvailable end -- -1: unlimited
			if not info.isPurchasable then item.locked = true end
			if info.hasExtendedCost and GetMerchantItemCostInfo(i) > 0 then item.cost = OtherCost(i) end
			items[#items + 1] = item
		end
	end
	return items
end

local function SeeVendor()
	if not merchant then return end
	local name, items = UnitName("npc"), MerchantItems()
	if not name or #items == 0 then return end
	local vendor = { name = name, npc = NpcID(), at = time(), items = items }
	for k, v in pairs(merchant.where) do vendor[k] = v end
	local vendors = DB().vendors
	for i = #vendors, 1, -1 do
		if vendors[i].name == vendor.name and vendors[i].zone == vendor.zone then table.remove(vendors, i) end
	end
	table.insert(vendors, 1, vendor)
	for i = #vendors, MAX_VENDORS + 1, -1 do vendors[i] = nil end
end

local function Vendors()
	local vendors = DB().vendors
	for _, vendor in ipairs(vendors) do
		for _, item in ipairs(vendor.items) do item.name = item.name or ItemName(item.id) end
	end
	return { vendors = vendors, problem = problems.vendors }
end

---------------------------------------------------------------------------

ns.RegisterSection(ns.Kinds.KNOWLEDGE, "recipes", Recipes)
ns.RegisterSection(ns.Kinds.KNOWLEDGE, "vendors", Vendors)

local rescanRecipes = Debounced(0.5, function() Try("recipes", ScanRecipes) end)
local rescanVendor = Debounced(0.5, function() Try("vendors", SeeVendor) end) -- item names arrive later

local events = CreateFrame("Frame")
for _, event in ipairs({ "TRADE_SKILL_SHOW", "TRADE_SKILL_LIST_UPDATE", "TRADE_SKILL_DATA_SOURCE_CHANGED",
	"MERCHANT_SHOW", "MERCHANT_UPDATE", "MERCHANT_CLOSED" }) do
	events:RegisterEvent(event)
end
events:SetScript("OnEvent", function(_, event)
	if event == "MERCHANT_SHOW" then
		local ok, where = pcall(Where)
		merchant = { where = ok and where or {} }
		Try("vendors", SeeVendor)
	elseif event == "MERCHANT_UPDATE" then
		if merchant then rescanVendor() end
	elseif event == "MERCHANT_CLOSED" then
		merchant = nil
	else
		rescanRecipes()
	end
end)
