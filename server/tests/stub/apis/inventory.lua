-- Bags, equipped gear, the bank, item data, stats and item tooltips (Inventory.lua).

Enum.BagIndex = {
	Accountbanktab = -3, Characterbanktab = -2, Keyring = -1, Backpack = 0, Bag_1 = 1, Bag_2 = 2, Bag_3 = 3,
	Bag_4 = 4, ReagentBag = 5, CharacterBankTab_1 = 6, AccountBankTab_1 = 15,
}
Enum.BankType = { Character = 0, Guild = 1, Account = 2 }
Enum.TooltipDataLineType = { None = 0, Blank = 1, SellPrice = 11, ItemBinding = 20, EquipSlot = 21, ItemName = 22 }

RETRIEVING_ITEM_INFO = "Retrieving item information"
ARMOR_TEMPLATE = "%s Armor"
SELL_PRICE = "Sell Price"

-- Item data. An uncached item has no name anywhere until something asks for it; then it loads
-- loadTime seconds later (default: the next frame), as on the real client. Gear has a class,
-- subclass and equip slot, and may have armor, stats (keyed as GetItemStats keys them),
-- weapon damage { low, high, speed }, a required level and effects (green tooltip lines).
client.items = {
	[6948] = { name = "Hearthstone", quality = 1, level = 1, class = 15 },
	[2589] = { name = "Linen Cloth", quality = 1, level = 5, class = 7, sell = 13 },
	[2318] = { name = "Light Leather", quality = 1, level = 10, class = 7, sell = 15 },
	[4865] = { name = "Ruined Pelt", quality = 0, level = 1, class = 15, sell = 5 },
	[159] = { name = "Refreshing Spring Water", quality = 1, level = 5, class = 0, sell = 1 },
	[2512] = { name = "Rough Arrow", quality = 1, level = 5, class = 6, subclass = 2, slot = "INVTYPE_AMMO" },
	[9789] = { name = "Raider's Legguards", quality = 2, level = 21, class = 4, subclass = 2, slot = "INVTYPE_LEGS",
		sell = 304, armor = 72, required = 16 },
	[3164] = { name = "Discolored Worg Heart", quality = 1, level = 1, class = 12, uncached = true },
	[5396] = { name = "Key to Searing Gorge", quality = 1, level = 1, class = 13 },
	[4243] = { name = "Fine Leather Tunic", quality = 2, level = 17, class = 4, subclass = 2, slot = "INVTYPE_CHEST",
		sell = 220, armor = 59, stats = { ITEM_MOD_STAMINA_SHORT = 3 }, required = 12 },
	[2302] = { name = "Handstitched Leather Pants", quality = 1, level = 10, class = 4, subclass = 2,
		slot = "INVTYPE_LEGS", sell = 40, armor = 24, required = 5 },
	[12282] = { name = "Worn Battleaxe", quality = 1, level = 2, class = 2, subclass = 1, slot = "INVTYPE_2HWEAPON",
		sell = 9, damage = { 7, 11, 2.9 } },
	[2504] = { name = "Worn Shortbow", quality = 1, level = 2, class = 2, subclass = 2, slot = "INVTYPE_RANGED",
		sell = 7, damage = { 3, 6, 2.3 } },
	[45] = { name = "Squire's Shirt", quality = 1, level = 1, class = 4, subclass = 0, slot = "INVTYPE_BODY" },
	-- Not the player's (stats made up for the tests).
	[4253] = { name = "Toughened Leather Gloves", quality = 2, level = 30, class = 4, subclass = 2,
		slot = "INVTYPE_HAND", sell = 512, armor = 49, required = 25, bind = "Binds when equipped",
		stats = { ITEM_MOD_AGILITY_SHORT = 6, ITEM_MOD_STRENGTH_SHORT = 4, ITEM_MOD_ATTACK_POWER_SHORT = 10 },
		effects = { "Equip: +10 Attack Power." } },
}

-- Random suffixes by ID, as in item links.
client.suffixes = {
	[1102] = { name = " of the Bear", stats = { ITEM_MOD_STRENGTH_SHORT = 3, ITEM_MOD_STAMINA_SHORT = 3 } },
}

-- Containers by bag ID: { size, name, family, slots = { [slot] = { id, count, suffix } } }.
-- -1 is the keyring; 6 and up are bank tabs, only readable at the banker.
client.bags = {
	[-1] = { size = 4, slots = { [1] = { id = 5396 } } },
	[0] = { size = 16, name = "Backpack", slots = {
		[1] = { id = 6948 }, [2] = { id = 2589, count = 7 }, [3] = { id = 4865, count = 2 },
		[4] = { id = 9789, suffix = 1102 }, [5] = { id = 3164 }, [6] = { id = 159, count = 5 },
	} },
	[1] = { size = 6, name = "Small Brown Pouch", slots = {} },
	[2] = { size = 8, name = "Light Quiver", family = 1, slots = { [1] = { id = 2512, count = 200 }, [2] = { id = 2512, count = 143 } } },
	[6] = { size = 24, slots = { [1] = { id = 2589, count = 20 }, [3] = { id = 2318, count = 10 } } },
	[7] = { size = 6, slots = { [2] = { id = 4865, count = 3 } } },
}

-- Equipped by inventory slot (0 is ammo, which counts what's in the bags).
client.equipment = {
	[0] = { id = 2512 },
	[4] = { id = 45 },
	[5] = { id = 4243, durability = 70, maxDurability = 85 },
	[7] = { id = 2302, durability = 0, maxDurability = 55 },
	[16] = { id = 12282, durability = 25, maxDurability = 25 },
	[18] = { id = 2504, durability = 30, maxDurability = 35 },
}
client.atBank = false
-- Set to leave armor out of GetItemStats (the addon then reads it off the tooltip).
client.armorNotInStats = false

local COLORS = { [0] = "9d9d9d", "ffffff", "1eff00", "0070dd", "a335ee" }
local CLASSES = { [0] = "Consumable", [2] = "Weapon", [4] = "Armor", [6] = "Projectile", [7] = "Trade Goods",
	[12] = "Quest", [13] = "Key", [15] = "Miscellaneous" }
local SUBCLASSES = { [2] = { [1] = "Two-Handed Axes", [2] = "Bows" }, [4] = { [0] = "Miscellaneous", [2] = "Leather" },
	[6] = { [2] = "Arrow" } }

local function Cached(itemID)
	local item = client.items[itemID]
	if item.uncached and not item.loading then
		item.loading = true
		C_Timer.After(item.loadTime or 0, function()
			item.uncached, item.loading = nil, nil
			if item.requested then
				item.requested = nil
				client.Fire("ITEM_DATA_LOAD_RESULT", itemID, true)
			end
		end)
	end
	return not item.uncached
end

-- item:ID:enchant:gem1:gem2:gem3:gem4:suffix:unique:linkLevel:...
local function Link(itemID, suffix)
	local item = client.items[itemID]
	local name = Cached(itemID) and item.name .. (suffix and client.suffixes[suffix].name or "") or ""
	return string.format("|cff%s|Hitem:%d::::::%s::12:::::::::|h[%s]|h|r", COLORS[item.quality], itemID,
		suffix or "", name)
end

-- An item ID, or a link ("item:123" or a full one): itemID, suffix ID.
local function ParseItem(item)
	if type(item) == "number" then return item end
	local itemID, rest = tostring(item):match("item:(%d+)(.*)")
	return tonumber(itemID), tonumber(rest and rest:match("^:[^:]*:[^:]*:[^:]*:[^:]*:[^:]*:(%d+)"))
end

local function Bag(bag)
	if bag >= Enum.BagIndex.CharacterBankTab_1 and not client.atBank then return nil end
	return client.bags[bag]
end

C_Container = {
	-- The UI asks GetKeyRingSize() for the keyring instead.
	GetContainerNumSlots = function(bag)
		local b = bag ~= Enum.BagIndex.Keyring and Bag(bag)
		return b and b.size or 0
	end,
	-- nil for an empty slot.
	GetContainerItemInfo = function(bag, slot)
		local s = Bag(bag) and Bag(bag).slots[slot]
		if not s then return nil end
		local item = client.items[s.id]
		return {
			iconFileID = 134400, stackCount = s.count or 1, isLocked = false, quality = item.quality,
			isReadable = false, hasLoot = false, hyperlink = Link(s.id, s.suffix), isFiltered = false,
			hasNoValue = (item.sell or 0) == 0, itemID = s.id, isBound = false,
			itemName = Cached(s.id) and item.name or "",
		}
	end,
	GetBagName = function(bag) return Bag(bag) and Bag(bag).name end,
	GetContainerNumFreeSlots = function(bag)
		local b = Bag(bag)
		if not b then return 0, nil end
		local used = 0
		for _ in pairs(b.slots) do used = used + 1 end
		return b.size - used, b.family or 0
	end,
}

function GetKeyRingSize() return client.bags[-1] and client.bags[-1].size or 0 end

local function TypeNames(info)
	return CLASSES[info.class] or "", SUBCLASSES[info.class] and SUBCLASSES[info.class][info.subclass or 0] or ""
end

-- Take an item ID or link. GetItemInfo is nil until the item's data is cached (asking starts
-- loading it); GetItemInfoInstant reads the client's static data, nil only for unknown items.
C_Item = {
	GetItemInfo = function(item)
		local itemID = ParseItem(item)
		local info = client.items[itemID]
		if not info or not Cached(itemID) then return nil end
		local typeName, subtypeName = TypeNames(info)
		return info.name, Link(itemID), info.quality, info.level, info.required or 0, typeName, subtypeName, 1,
			info.slot or "", 134400, info.sell or 0, info.class, info.subclass or 0
	end,
	GetItemInfoInstant = function(item)
		local itemID = ParseItem(item)
		local info = client.items[itemID]
		if not info then return nil end
		local typeName, subtypeName = TypeNames(info)
		return itemID, typeName, subtypeName, info.slot or "", 134400, info.class, info.subclass or 0
	end,
	IsItemDataCachedByID = function(item)
		local info = client.items[ParseItem(item)]
		return info ~= nil and not info.uncached
	end,
	-- Loads the item; ITEM_DATA_LOAD_RESULT says when, or that there's no such item.
	RequestLoadItemDataByID = function(item)
		local itemID = ParseItem(item)
		local info = client.items[itemID]
		if not info or Cached(itemID) then
			C_Timer.After(0, function() client.Fire("ITEM_DATA_LOAD_RESULT", itemID, info ~= nil) end)
		else
			info.requested = true
		end
	end,
	-- { [global string name] = value }: armor as RESISTANCE0_NAME, a weapon's DPS, a suffix's stats.
	GetItemStats = function(link)
		local itemID, suffix = ParseItem(link)
		local info = client.items[itemID]
		if not info or not Cached(itemID) then return nil end
		local stats = {}
		for key, value in pairs(info.stats or {}) do stats[key] = value end
		for key, value in pairs(suffix and client.suffixes[suffix].stats or {}) do stats[key] = (stats[key] or 0) + value end
		if info.armor and not client.armorNotInStats then stats.RESISTANCE0_NAME = info.armor end
		if info.damage then stats.ITEM_MOD_DAMAGE_PER_SECOND_SHORT = (info.damage[1] + info.damage[2]) / 2 / info.damage[3] end
		return stats
	end,
}

-- Tooltip data as C_TooltipInfo builds it: { type, id, lines = { { type, leftText, leftColor,
-- rightText, rightColor, price } } }, coloured like the game (red: a requirement not met).
local WHITE, RED, GREEN = { r = 1, g = 1, b = 1 }, { r = 1, g = 0.125, b = 0.125 }, { r = 0.118, g = 1, b = 0.118 }
local QUALITY_COLORS = { [0] = { r = 0.62, g = 0.62, b = 0.62 }, WHITE, { r = 0.118, g = 1, b = 0 },
	{ r = 0, g = 0.439, b = 0.867 }, { r = 0.639, g = 0.208, b = 0.933 } }
local SLOT_NAMES = { INVTYPE_CHEST = "Chest", INVTYPE_LEGS = "Legs", INVTYPE_HAND = "Hands", INVTYPE_BODY = "Shirt",
	INVTYPE_2HWEAPON = "Two-Hand", INVTYPE_RANGED = "Ranged", INVTYPE_AMMO = "Projectile" }
local STAT_LINES = { { "ITEM_MOD_STRENGTH_SHORT", "Strength" }, { "ITEM_MOD_AGILITY_SHORT", "Agility" },
	{ "ITEM_MOD_STAMINA_SHORT", "Stamina" }, { "ITEM_MOD_INTELLECT_SHORT", "Intellect" }, { "ITEM_MOD_SPIRIT_SHORT", "Spirit" } }

local function TooltipData(itemID, suffix)
	local info = client.items[itemID]
	if not info then return nil end
	local T = Enum.TooltipDataLineType
	local lines = {}
	local function Add(text, color, line)
		line = line or {}
		line.type, line.leftText, line.leftColor = line.type or T.None, text, color or WHITE
		lines[#lines + 1] = line
	end
	if not Cached(itemID) then
		Add(RETRIEVING_ITEM_INFO, RED)
		return { type = Enum.TooltipDataType.Item, id = itemID, lines = lines }
	end
	Add(info.name .. (suffix and client.suffixes[suffix].name or ""), QUALITY_COLORS[info.quality], { type = T.ItemName })
	if info.bind then Add(info.bind, WHITE, { type = T.ItemBinding }) end
	if SLOT_NAMES[info.slot] then
		local _, subtypeName = TypeNames(info)
		Add(SLOT_NAMES[info.slot], WHITE, { type = T.EquipSlot, rightText = subtypeName, rightColor = WHITE })
	end
	if info.damage then
		local low, high, speed = info.damage[1], info.damage[2], info.damage[3]
		Add(string.format("%d - %d Damage", low, high), WHITE, { rightText = string.format("Speed %.2f", speed), rightColor = WHITE })
		Add(string.format("(%.1f damage per second)", (low + high) / 2 / speed))
	end
	if info.armor then Add(ARMOR_TEMPLATE:format(info.armor)) end
	local stats = C_Item.GetItemStats(Link(itemID, suffix))
	for _, stat in ipairs(STAT_LINES) do
		if stats[stat[1]] then Add(string.format("+%d %s", stats[stat[1]], stat[2])) end
	end
	if info.required then Add("Requires Level " .. info.required, UnitLevel("player") < info.required and RED or WHITE) end
	for _, effect in ipairs(info.effects or {}) do Add(effect, GREEN) end
	if (info.sell or 0) > 0 then Add(SELL_PRICE .. ":", WHITE, { type = T.SellPrice, price = info.sell }) end
	return { type = Enum.TooltipDataType.Item, id = itemID, lines = lines }
end

C_TooltipInfo = {
	GetItemByID = function(itemID) return TooltipData(itemID) end,
	GetHyperlink = function(link) return TooltipData(ParseItem(link)) end,
}

local function Equipped(unit, slot) return unit == "player" and client.equipment[slot] end

function GetInventoryItemID(unit, slot) return Equipped(unit, slot) and Equipped(unit, slot).id end
function GetInventoryItemLink(unit, slot) return Equipped(unit, slot) and Link(Equipped(unit, slot).id) end
function GetInventoryItemQuality(unit, slot)
	return Equipped(unit, slot) and client.items[Equipped(unit, slot).id].quality
end
function GetInventoryItemDurability(slot)
	local e = client.equipment[slot]
	if e and e.maxDurability then return e.durability, e.maxDurability end
end
-- For ammo: how many of it are in the bags.
function GetInventoryItemCount(unit, slot)
	local e = Equipped(unit, slot)
	if not e then return 0 end
	if slot ~= 0 then return 1 end
	local total = 0
	for bag = 0, 4 do
		for _, s in pairs(client.bags[bag] and client.bags[bag].slots or {}) do
			if s.id == e.id then total = total + (s.count or 1) end
		end
	end
	return total
end

-- Bank tabs: the first is the base bank, the rest bank bags. Viewable only at the banker.
C_Bank = {
	CanViewBank = function(bankType) return client.atBank and bankType == Enum.BankType.Character end,
	FetchPurchasedBankTabData = function(bankType)
		local tabs = {}
		if bankType == Enum.BankType.Character then
			for bag = 6, 14 do
				if client.bags[bag] then tabs[#tabs + 1] = { ID = bag, bankType = bankType, name = "", icon = 0, depositFlags = 0 } end
			end
		end
		return tabs
	end,
}

function client.OpenBank()
	client.atBank = true
	client.Fire("BANKFRAME_OPENED")
end

function client.CloseBank()
	client.Fire("BANKFRAME_CLOSED")
	client.atBank = false
end
