-- Bags, equipped gear, the bank and item data (Inventory.lua).

Enum.BagIndex = {
	Accountbanktab = -3, Characterbanktab = -2, Keyring = -1, Backpack = 0, Bag_1 = 1, Bag_2 = 2, Bag_3 = 3,
	Bag_4 = 4, ReagentBag = 5, CharacterBankTab_1 = 6, AccountBankTab_1 = 15,
}
Enum.BankType = { Character = 0, Guild = 1, Account = 2 }

time = os.time
function GetRealmName() return "Firemaw" end

-- Item data. An uncached item has no name anywhere until something asks for it; then it
-- loads a moment later, as on the real client.
client.items = {
	[6948] = { name = "Hearthstone", quality = 1, level = 1, class = 15 },
	[2589] = { name = "Linen Cloth", quality = 1, level = 5, class = 7, sell = 13 },
	[2318] = { name = "Light Leather", quality = 1, level = 10, class = 7, sell = 15 },
	[4865] = { name = "Ruined Pelt", quality = 0, level = 1, class = 15, sell = 5 },
	[159] = { name = "Refreshing Spring Water", quality = 1, level = 5, class = 0, sell = 1 },
	[2512] = { name = "Rough Arrow", quality = 1, level = 5, class = 6 },
	[9789] = { name = "Raider's Legguards", quality = 2, level = 21, class = 4, sell = 304 },
	[3164] = { name = "Discolored Worg Heart", quality = 1, level = 1, class = 12, uncached = true },
	[5396] = { name = "Key to Searing Gorge", quality = 1, level = 1, class = 13 },
	[4243] = { name = "Fine Leather Tunic", quality = 2, level = 17, class = 4, sell = 220 },
	[2302] = { name = "Handstitched Leather Pants", quality = 1, level = 10, class = 4, sell = 40 },
	[12282] = { name = "Worn Battleaxe", quality = 1, level = 2, class = 2, sell = 9 },
	[2504] = { name = "Worn Shortbow", quality = 1, level = 2, class = 2, sell = 7 },
	[45] = { name = "Squire's Shirt", quality = 1, level = 1, class = 4 },
}

-- Containers by bag ID: { size, name, family, slots = { [slot] = { id, count, suffix } } }.
-- -1 is the keyring; 6 and up are bank tabs, only readable at the banker.
client.bags = {
	[-1] = { size = 4, slots = { [1] = { id = 5396 } } },
	[0] = { size = 16, name = "Backpack", slots = {
		[1] = { id = 6948 }, [2] = { id = 2589, count = 7 }, [3] = { id = 4865, count = 2 },
		[4] = { id = 9789, suffix = " of the Bear" }, [5] = { id = 3164 }, [6] = { id = 159, count = 5 },
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

local COLORS = { [0] = "9d9d9d", "ffffff", "1eff00", "0070dd", "a335ee" }

local function Cached(itemID)
	local item = client.items[itemID]
	if item.uncached and not item.loading then
		item.loading = true
		C_Timer.After(0, function() item.uncached, item.loading = nil, nil end)
	end
	return not item.uncached
end

local function Link(itemID, suffix)
	local item = client.items[itemID]
	local name = Cached(itemID) and item.name .. (suffix or "") or ""
	return string.format("|cff%s|Hitem:%d::::::::12:::::::::|h[%s]|h|r", COLORS[item.quality], itemID, name)
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

-- Takes an item ID or link; nil until the item's data is cached (asking starts loading it).
C_Item = {
	GetItemInfo = function(item)
		local itemID = type(item) == "number" and item or tonumber(tostring(item):match("item:(%d+)"))
		local info = client.items[itemID]
		if not info or not Cached(itemID) then return nil end
		return info.name, Link(itemID), info.quality, info.level, 1, "", "", 1, "", 134400, info.sell or 0, info.class, 0
	end,
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
