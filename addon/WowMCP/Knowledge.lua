-- WowMCP knowledge: recipes known and vendors seen, cached when those windows open (request kind KNOWLEDGE).
--
-- The game only lists recipes while a profession window is open, so each profession is cached
-- per character whenever its window opens or changes. Recipes learned since are caught as they're
-- learned and kept per character until a profession window lists them. Vendors are cached for all
-- characters when their window opens, with the player's position then and what each item binds to:
-- newest first, at most MAX_VENDORS. All of it lives in WowMCPDB.knowledge.

local _, ns = ...

local Knowledge = {}
ns.Knowledge = Knowledge

local MAX_VENDORS = 20
local MAX_COSTS = 3 -- other currencies per item, as MerchantFrame reads them
local BASIC_REAGENT = Enum.CraftingReagentType and Enum.CraftingReagentType.Basic or 1
local COLORS = { [0] = "orange", [1] = "yellow", [2] = "green", [3] = "gray" } -- Enum.TradeskillRelativeDifficulty

local problems = {} -- why the last caching failed, per section
local merchant      -- the vendor window open now: { where = player position when it opened }
local session = {}  -- spells learned since login, class spells too: { id, name, recipe }

local function DB()
	local db = WowMCPDB.knowledge or {}
	WowMCPDB.knowledge = db
	db.recipes = db.recipes or {} -- character -> profession name -> profession
	db.learned = db.learned or {} -- character -> recipes learned since a window listed them: { id, name, at, ... }
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

-- The item a recipe makes (nil for enchants and the like) and its reagents.
local function Schematic(recipeID)
	local list = {}
	local schematic = C_TradeSkillUI.GetRecipeSchematic(recipeID, false)
	for _, slot in ipairs(schematic and schematic.reagentSlotSchematics or {}) do
		local reagent = slot.reagentType == BASIC_REAGENT and slot.reagents[1]
		if reagent and reagent.itemID then
			list[#list + 1] = { item = reagent.itemID, count = slot.quantityRequired, name = ItemName(reagent.itemID) }
		end
	end
	return schematic and schematic.outputItemID, list
end

-- Whether a profession's recipes include a learned one: by ID, or by name if it came without one.
local function Lists(recipes, learned)
	for _, recipe in ipairs(recipes) do
		if (learned.id and recipe.id == learned.id) or (not learned.id and recipe.name == learned.name) then return true end
	end
	return false
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
			local item, reagents = Schematic(recipeID)
			recipes[#recipes + 1] = {
				id = recipeID, name = info.name, item = item, color = COLORS[info.relativeDifficulty], reagents = reagents,
			}
		end
	end
	if #recipes == 0 then return end -- not loaded yet; a list update follows
	table.sort(recipes, function(a, b) return a.name < b.name end)
	local parent = profession.parentProfessionName
	local name = parent and parent ~= "" and parent or profession.professionName
	local db = DB()
	local mine = db.recipes[CharacterKey()] or {}
	db.recipes[CharacterKey()] = mine
	mine[name] = { name = name, rank = profession.skillLevel, maxRank = profession.maxSkillLevel, at = time(), recipes = recipes }
	local learned = db.learned[CharacterKey()] or {}
	for i = #learned, 1, -1 do
		if Lists(recipes, learned[i]) then table.remove(learned, i) end
	end
end

-- Item names the game hadn't loaded while the window was open may be there now.
local function NameReagents(recipes)
	for _, recipe in ipairs(recipes) do
		for _, reagent in ipairs(recipe.reagents or {}) do reagent.name = reagent.name or ItemName(reagent.item) end
	end
end

local function Recipes()
	local list = {}
	for _, profession in pairs(DB().recipes[CharacterKey()] or {}) do
		NameReagents(profession.recipes)
		list[#list + 1] = profession
	end
	table.sort(list, function(a, b) return a.name < b.name end)
	local learned = DB().learned[CharacterKey()] or {}
	NameReagents(learned)
	return { professions = list, learned = learned, problem = problems.recipes }
end

---------------------------------------------------------------------------
-- Learning
---------------------------------------------------------------------------

-- Which signals Forever gives when a recipe is learned isn't known, so they all count, once per
-- learn: NEW_RECIPE_LEARNED (recipes), LEARNED_SPELL_IN_SKILL_LINE (any spell) and the chat line,
-- where "how to create a new item" says it's a recipe. Only recipes are saved.

-- The pattern of a chat line printed from a format string, capturing its %s.
local function Pattern(format)
	return "^" .. format:gsub("%%%d?%$?s", "\1"):gsub("[%^%$%(%)%%%.%[%]%*%+%-%?]", "%%%0"):gsub("\1", "(.+)") .. "$"
end

local LEARN_LINES = { -- pattern, whether it's a recipe
	{ Pattern(ERR_LEARN_RECIPE_S or "You have learned how to create a new item: %s."), true },
	{ Pattern(ERR_LEARN_SPELL_S or "You have learned a new spell: %s."), false },
}

-- The profession a learned recipe belongs to, what it makes and needs, as far as the game says
-- with no profession window open.
local function Describe(recipe)
	local ok, info = pcall(C_TradeSkillUI.GetProfessionInfoByRecipeID, recipe.id)
	if ok and type(info) == "table" and (info.professionID or 0) ~= 0 then
		local parent = info.parentProfessionName
		recipe.profession = parent and parent ~= "" and parent or info.professionName
	end
	local item, reagents
	ok, item, reagents = pcall(Schematic, recipe.id)
	if ok then recipe.item, recipe.reagents = item, reagents end
end

-- The entry for a spell by ID or name; by name only when one of them has no ID (ranks share a name).
local function Find(list, id, name)
	for _, entry in ipairs(list) do
		if (id and entry.id == id) or (name and entry.name == name and not (id and entry.id)) then return entry end
	end
end

local function Add(list, id, name)
	local entry = Find(list, id, name)
	if not entry then
		entry = {}
		list[#list + 1] = entry
	end
	entry.id, entry.name = entry.id or id, entry.name or name
	return entry
end

local function Learn(id, name, recipe)
	if id and not name then
		local ok, spell = pcall(C_Spell.GetSpellName, id)
		name = ok and spell or nil
	end
	if not (id or name) then return end
	local spell = Add(session, id, name)
	spell.recipe = spell.recipe or recipe
	if not spell.recipe then return end
	for _, profession in pairs(DB().recipes[CharacterKey()] or {}) do
		if Lists(profession.recipes, spell) then return end -- a profession window listed it already
	end
	local learned = DB().learned
	learned[CharacterKey()] = learned[CharacterKey()] or {}
	local entry = Add(learned[CharacterKey()], spell.id, spell.name)
	entry.at = entry.at or time()
	if entry.id and not entry.reagents then Describe(entry) end
end

local function LearnFromChat(text)
	for _, line in ipairs(LEARN_LINES) do
		local what = text:match(line[1])
		if what then
			-- Newer clients write a link: |cff71d5ff|Hspell:3816:0|h[Cured Light Hide]|h|r
			local id = tonumber(what:match("|Hspell:(%d+)") or what:match("|Henchant:(%d+)"))
			return Learn(id, what:match("|h%[(.-)%]|h") or what, line[2])
		end
	end
end

-- For plans: what was learned since login (class spells too), then the recipes learned since a
-- profession window last listed them; { id, name }, either possibly missing.
function Knowledge.Learned()
	local list = {}
	for _, spell in ipairs(session) do list[#list + 1] = spell end
	for _, recipe in ipairs(DB().learned[CharacterKey()] or {}) do list[#list + 1] = recipe end
	return list
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

-- What an item binds to (Enum.ItemBind), if the client has its data; asking makes it load for the rescan.
local function BindType(itemID)
	if not itemID then return nil end
	local ok, name, _, _, _, _, _, _, _, _, _, _, _, _, bind = pcall(C_Item.GetItemInfo, itemID)
	if ok and name and type(bind) == "number" then return bind end
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
			item.bind = BindType(item.id)
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
	"NEW_RECIPE_LEARNED", "LEARNED_SPELL_IN_SKILL_LINE", "CHAT_MSG_SYSTEM",
	"MERCHANT_SHOW", "MERCHANT_UPDATE", "MERCHANT_CLOSED" }) do
	events:RegisterEvent(event)
end
-- A learn that fails to be read (e.g. a secret value) is left to the other signals.
events:SetScript("OnEvent", function(_, event, arg1)
	if event == "NEW_RECIPE_LEARNED" or event == "LEARNED_SPELL_IN_SKILL_LINE" then
		if type(arg1) == "number" then pcall(Learn, arg1, nil, event == "NEW_RECIPE_LEARNED") end
	elseif event == "CHAT_MSG_SYSTEM" then
		if type(arg1) == "string" then pcall(LearnFromChat, arg1) end
	elseif event == "MERCHANT_SHOW" then
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
