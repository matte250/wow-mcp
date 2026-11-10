-- Profession windows (C_TradeSkillUI), vendors (the merchant API) and item names (Knowledge.lua).

-- Item names the client knows. Like the real one, it has to load an item's data first: the
-- first lookup of an item it hasn't seen returns nil and loads it.
client.itemNames = {
	[159] = "Refreshing Spring Water", [2320] = "Coarse Thread", [2318] = "Light Leather", [783] = "Light Hide",
	[4289] = "Salt", [2678] = "Mild Spices", [5976] = "Guild Tabard", [2512] = "Rough Arrow", [4470] = "Simple Wood",
	[4232] = "Medium Hide", [2319] = "Medium Leather", [2321] = "Fine Thread", [4231] = "Cured Light Hide",
	[4233] = "Cured Medium Hide", [4237] = "Handstitched Leather Belt", [4249] = "Dark Leather Belt",
}
client.itemsLoaded = { [159] = true, [2320] = true, [2318] = true, [783] = true, [2678] = true, [2512] = true }
C_Item = C_Item or {}
function C_Item.GetItemNameByID(itemID)
	if client.itemsLoaded[itemID] then return client.itemNames[itemID] end
	client.itemsLoaded[itemID] = true
end

---------------------------------------------------------------------------
-- Professions: the recipe list is only there while the window is open, and only after the
-- first TRADE_SKILL_LIST_UPDATE (it's empty at TRADE_SKILL_SHOW). What a recipe makes and needs,
-- and its profession, are the client's static data: there with no window open.
---------------------------------------------------------------------------

client.professions = {
	Leatherworking = {
		info = { professionID = 165, professionName = "Leatherworking", skillLevel = 60, maxSkillLevel = 75 },
		recipes = {
			{ id = 2149, name = "Handstitched Leather Boots", item = 2300, learned = true, difficulty = 1, reagents = { { 2318, 2 }, { 2320, 1 } } },
			{ id = 2881, name = "Light Leather", item = 2318, learned = true, difficulty = 3, reagents = { { 783, 3 } } },
			{ id = 3753, name = "Handstitched Leather Belt", item = 4237, learned = false, difficulty = 0, reagents = { { 2318, 6 }, { 2320, 1 } } },
			{ id = 2152, name = "Light Armor Kit", item = 2304, learned = true, difficulty = 0, reagents = { { 2318, 1 } } },
			{ id = 3816, name = "Cured Light Hide", item = 4231, learned = false, difficulty = 0, reagents = { { 783, 1 }, { 4289, 1 } } },
			{ id = 3817, name = "Cured Medium Hide", item = 4233, learned = false, difficulty = 0, reagents = { { 4232, 1 }, { 4289, 1 } } },
			{ id = 3766, name = "Dark Leather Belt", item = 4249, learned = false, difficulty = 0, reagents = { { 2319, 5 }, { 2321, 2 } } },
		},
	},
	Cooking = {
		info = { professionID = 185, professionName = "Cooking", skillLevel = 40, maxSkillLevel = 75 },
		recipes = {
			{ id = 2538, name = "Charred Wolf Meat", item = 2679, learned = true, difficulty = 2, reagents = { { 2672, 1 } } },
		},
	},
}

client.tradeSkill = nil -- { profession, ready, linked }

local function Recipe(id)
	local skill = client.tradeSkill
	for _, recipe in ipairs(skill and skill.profession.recipes or {}) do
		if recipe.id == id then return recipe end
	end
end

-- Any profession's recipe, and the profession.
local function AnyRecipe(id)
	for _, profession in pairs(client.professions) do
		for _, recipe in ipairs(profession.recipes) do
			if recipe.id == id then return recipe, profession end
		end
	end
end

local function RecipeIDs(learnedOnly)
	local ids = {}
	local skill = client.tradeSkill
	if not (skill and skill.ready) then return ids end
	for _, recipe in ipairs(skill.profession.recipes) do
		if recipe.learned or not learnedOnly then ids[#ids + 1] = recipe.id end
	end
	return ids
end

local function ProfessionInfo()
	local skill = client.tradeSkill
	if not skill then return { professionID = 0, professionName = "", skillLevel = 0, maxSkillLevel = 0 } end
	local info = {}
	for k, v in pairs(skill.profession.info) do info[k] = v end
	return info
end

C_TradeSkillUI = {
	GetAllRecipeIDs = function() return RecipeIDs(false) end,
	GetFilteredRecipeIDs = function() return RecipeIDs(true) end, -- unlearned are hidden by default on Forever
	GetChildProfessionInfo = ProfessionInfo,
	GetBaseProfessionInfo = ProfessionInfo,
	GetRecipeInfo = function(id)
		local recipe = Recipe(id)
		return recipe and {
			recipeID = id, name = recipe.name, learned = recipe.learned, relativeDifficulty = recipe.difficulty,
			categoryID = 1, maxTrivialLevel = 100, itemLevel = 0, numSkillUps = 1, canSkillUp = true,
		}
	end,
	GetRecipeSchematic = function(id)
		local recipe = AnyRecipe(id)
		local slots = {}
		for i, reagent in ipairs(recipe and recipe.reagents or {}) do
			slots[i] = { reagents = { { itemID = reagent[1] } }, quantityRequired = reagent[2], reagentType = 1, required = true, slotIndex = i }
		end
		return { recipeID = id, name = recipe and recipe.name or "", outputItemID = recipe and recipe.item,
			reagentSlotSchematics = slots, isRecraft = false }
	end,
	GetProfessionInfoByRecipeID = function(id)
		local _, profession = AnyRecipe(id)
		local info = { professionID = 0, professionName = "", skillLevel = 0, maxSkillLevel = 0 }
		for k, v in pairs(profession and profession.info or {}) do info[k] = v end
		return info
	end,
	IsTradeSkillLinked = function()
		local skill = client.tradeSkill
		return skill and skill.linked and true or false, skill and skill.linked or nil
	end,
	IsTradeSkillGuild = function() return false end,
	IsTradeSkillGuildMember = function() return false end,
	IsNPCCrafting = function() return false end,
	IsDataSourceChanging = function() return false end,
}

-- opts.linked: someone else's profession linked in chat.
function client.OpenProfession(name, opts)
	client.tradeSkill = { profession = assert(client.professions[name], name), ready = false, linked = opts and opts.linked }
	client.Fire("TRADE_SKILL_SHOW")
	C_Timer.After(0.5, function()
		if not client.tradeSkill then return end
		client.tradeSkill.ready = true
		client.Fire("TRADE_SKILL_LIST_UPDATE")
	end)
end

function client.CloseProfession()
	client.tradeSkill = nil
	client.Fire("TRADE_SKILL_CLOSE")
end

---------------------------------------------------------------------------
-- Vendors: item names may only arrive with a MERCHANT_UPDATE after MERCHANT_SHOW.
---------------------------------------------------------------------------

client.merchant = nil -- { name, npc, items = { { id, price, stack, stock, locked, costs = { { value, currency } } } } }

local function MerchantItem(index)
	return client.merchant and client.merchant.items[index]
end

function GetMerchantNumItems() return client.merchant and #client.merchant.items or 0 end
function GetMerchantItemID(index)
	local item = MerchantItem(index)
	return item and item.id
end
function GetMerchantItemCostInfo(index)
	local item = MerchantItem(index)
	return item and item.costs and #item.costs or 0
end
function GetMerchantItemCostItem(index, n)
	local cost = MerchantItem(index) and MerchantItem(index).costs and MerchantItem(index).costs[n]
	if not cost then return nil end
	return 136000 + n, cost[1], nil, cost[2]
end

C_MerchantFrame = C_MerchantFrame or {}
function C_MerchantFrame.GetItemInfo(index)
	local item = MerchantItem(index)
	if not item then return nil end
	return {
		name = client.itemsLoaded[item.id] and client.itemNames[item.id] or nil, texture = 132000 + index,
		price = item.price, stackCount = item.stack or 1, numAvailable = item.stock or -1,
		isPurchasable = not item.locked, isUsable = true, hasExtendedCost = item.costs ~= nil, isQuestStartItem = false,
	}
end

-- UnitName and UnitGUID know the "npc" unit while a vendor window is open; installed on first
-- use, after the other API files (UnitName comes from player.lua).
local function KnowNpc()
	if client.npcUnit then return end
	client.npcUnit = true
	local UnitNameBase, UnitGUIDBase = UnitName, UnitGUID
	function UnitName(unit)
		if unit == "npc" then return client.merchant and client.merchant.name, nil end
		return UnitNameBase(unit)
	end
	function UnitGUID(unit)
		if unit == "npc" then
			return client.merchant and string.format("Creature-0-5250-0-1-%d-00002B7C1E", client.merchant.npc) or nil
		end
		return UnitGUIDBase and UnitGUIDBase(unit)
	end
end

function client.OpenMerchant(vendor)
	KnowNpc()
	client.merchant = vendor
	client.Fire("MERCHANT_SHOW")
	C_Timer.After(0.5, function()
		if client.merchant ~= vendor then return end
		for _, item in ipairs(vendor.items) do client.itemsLoaded[item.id] = true end
		client.Fire("MERCHANT_UPDATE")
	end)
end

function client.CloseMerchant()
	client.merchant = nil
	client.Fire("MERCHANT_CLOSED")
end
