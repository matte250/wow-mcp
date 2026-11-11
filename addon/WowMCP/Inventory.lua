-- WowMCP inventory: bags, equipped gear with durability and stats, and the bank as last seen
-- (request kind ITEMS); any item's tooltip and stats by ID (kind ITEM_DETAILS).
--
-- The bank can only be read at the banker, so whenever it's open we save it, with the time,
-- per character in WowMCPDB.bank (SavedVariables are shared by all characters on the account).
-- Forever uses the modern bank: the character bank is bag IDs 6+ (one per purchased tab; the
-- first tab is the base bank, the rest are bank bags), read like any other container. The mailbox is
-- kept the same way, in WowMCPDB.mail (see Mail below).

local _, ns = ...

local WEAPON, ARMOR = 2, 4 -- Enum.ItemClass: the classes where item level matters
local MISC_ARMOR = 0 -- Enum.ItemArmorSubclass.Generic: rings, necks, trinkets, shirts (no armor value)
local AMMO, LAST_EQUIPPED = 0, 19 -- INVSLOT_AMMO, INVSLOT_TABARD
local ARMOR_STAT = "RESISTANCE0_NAME" -- how GetItemStats names armor
local MAX_LOOKUP = 20 -- items per get_item_details

-- The name as linked: includes random suffixes ("of the Bear"). Empty until the item's data loads.
local function LinkName(link)
	local name = link and link:match("|h%[(.-)%]|h")
	return name ~= "" and name or nil
end

-- Name, item level, vendor price, class and subclass, if the client has the item's data. If not,
-- this call makes the client load it, so it's there next time.
local function Details(item)
	local ok, name, _, _, level, _, _, _, _, _, _, sell, class, subclass = pcall(C_Item.GetItemInfo, item)
	if ok and name then return name, level, sell, class, subclass end
end

---------------------------------------------------------------------------
-- Stats, as the game reports them
---------------------------------------------------------------------------

-- The tooltip's armor line ("%s Armor") as a pattern capturing the number.
local ARMOR_LINE = "^" .. (tostring(ARMOR_TEMPLATE or "%s Armor")
	:gsub("[%^%$%(%)%.%[%]%*%+%-%?]", "%%%0")
	:gsub("%%[sd]", "([%%d,%%. ]+)")) .. "$"

-- The number on a tooltip's armor line, or nil for any other line.
local function ArmorIn(text)
	local number = type(text) == "string" and text:match(ARMOR_LINE)
	return number and tonumber((number:gsub("%D", ""))) or nil
end

-- { [key] = value } from GetItemStats, keyed by global string names (ITEM_MOD_STAMINA_SHORT,
-- RESISTANCE0_NAME, ...); the server names them. nil if none. A link carries random suffixes.
local function Stats(link)
	local ok, stats = pcall(function()
		local copy
		for key, value in pairs(C_Item.GetItemStats(link) or {}) do
			if type(key) == "string" and type(value) == "number" and value ~= 0 then
				copy = copy or {}
				copy[key] = value
			end
		end
		return copy
	end)
	return ok and stats or nil
end

-- Takes armor out of the stats: stats (nil if that was all), armor.
local function TakeArmor(stats)
	local armor = stats and stats[ARMOR_STAT]
	if not armor then return stats, nil end
	stats[ARMOR_STAT] = nil
	return next(stats) and stats or nil, armor
end

-- Stats and armor of a weapon or piece of armor, from its link. If the stats don't carry the
-- armor, it's read off the item's tooltip.
local function GearStats(link, class, subclass)
	local stats, armor = TakeArmor(Stats(link))
	if not armor and class == ARMOR and subclass ~= MISC_ARMOR then
		pcall(function()
			for _, line in ipairs(C_TooltipInfo.GetHyperlink(link).lines) do
				armor = armor or ArmorIn(line.leftText)
			end
		end)
	end
	return stats, armor
end

---------------------------------------------------------------------------
-- Bags and equipment
---------------------------------------------------------------------------

-- A container slot as { slot, id, name, count, quality, ilvl, sell, bound, stats, armor }, or nil if
-- it's empty. count is left out when 1; ilvl for anything but gear; stats and armor for anything but
-- gear, and unless asked for (the bank leaves them out to stay small); sell for what vendors won't buy;
-- bound if it's soulbound (Auction.lua's baseline leaves those out, also from the bank as last seen).
local function Item(bag, slot, withStats)
	local info = C_Container.GetContainerItemInfo(bag, slot)
	if not (info and info.itemID) then return nil end
	local name, level, sell, class, subclass = Details(info.hyperlink or info.itemID)
	local item = {
		slot = slot,
		id = info.itemID,
		name = LinkName(info.hyperlink) or (info.itemName ~= "" and info.itemName) or name,
		quality = info.quality,
	}
	if (info.stackCount or 1) > 1 then item.count = info.stackCount end
	if class == WEAPON or class == ARMOR then
		item.ilvl = level
		if withStats and info.hyperlink then item.stats, item.armor = GearStats(info.hyperlink, class, subclass) end
	end
	if sell and sell > 0 and not info.hasNoValue then item.sell = sell end
	if info.isBound then item.bound = true end
	return item
end

local function Container(bag, size, withStats)
	local items = {}
	for slot = 1, size do
		items[#items + 1] = Item(bag, slot, withStats)
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
			local container = Container(bag, size, true)
			container.name = C_Container.GetBagName(bag)
			local _, family = C_Container.GetContainerNumFreeSlots(bag)
			if family and family ~= 0 then container.special = true end
			bags[#bags + 1] = container
		end
	end
	return bags
end

-- Equipped items by inventory slot (0 is ammo, with how much is left), with their stats.
local function Equipment()
	local list = {}
	for slot = AMMO, LAST_EQUIPPED do
		local itemID = GetInventoryItemID("player", slot)
		if itemID then
			local link = GetInventoryItemLink("player", slot)
			local name, level, _, class, subclass = Details(link or itemID)
			local item = {
				slot = slot,
				id = itemID,
				name = LinkName(link) or name,
				quality = GetInventoryItemQuality("player", slot),
				ilvl = level,
			}
			if link and (class == WEAPON or class == ARMOR) then item.stats, item.armor = GearStats(link, class, subclass) end
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

---------------------------------------------------------------------------
-- Mail
---------------------------------------------------------------------------

-- Like the bank, the inbox can only be read at a mailbox, so it's saved per character in WowMCPDB.mail
-- on every MAIL_INBOX_UPDATE while one is open (the inbox arrived, or mail was taken or deleted): after
-- closing, it's what was left. Not at MAIL_SHOW: until the game has fetched the inbox, it still holds the
-- last one, or none. GetInboxText is never called: it marks the mail as read.

local ATTACHMENTS = ATTACHMENTS_MAX_RECEIVE or 16
local MAILBOX = Enum.PlayerInteractionType and Enum.PlayerInteractionType.MailInfo or 17
local INVOICES = { seller = "sold", seller_temp_invoice = "pending", buyer = "won" }
local mailbox -- while one is open: { fetched = the inbox arrived this visit }

-- Auction house mail without an invoice, by its subject as the game words it ("Auction expired: %s").
local AH_SUBJECTS = {}
for kind, template in pairs({
	sold = AUCTION_SOLD_MAIL_SUBJECT or "Auction successful: %s",
	won = AUCTION_WON_MAIL_SUBJECT or "Auction won: %s",
	expired = AUCTION_EXPIRED_MAIL_SUBJECT or "Auction expired: %s",
	cancelled = AUCTION_REMOVED_MAIL_SUBJECT or "Auction cancelled: %s",
	outbid = AUCTION_OUTBID_MAIL_SUBJECT or "Outbid on %s",
}) do
	AH_SUBJECTS[kind] = "^" .. (template:gsub("[%^%$%(%)%.%[%]%*%+%-%?]", "%%%0"):gsub("%%s", ".+")) .. "$"
end

-- A character of this account the addon has seen at its bank or mailbox. Senders on this realm have no realm.
local function OurCharacter(sender)
	local key = sender:find("-", 1, true) and sender or sender .. "-" .. tostring(GetRealmName())
	return (WowMCPDB.bank and WowMCPDB.bank[key] or WowMCPDB.mail and WowMCPDB.mail[key]) and true or false
end

-- { { id, name, count, quality } } of what's attached; count is left out when 1. Taken ones leave gaps.
local function Attachments(index, count)
	local items = {}
	for slot = 1, ATTACHMENTS do
		if #items >= count then break end
		local name, itemID, _, stack, quality = GetInboxItem(index, slot)
		local link = GetInboxItemLink and GetInboxItemLink(index, slot)
		itemID = itemID or (link and tonumber(link:match("item:(%d+)")))
		if itemID then
			local item = { id = itemID, name = LinkName(link) or name or Details(itemID), quality = quality }
			if (stack or 1) > 1 then item.count = stack end
			items[#items + 1] = item
		end
	end
	return items
end

-- A mail as { from, subject, money, cod, expires, read, returned, returns, player, alt, gm, ah, sale, items }:
-- money and cod in copper (left out when 0); expires as a unix time; returns: it goes back to the sender when
-- it expires (else it's deleted, with what's attached); player: another player sent it (alt: one of ours);
-- ah: sold, pending (the money comes later), won, expired, cancelled or outbid; sale: the auction house's
-- invoice { item, count, player, price, deposit, fee, eta } (eta: realm time the pending money comes).
local function Mail(index, now)
	local _, _, sender, subject, money, cod, daysLeft, itemCount, wasRead, wasReturned, _, canReply, isGM =
		GetInboxHeaderInfo(index)
	local mail = { from = sender, subject = subject ~= "" and subject or nil,
		expires = math.floor(now + (daysLeft or 0) * 86400) }
	if (money or 0) > 0 then mail.money = money end
	if (cod or 0) > 0 then mail.cod = cod end
	if wasRead then mail.read = true end
	if wasReturned then mail.returned = true end
	if not InboxItemCanDelete(index) then mail.returns = true end -- the mail window's Return button

	local ok, kind, item, player, bid, _, deposit, fee, _, etaHour, etaMin, count = pcall(GetInboxInvoiceInfo, index)
	if ok and INVOICES[kind] then
		mail.ah = INVOICES[kind]
		mail.sale = { item = item, player = player, price = bid }
		if (count or 1) > 1 then mail.sale.count = count end
		if (deposit or 0) > 0 then mail.sale.deposit = deposit end
		if (fee or 0) > 0 then mail.sale.fee = fee end
		if kind == "seller_temp_invoice" and etaHour then mail.sale.eta = string.format("%02d:%02d", etaHour, etaMin or 0) end
	elseif isGM then
		mail.gm = true
	elseif canReply and sender then
		-- Mail that can be answered is a player's, whatever its subject says.
		mail.player = true
		if OurCharacter(sender) then mail.alt = true end
	elseif subject then
		for ah, pattern in pairs(AH_SUBJECTS) do
			if subject:match(pattern) then mail.ah = ah break end
		end
	end
	if (itemCount or 0) > 0 then mail.items = Attachments(index, itemCount) end
	return mail
end

-- { time, total, mails }: total counts the mail the inbox doesn't show yet (it shows at most 50 at a time).
local function ReadInbox()
	local shown, total = GetInboxNumItems()
	local now, mails = time(), {}
	for index = 1, shown do mails[index] = Mail(index, now) end
	return { time = now, total = math.max(total or 0, shown), mails = mails }
end

-- Runs from events, so it must never error.
local function SaveMail()
	local ok, inbox = pcall(ReadInbox)
	if ok then
		WowMCPDB.mail = WowMCPDB.mail or {}
		WowMCPDB.mail[Character()] = inbox
	end
end

-- { inbox = this character's inbox as last seen (absent if never), open = at a mailbox now, newMail = who
-- the game's new-mail notice names (up to three; can be empty), absent without new mail }.
local function MailSection()
	if mailbox and mailbox.fetched then SaveMail() end
	local section = { inbox = WowMCPDB.mail and WowMCPDB.mail[Character()] or nil, open = mailbox ~= nil }
	if HasNewMail() then section.newMail = { GetLatestThreeSenders() } end
	return section
end

local mailEvents = CreateFrame("Frame")
for _, event in ipairs({ "MAIL_SHOW", "MAIL_INBOX_UPDATE", "MAIL_CLOSED", "PLAYER_INTERACTION_MANAGER_FRAME_SHOW",
	"PLAYER_INTERACTION_MANAGER_FRAME_HIDE" }) do
	pcall(mailEvents.RegisterEvent, mailEvents, event)
end
mailEvents:SetScript("OnEvent", function(_, event, kind)
	if event == "MAIL_SHOW" or (event == "PLAYER_INTERACTION_MANAGER_FRAME_SHOW" and kind == MAILBOX) then
		mailbox = mailbox or {} -- both say so
	elseif event == "MAIL_CLOSED" or (event == "PLAYER_INTERACTION_MANAGER_FRAME_HIDE" and kind == MAILBOX) then
		mailbox = nil
	elseif event == "MAIL_INBOX_UPDATE" and mailbox then
		mailbox.fetched = true
		SaveMail()
	end
end)

---------------------------------------------------------------------------
-- Any item by ID (get_item_details)
---------------------------------------------------------------------------

-- Requests carry no parameters, so the server first publishes the item IDs (payload key
-- itemLookup = { id, items }), then asks; the reply names the lookup it answers, so the server
-- can tell when the list hasn't arrived yet. Items the client hasn't cached are requested and
-- reported as loading; the server asks again a moment later.
local lookup -- { id, items }
local unknown = {} -- itemID -> true: the client couldn't load it (no such item in this game)

local function RequestLoad(itemID)
	pcall(C_Item.RequestLoadItemDataByID, itemID)
end

-- Red tooltip text marks what the player can't use: a level, class, skill or armor type they lack.
local function IsRed(color)
	return type(color) == "table" and type(color.r) == "number"
		and color.r > 0.9 and color.g < 0.3 and color.b < 0.3
end

-- Tooltip data as { left, right, price (a money line, in copper), red } per line.
local function Lines(data)
	local lines = {}
	for _, line in ipairs(type(data) == "table" and type(data.lines) == "table" and data.lines or {}) do
		local left, right = line.leftText, line.rightText
		if type(left) == "string" or type(right) == "string" then
			local entry = { left = type(left) == "string" and left or "" }
			if type(right) == "string" and right ~= "" then entry.right = right end
			if type(line.price) == "number" and line.price > 0 then entry.price = line.price end
			if IsRed(line.leftColor) or IsRed(line.rightColor) then entry.red = true end
			lines[#lines + 1] = entry
		end
	end
	return lines
end

-- { id, status, name, quality, ilvl, minLevel, type, subtype, slot, stats, armor, bind, lines }; bind is the
-- Enum.ItemBind value. status is left out when the item's there, else "loading" or "unknown".
local function Lookup(itemID)
	local item = { id = itemID }
	-- Class names and equip slot come from the client's static data, loaded or not.
	local _, itemType, subType, equipLoc = C_Item.GetItemInfoInstant(itemID)
	item.type, item.subtype = itemType, subType
	if equipLoc ~= "" then item.slot = equipLoc end

	local name, link, quality, level, minLevel, _, _, _, _, _, _, class, _, bind = C_Item.GetItemInfo(itemID)
	local lines = name and Lines(C_TooltipInfo.GetItemByID(itemID))
	for _, line in ipairs(lines or {}) do
		if line.left == RETRIEVING_ITEM_INFO then lines = nil break end
	end
	if not lines then
		if unknown[itemID] then
			item.status = "unknown"
		else
			RequestLoad(itemID)
			item.status = "loading"
		end
		return item
	end

	item.name, item.quality, item.lines, item.bind = name, quality, lines, bind
	if minLevel and minLevel > 1 then item.minLevel = minLevel end
	if class == WEAPON or class == ARMOR then item.ilvl = level end
	item.stats, item.armor = TakeArmor(Stats(link or ("item:" .. itemID)))
	for _, line in ipairs(lines) do
		item.armor = item.armor or ArmorIn(line.left)
	end
	return item
end

-- { [itemID] = { n, bound } }: how many the player has in their bags and bank (as last seen), and how many of
-- those are soulbound, which the auction house won't take even if the item only binds when equipped.
local function Held()
	local held = {}
	local function Add(itemID, count, bound)
		local h = held[itemID] or { n = 0, bound = 0 }
		held[itemID] = h
		h.n = h.n + (count or 1)
		if bound then h.bound = h.bound + (count or 1) end
	end
	for bag = Enum.BagIndex.Backpack, Enum.BagIndex.ReagentBag do
		for slot = 1, C_Container.GetContainerNumSlots(bag) do
			local info = C_Container.GetContainerItemInfo(bag, slot)
			if info and info.itemID then Add(info.itemID, info.stackCount, info.isBound) end
		end
	end
	local bank = WowMCPDB.bank and WowMCPDB.bank[Character()]
	for _, container in ipairs(bank and bank.bags or {}) do
		for _, item in ipairs(container.items or {}) do Add(item.id, item.count, item.bound) end
	end
	return held
end

local function ItemDetails()
	if not lookup then return { items = {} } end
	local read, held = pcall(Held)
	if not read then held = {} end
	local items = {}
	for _, itemID in ipairs(lookup.items) do
		local ok, item = pcall(Lookup, itemID)
		item = ok and item or { id = itemID, status = "error", error = tostring(item) }
		if held[itemID] then item.held, item.heldBound = held[itemID].n, held[itemID].bound end
		items[#items + 1] = item
	end
	return { lookup = lookup.id, items = items }
end

local function ReceiveLookup(value)
	if type(value) ~= "table" or type(value.id) ~= "number" or type(value.items) ~= "table" then return end
	if lookup and lookup.id == value.id then return end
	local items = {}
	for _, itemID in ipairs(value.items) do
		if type(itemID) == "number" and #items < MAX_LOOKUP then
			items[#items + 1] = itemID
			unknown[itemID] = nil
			-- Start loading now, so it's likely there by the time the server asks.
			local ok, cached = pcall(C_Item.IsItemDataCachedByID, itemID)
			if not (ok and cached) then RequestLoad(itemID) end
		end
	end
	lookup = { id = value.id, items = items }
end

local loads = CreateFrame("Frame")
loads:RegisterEvent("ITEM_DATA_LOAD_RESULT")
loads:SetScript("OnEvent", function(_, _, itemID, success)
	if type(itemID) == "number" then unknown[itemID] = not success or nil end
end)

-- For the upgrade finder (Auction.lua).
ns.Inventory = { Equipment = Equipment, GearStats = GearStats, Lines = Lines }

ns.RegisterSection(ns.Kinds.ITEMS, "bags", Bags)
ns.RegisterSection(ns.Kinds.ITEMS, "equipment", Equipment)
ns.RegisterSection(ns.Kinds.ITEMS, "bank", Bank)
ns.RegisterSection(ns.Kinds.ITEMS, "mail", MailSection)
ns.RegisterSection(ns.Kinds.ITEM_DETAILS, "itemDetails", ItemDetails)
ns.RegisterReceiver("itemLookup", ReceiveLookup)
