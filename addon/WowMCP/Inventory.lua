-- WowMCP inventory: bags, equipped gear with durability, and the bank as last seen (request kind ITEMS).
--
-- The bank can only be read at the banker, so whenever it's open we save it, with the time,
-- per character in WowMCPDB.bank (SavedVariables are shared by all characters on the account).
-- Forever uses the modern bank: the character bank is bag IDs 6+ (one per purchased tab; the
-- first tab is the base bank, the rest are bank bags), read like any other container.

local _, ns = ...

local WEAPON, ARMOR = 2, 4 -- Enum.ItemClass: the classes where item level matters
local AMMO, LAST_EQUIPPED = 0, 19 -- INVSLOT_AMMO, INVSLOT_TABARD

-- The name as linked: includes random suffixes ("of the Bear"). Empty until the item's data loads.
local function LinkName(link)
	local name = link and link:match("|h%[(.-)%]|h")
	return name ~= "" and name or nil
end

-- Name, item level, vendor price and class, if the client has the item's data. If not, this
-- call makes the client load it, so it's there next time.
local function Details(item)
	local ok, name, _, _, level, _, _, _, _, _, _, sell, class = pcall(C_Item.GetItemInfo, item)
	if ok and name then return name, level, sell, class end
end

-- A container slot as { slot, id, name, count, quality, ilvl, sell }, or nil if it's empty.
-- count is left out when 1, ilvl for anything but gear, sell for what vendors won't buy.
local function Item(bag, slot)
	local info = C_Container.GetContainerItemInfo(bag, slot)
	if not (info and info.itemID) then return nil end
	local name, level, sell, class = Details(info.hyperlink or info.itemID)
	local item = {
		slot = slot,
		id = info.itemID,
		name = LinkName(info.hyperlink) or (info.itemName ~= "" and info.itemName) or name,
		quality = info.quality,
	}
	if (info.stackCount or 1) > 1 then item.count = info.stackCount end
	if class == WEAPON or class == ARMOR then item.ilvl = level end
	if sell and sell > 0 and not info.hasNoValue then item.sell = sell end
	return item
end

local function Container(bag, size)
	local items = {}
	for slot = 1, size do
		items[#items + 1] = Item(bag, slot)
	end
	return { bag = bag, size = size, items = items }
end

-- Backpack, bags (special ones such as quivers marked) and the keyring, if this client has one.
local function Bags()
	local bags = {}
	local keyring = type(GetKeyRingSize) == "function" and GetKeyRingSize() or 0
	if keyring > 0 then bags[1] = Container(Enum.BagIndex.Keyring, keyring) end
	for bag = Enum.BagIndex.Backpack, Enum.BagIndex.ReagentBag do
		local size = C_Container.GetContainerNumSlots(bag)
		if size > 0 then
			local container = Container(bag, size)
			container.name = C_Container.GetBagName(bag)
			local _, family = C_Container.GetContainerNumFreeSlots(bag)
			if family and family ~= 0 then container.special = true end
			bags[#bags + 1] = container
		end
	end
	return bags
end

-- Equipped items by inventory slot (0 is ammo, with how much is left).
local function Equipment()
	local list = {}
	for slot = AMMO, LAST_EQUIPPED do
		local itemID = GetInventoryItemID("player", slot)
		if itemID then
			local link = GetInventoryItemLink("player", slot)
			local name, level = Details(link or itemID)
			local item = {
				slot = slot,
				id = itemID,
				name = LinkName(link) or name,
				quality = GetInventoryItemQuality("player", slot),
				ilvl = level,
			}
			local current, maximum = GetInventoryItemDurability(slot)
			if maximum and maximum > 0 then item.durability, item.maxDurability = current, maximum end
			if slot == AMMO then item.count = GetInventoryItemCount("player", slot) end
			list[#list + 1] = item
		end
	end
	return list
end

---------------------------------------------------------------------------
-- Bank
---------------------------------------------------------------------------

local bankOpen = false

local function Character()
	return tostring(UnitName("player")) .. "-" .. tostring(GetRealmName())
end

local function ReadBank()
	local bags = {}
	local types = { Enum.BankType.Character }
	if C_Bank.CanViewBank(Enum.BankType.Account) then types[2] = Enum.BankType.Account end
	for _, bankType in ipairs(types) do
		for _, tab in ipairs(C_Bank.FetchPurchasedBankTabData(bankType) or {}) do
			local size = C_Container.GetContainerNumSlots(tab.ID)
			if size > 0 then bags[#bags + 1] = Container(tab.ID, size) end
		end
	end
	return bags
end

-- Runs from events, so it must never error; an empty read (data not there yet) keeps the old copy.
local function SaveBank()
	local ok, bags = pcall(ReadBank)
	if ok and #bags > 0 then
		WowMCPDB.bank = WowMCPDB.bank or {}
		WowMCPDB.bank[Character()] = { time = time(), bags = bags }
	end
end

-- false if this character's bank hasn't been seen since the addon was installed.
local function Bank()
	if bankOpen then SaveBank() end
	return WowMCPDB.bank and WowMCPDB.bank[Character()] or false
end

-- BAG_UPDATE_DELAYED comes once after a batch of bag changes (bank tabs are bags too).
local events = CreateFrame("Frame")
for _, event in ipairs({ "BANKFRAME_OPENED", "BANKFRAME_CLOSED", "BAG_UPDATE_DELAYED", "PLAYERBANKSLOTS_CHANGED", "BANK_TABS_CHANGED" }) do
	events:RegisterEvent(event)
end
events:SetScript("OnEvent", function(_, event)
	if event == "BANKFRAME_OPENED" then
		bankOpen = true
	elseif event == "BANKFRAME_CLOSED" then
		bankOpen = false
	end
	if bankOpen then SaveBank() end
end)

ns.RegisterSection(ns.Kinds.ITEMS, "bags", Bags)
ns.RegisterSection(ns.Kinds.ITEMS, "equipment", Equipment)
ns.RegisterSection(ns.Kinds.ITEMS, "bank", Bank)
