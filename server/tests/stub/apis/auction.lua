-- The auction house (Auction.lua): C_AuctionHouse as the client has it, and a stand-in for Blizzard's
-- auction house window that keeps what the player sees there, to check the addon's lookups leave it alone.
--
-- Like the real client: every search goes through one throttled channel (one message at a time, 100
-- searches a minute, more are dropped), and its results arrive a moment later through events. The channel
-- says when a message goes out (AUCTION_HOUSE_THROTTLED_MESSAGE_SENT), whoever sent it.
-- Stackable items are commodities: a search gives price points (COMMODITY_SEARCH_RESULTS_UPDATED with
-- the item ID); other items are single auctions, maybe bid-only (ITEM_SEARCH_RESULTS_UPDATED with the
-- item key). A browse search (search box, favorites) replaces the Browse view's list.
--
-- The full scan (ReplicateItems, AuctionHouseDocumentation.lua): one per SCAN_INTERVAL for the account;
-- asked for sooner, the server doesn't answer at all (assumed: nothing in the API says how it refuses).
-- Otherwise REPLICATE_ITEM_LIST_UPDATE comes a moment later, and every auction, the player's too, can be
-- read by index from 0 (GetReplicateItemInfo, its returns as documented; GetReplicateItemLink). An item the
-- client hasn't loaded has no name, texture or link yet (hasAllInfo false), but its ID, count, prices and owner. Whether the
-- event comes again once those load isn't documented; here it does, to check the addon copes. Closing
-- the auction house empties the list, and a scan that arrives after that stays empty (assumed). It goes
-- through the throttled channel like a search (assumed).
--
-- The window (Shared/Blizzard_AuctionHouseFrame.lua and its views): the Browse view only listens to
-- browse results; the commodity and item views reset their list on any search result, whichever item
-- it's for, and the sell views pick their price (or clear the selection) again. Before a view has its
-- own item's results there's nothing to lose; after, client.ah.disturbed lists each result for another
-- item that reached it. No part of it uses the full scan (nothing in Blizzard_AuctionHouseUI does).
--
-- The addon's status panel under the window needs status bars (SimpleStatusBarAPIDocumentation.lua): only
-- frames made as "StatusBar" have their methods, which keep the range and the value (clamped to it), as the
-- client does; and every frame has a strata.
--
-- The player's own auctions (client.ah.owned): QueryOwnedAuctions goes through the throttled channel like a search,
-- and OWNED_AUCTIONS_UPDATED comes with the answer; the client then has the list as it was then (GetNumOwnedAuctions,
-- GetOwnedAuctionInfo, as documented). Commodities give the time left in seconds, other items a time left band
-- (assumed: the documentation has both as optional). A sold auction stays listed as sold until its money goes out,
-- an hour later (assumed), with the time until then. The window's Auctions view (AuctionHouseAuctionsFrameMixin)
-- asks when it shows and when an auction is cancelled, and on every answer clears the player's selection: an answer
-- the addon asked for while it shows is a disturbance. Sales and expiries come as the game's notice
-- (AUCTION_HOUSE_SHOW_FORMATTED_NOTIFICATION with the item's name and the auction) and as a system chat line, each
-- optional per call.

local LATENCY = 0.5 -- seconds for a search's answer
local THROTTLE = 100 -- searches a minute
local SCAN_INTERVAL = 15 * 60
local SELLER, PLAYER = "Grimbolt", "Thorgrim"

client.ah = {
	-- item ID -> { commodity, results = { { price, quantity, own } } } (commodities) or
	-- { results = { { buyout, bid, quantity, own, suffix } } } (single auctions; suffix: a random suffix, see
	-- client.suffixes in inventory.lua). Unlisted items have no results.
	listings = {
		[4232] = { commodity = true, results = {
			{ price = 45, quantity = 30 }, { price = 52, quantity = 12, own = 2 }, { price = 60, quantity = 40 } } },
		[4289] = { commodity = true, results = { { price = 38, quantity = 200 } } },
		[2321] = { commodity = true, results = {} },
		[4340] = { commodity = true, results = { { price = 210, quantity = 3 } } },
		[2318] = { commodity = true, results = { { price = 30, quantity = 80 } } },
		[4253] = { results = { { buyout = 12000, quantity = 1 }, { buyout = 9500, bid = 4000, quantity = 1 },
			{ bid = 3000, quantity = 1 }, { buyout = 8000, quantity = 1, own = true } } },
		[783] = { commodity = true, results = { { price = 20, quantity = 5 } } },
	},
	keyInfo = { [4232] = true, [4253] = true }, -- items whose GetItemKeyInfo the client has; others load when asked for
	searches = {},   -- every search: { item, by = "addon" | "player", mode (the window's view then), sorts, at, queued }
	results = {},    -- item ID -> the last results received, as the client keeps them
	messages = {},   -- the throttled channel: waiting and in flight, oldest first
	sentAt = {},     -- when each search went out, for the per-minute limit
	dropped = 0,
	log = {},        -- every message as it went out: { at, by = "addon" | "player" }
	browse = {},     -- what the Browse view lists
	favorites = nil, -- item IDs the player has favourited: searched for when the window opens
	disturbed = {},
	sorts = { [14] = { { sortOrder = 4, reverseSort = false }, { sortOrder = 3, reverseSort = false }, mark = "window" }, [15] = {},
		[22] = { { sortOrder = 1, reverseSort = false }, { sortOrder = 0, reverseSort = false }, mark = "window" } },
	-- The full scan: calls = { { at, mode } }; at = time() of the account's last one the server allowed (e.g.
	-- by another addon); list = the auctions as last sent; refused = calls the server didn't answer.
	replicate = { calls = {}, at = nil, latency = 2, list = {}, refused = 0, reads = 0 },
	missingInfo = {}, -- item ID -> true: the full scan's entries for it have no name yet
	-- The player's auctions, the ones in listings marked own: { id, item, quantity, buyout (per item), bid, ends
	-- (client.now), banded (the time left comes as a band), soldAt }.
	owned = {
		{ id = 7001, item = 4232, quantity = 2, buyout = 52, ends = 20 * 3600 },
		{ id = 7002, item = 4253, quantity = 1, buyout = 8000, ends = 30 * 3600, banded = true },
	},
	ownedList = {},    -- what the client has from the last answer: { id, ..., left }
	ownedQueries = {}, -- every QueryOwnedAuctions: { at, by, mode, sorts }
	nextID = 7100,
}

Enum.AuctionHouseSortOrder = { Price = 0, Name = 1, Level = 2, Bid = 3, Buyout = 4, TimeRemaining = 5 }
Enum.PlayerInteractionType = { Merchant = 5, Auctioneer = 21 }
Enum.AuctionStatus = { Active = 0, Sold = 1 }
Enum.AuctionHouseTimeLeftBand = { Short = 0, Medium = 1, Long = 2, VeryLong = 3 }
Enum.AuctionHouseNotification = { BidPlaced = 0, AuctionRemoved = 1, AuctionWon = 2, AuctionOutbid = 3, AuctionSold = 4,
	AuctionExpired = 5 }
ERR_AUCTION_SOLD_S = "A buyer has been found for your auction of %s."
ERR_AUCTION_EXPIRED_S = "Your auction of %s has expired."
ERR_AUCTION_OUTBID_S = "You have been outbid on %s."
ERR_AUCTION_WON_S = "You won an auction for %s"
local BANDS = { { 1800, 0 }, { 7200, 1 }, { 43200, 2 } } -- seconds left below which a band applies; else VeryLong (3)
local BAND_SECONDS = { [0] = { 0, 1800 }, [1] = { 1800, 7200 }, [2] = { 7200, 43200 }, [3] = { 43200, 172800 } }
local PAYOUT = 3600 -- a sold auction's money goes out this long after the sale (assumed)
-- From Blizzard_AuctionHouseUI, which loads the first time the auction house opens; the tests don't need that.
AuctionHouseSearchContext = { BuyItems = 14, BuyCommodities = 15, SellItems = 16, SellCommodities = 17, AllAuctions = 22 }
AuctionHouseFrameDisplayMode = {
	Buy = { "CategoriesList", "SearchBar", "BrowseResultsFrame" },
	CommoditiesBuy = { "CategoriesList", "SearchBar", "CommoditiesBuyFrame" },
	ItemBuy = { "CategoriesList", "SearchBar", "ItemBuyFrame" },
	CommoditiesSell = { "CommoditiesSellFrame", "CommoditiesSellList" },
	ItemSell = { "ItemSellFrame", "ItemSellList" },
	Auctions = { "AuctionsFrame" },
}

local Frame = getmetatable(UIParent)
function Frame:GetFrameStrata() return self.strata or "MEDIUM" end

local StatusBar = setmetatable({}, { __index = Frame })
StatusBar.__index = StatusBar
function StatusBar:SetMinMaxValues(low, high) self.minValue, self.maxValue = low, high end
function StatusBar:GetMinMaxValues() return self.minValue or 0, self.maxValue or 0 end
function StatusBar:SetValue(value)
	local low, high = self:GetMinMaxValues()
	self.value = math.max(low, math.min(high, value))
end
function StatusBar:GetValue() return self.value or 0 end
function StatusBar:SetStatusBarTexture(asset)
	self.barTexture = asset
	return true
end
function StatusBar:SetStatusBarColor(r, g, b, a) self.barColor = { r, g, b, a or 1 } end

local createFrame = CreateFrame
function CreateFrame(frameType, name, parent, template)
	local frame = createFrame(frameType, name, parent, template)
	if frameType == "StatusBar" then setmetatable(frame, StatusBar) end
	return frame
end

local function Listing(itemID)
	return client.ah.listings[itemID] or { results = {} }
end

local function Copy(t)
	local c = {}
	for k, v in pairs(t) do c[k] = type(v) == "table" and Copy(v) or v end
	return c
end

---------------------------------------------------------------------------
-- The throttled channel
---------------------------------------------------------------------------

local Next

local function Answer(message)
	table.remove(client.ah.messages, 1)
	message.answer()
	Next()
end

function Next()
	local message = client.ah.messages[1]
	if message and not message.sent then
		message.sent = true
		table.insert(client.ah.log, { at = client.now, by = message.by })
		client.Fire("AUCTION_HOUSE_THROTTLED_MESSAGE_SENT")
		C_Timer.After(LATENCY, function() Answer(message) end)
	end
end

local function Throttled(answer, by)
	local now = client.now
	local recent = {}
	for _, at in ipairs(client.ah.sentAt) do
		if now - at < 60 then table.insert(recent, at) end
	end
	client.ah.sentAt = recent
	if #recent >= THROTTLE or client.ah.dropNext then
		client.ah.dropNext = nil
		client.ah.dropped = client.ah.dropped + 1
		client.Fire("AUCTION_HOUSE_THROTTLED_MESSAGE_DROPPED")
		return
	end
	table.insert(client.ah.sentAt, now)
	if #client.ah.messages > 0 then client.Fire("AUCTION_HOUSE_THROTTLED_MESSAGE_QUEUED") end
	table.insert(client.ah.messages, { answer = answer, by = by })
	Next()
end

-- The window's view now, by name (nil while it's hidden).
local function Mode()
	for name, m in pairs(AuctionHouseFrameDisplayMode) do
		if AuctionHouseFrame and AuctionHouseFrame:IsShown() and AuctionHouseFrame.mode == m then return name end
	end
end

local function Search(key, sorts, by)
	local itemID = key.itemID
	local mode = Mode()
	-- queued: sent while another message was still out, so it waits behind it.
	table.insert(client.ah.searches, { item = itemID, by = by, mode = mode, sorts = sorts, at = client.now,
		queued = #client.ah.messages > 0 })
	Throttled(function()
		local listing = Listing(itemID)
		client.ah.results[itemID] = Copy(listing.results)
		if listing.commodity then
			client.Fire("COMMODITY_SEARCH_RESULTS_UPDATED", itemID)
			client.Fire("COMMODITY_SEARCH_RESULTS_RECEIVED")
		else
			client.Fire("ITEM_SEARCH_RESULTS_UPDATED", { itemID = itemID, itemLevel = key.itemLevel or 0, itemSuffix = 0, battlePetSpeciesID = 0 })
		end
	end, by)
end

local function Browse(items)
	Throttled(function()
		client.ah.browse = {}
		for _, itemID in ipairs(items) do
			local listing = Listing(itemID)
			local cheapest
			for _, r in ipairs(listing.results) do
				local price = r.price or r.buyout
				if price and (not cheapest or price < cheapest) then cheapest = price end
			end
			table.insert(client.ah.browse,
				{ itemKey = C_AuctionHouse.MakeItemKey(itemID), minPrice = cheapest or 0, totalQuantity = #listing.results })
		end
		client.Fire("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")
	end, "player")
end

-- Every auction listed, one entry each, in item ID order: { item, count, buyout (0: bids only), bid, owner }.
local function Auctions()
	local ids, list = {}, {}
	for itemID in pairs(client.ah.listings) do ids[#ids + 1] = itemID end
	table.sort(ids)
	for _, itemID in ipairs(ids) do
		local listing = client.ah.listings[itemID]
		for _, r in ipairs(listing.results) do
			if listing.commodity then
				local others, own = r.quantity - (r.own or 0), r.own or 0
				if others > 0 then table.insert(list, { item = itemID, count = others, buyout = r.price * others, owner = SELLER }) end
				if own > 0 then table.insert(list, { item = itemID, count = own, buyout = r.price * own, owner = PLAYER }) end
			else
				table.insert(list, { item = itemID, count = r.quantity, buyout = r.buyout or 0, bid = r.bid or 0,
					owner = r.own and PLAYER or SELLER, suffix = r.suffix })
			end
		end
	end
	return list
end

local function Replicate()
	local r = client.ah.replicate
	table.insert(r.calls, { at = client.now, mode = Mode() })
	local allowed = not r.at or time() - r.at >= SCAN_INTERVAL
	if allowed then r.at = time() end
	Throttled(function()
		if not allowed then
			r.refused = r.refused + 1
			return
		end
		C_Timer.After(r.latency, function()
			r.list = AuctionHouseFrame:IsShown() and Auctions() or {}
			client.Fire("REPLICATE_ITEM_LIST_UPDATE")
		end)
	end, "addon")
end

---------------------------------------------------------------------------
-- The player's own auctions
---------------------------------------------------------------------------

local function ItemName(itemID)
	local item = client.items[itemID]
	return item and item.name or client.itemNames[itemID] or ("Item " .. itemID)
end

local function Band(seconds)
	for _, b in ipairs(BANDS) do
		if seconds < b[1] then return b[2] end
	end
	return 3
end

-- The list as the server has it now (a sold one until its money goes out), with the seconds left then.
local function Owned()
	local list = {}
	for _, o in ipairs(client.ah.owned) do
		if not (o.soldAt and client.now - o.soldAt >= PAYOUT) then
			local copy = Copy(o)
			copy.left = math.max(0, math.floor((o.soldAt and o.soldAt + PAYOUT or o.ends) - client.now))
			list[#list + 1] = copy
		end
	end
	return list
end

-- client.ah.ownedLatency: seconds more before the answer's event, for a slow server.
local function QueryOwned(sorts, by)
	table.insert(client.ah.ownedQueries, { at = client.now, by = by, mode = Mode(), sorts = sorts })
	Throttled(function()
		local function answer()
			client.ah.ownedList, client.ah.ownedBy = Owned(), by
			client.Fire("OWNED_AUCTIONS_UPDATED")
		end
		if client.ah.ownedLatency then C_Timer.After(client.ah.ownedLatency, answer) else answer() end
	end, by)
end

-- OwnedAuctionInfo: the time left of a sold one is until its money comes (assumed).
local function OwnedInfo(i)
	local o = client.ah.ownedList[i]
	if not o then return nil end
	local link = client.ah.missingInfo[o.item] == nil
		and string.format("|cffffffff|Hitem:%d::::::::12:::::::::|h[%s]|h|r", o.item, ItemName(o.item)) or nil
	local banded = o.banded and not o.soldAt
	return { auctionID = o.id, itemKey = C_AuctionHouse.MakeItemKey(o.item), itemLink = link, status = o.soldAt and 1 or 0,
		quantity = o.quantity, timeLeftSeconds = not banded and o.left or nil, timeLeft = banded and Band(o.left) or nil,
		bidAmount = o.bid, buyoutAmount = o.buyout, bidder = o.bid and "" or nil }
end

local function FindOwned(id)
	for i, o in ipairs(client.ah.owned) do
		if o.id == id then return o, i end
	end
	error("no auction " .. tostring(id))
end

-- The player's auction in the listings: there while it's up.
local function List(o)
	local listing = client.ah.listings[o.item]
	if not listing then
		listing = { commodity = o.commodity, results = {} }
		client.ah.listings[o.item] = listing
	end
	if listing.commodity then
		table.insert(listing.results, { price = o.buyout, quantity = o.quantity, own = o.quantity })
	else
		table.insert(listing.results, { buyout = o.buyout * o.quantity, quantity = o.quantity, own = true })
	end
end

local function Unlist(o)
	local listing = client.ah.listings[o.item]
	for i, r in ipairs(listing and listing.results or {}) do
		if listing.commodity and r.price == o.buyout and (r.own or 0) >= o.quantity then
			r.quantity, r.own = r.quantity - o.quantity, r.own - o.quantity
			if r.quantity == 0 then table.remove(listing.results, i) end
			return
		elseif not listing.commodity and r.own == true and r.buyout == o.buyout * o.quantity then
			table.remove(listing.results, i)
			return
		end
	end
end

-- The player posts an auction (from a sell view): { item, quantity, price (per item), hours, banded, commodity (if
-- nothing of it is listed) }. Its ID.
function client.PostAuction(a)
	local id = client.ah.nextID
	client.ah.nextID = id + 1
	Throttled(function()
		local o = { id = id, item = a.item, quantity = a.quantity, buyout = a.price, ends = client.now + (a.hours or 24) * 3600,
			banded = a.banded, commodity = a.commodity }
		table.insert(client.ah.owned, o)
		List(o)
		client.Fire("AUCTION_HOUSE_AUCTION_CREATED", id)
	end, "player")
	return id
end

-- The player cancels one (on the Auctions view, which then asks for the list again).
function client.CancelOwnAuction(id)
	Throttled(function()
		local o, i = FindOwned(id)
		table.remove(client.ah.owned, i)
		Unlist(o)
		client.Fire("AUCTION_CANCELED", id)
	end, "player")
end

-- Someone buys one. says: { notice = false, chat = false } leaves out that way of saying so.
function client.SellOwnAuction(id, says)
	says = says or {}
	local o = FindOwned(id)
	o.soldAt = client.now
	Unlist(o)
	local name = ItemName(o.item)
	if says.notice ~= false then
		client.Fire("AUCTION_HOUSE_SHOW_FORMATTED_NOTIFICATION", Enum.AuctionHouseNotification.AuctionSold, name, id)
	end
	if says.chat ~= false then client.Fire("CHAT_MSG_SYSTEM", ERR_AUCTION_SOLD_S:format(name)) end
end

-- The sale's mail: the money, less the auction house's 5%, with the invoice.
function client.SaleMail(id)
	local o = FindOwned(id)
	local total, name = o.buyout * o.quantity, ItemName(o.item)
	local fee = math.floor(total * 0.05)
	client.Deliver({ sender = "Auction House", subject = AUCTION_SOLD_MAIL_SUBJECT:format(name), money = total - fee,
		invoice = { type = "seller", item = name, player = "Buyer", bid = total, buyout = total, fee = fee, count = o.quantity } })
end

-- One runs out: off the list, and the item comes back by mail. says as for SellOwnAuction, and mail = false.
function client.ExpireOwnAuction(id, says)
	says = says or {}
	local o, i = FindOwned(id)
	table.remove(client.ah.owned, i)
	Unlist(o)
	local name = ItemName(o.item)
	if says.notice ~= false then
		client.Fire("AUCTION_HOUSE_SHOW_FORMATTED_NOTIFICATION", Enum.AuctionHouseNotification.AuctionExpired, name, id)
	end
	if says.chat ~= false then client.Fire("CHAT_MSG_SYSTEM", ERR_AUCTION_EXPIRED_S:format(name)) end
	if says.mail ~= false then
		client.Deliver({ sender = "Auction House", subject = AUCTION_EXPIRED_MAIL_SUBJECT:format(name),
			items = { [1] = { id = o.item, count = o.quantity } } })
	end
end

-- The addon's QueryOwnedAuctions, in order: { at, mode, sorts }.
function client.AddonOwnedQueries()
	local list = {}
	for _, q in ipairs(client.ah.ownedQueries) do
		if q.by == "addon" then table.insert(list, q) end
	end
	return list
end

-- An item's data arrives a moment after the full scan's entry for it was first read without it.
local function LoadInfo(itemID)
	if client.ah.missingInfo[itemID] ~= true then return end
	client.ah.missingInfo[itemID] = "loading"
	C_Timer.After(1, function()
		client.ah.missingInfo[itemID] = nil
		local again = false
		for _, state in pairs(client.ah.missingInfo) do again = again or state == "loading" end
		if not again then client.Fire("REPLICATE_ITEM_LIST_UPDATE") end
	end)
end

-- name, texture, count, qualityID, usable, level, levelType, minBid, minIncrement, buyoutPrice, bidAmount,
-- highBidder, bidderFullName, owner, ownerFullName, saleStatus, itemID, hasAllInfo; nothing past the end.
local function ReplicateItemInfo(index)
	local a = client.ah.replicate.list[index + 1]
	if not a then return end
	client.ah.replicate.reads = client.ah.replicate.reads + 1
	local missing = client.ah.missingInfo[a.item] ~= nil
	if missing then LoadInfo(a.item) end
	local name = not missing and (client.itemNames[a.item] or ("Item " .. a.item)) or nil
	return name, not missing and 134400 or nil, a.count, 1, true, 1, "", a.bid or 0, 0, a.buyout, 0, nil, nil, a.owner,
		a.owner .. "-Firemaw", 0, a.item, not missing
end

-- The auction's item link, with its random suffix; like the name, nil until the item's data has loaded.
local function ReplicateItemLink(index)
	local a = client.ah.replicate.list[index + 1]
	if not a or client.ah.missingInfo[a.item] ~= nil then return nil end
	local item = client.items[a.item]
	local name = (item and item.name or client.itemNames[a.item] or ("Item " .. a.item))
		.. (a.suffix and client.suffixes[a.suffix].name or "")
	return string.format("|cff1eff00|Hitem:%d::::::%s::12:::::::::|h[%s]|h|r", a.item, a.suffix or "", name)
end

-- A big auction house for tests: items item IDs from first, each with perItem auctions of up to 20, about a
-- third of them commodities; prices made up but repeatable.
function client.FillAuctionHouse(first, items, perItem)
	for i = 0, items - 1 do
		local itemID, results = first + i, {}
		local commodity = i % 3 == 0
		for j = 1, perItem do
			local price = 100 + (i * 37 + j * 11) % 5000
			local quantity = 1 + (i + j) % 20
			if commodity then
				results[j] = { price = price, quantity = quantity }
			else
				results[j] = { buyout = j % 7 ~= 0 and price * quantity or nil, bid = price, quantity = quantity }
			end
		end
		client.ah.listings[itemID] = { commodity = commodity, results = results }
	end
end

---------------------------------------------------------------------------
-- C_AuctionHouse
---------------------------------------------------------------------------

local function Results(itemID)
	return client.ah.results[itemID] or {}
end

C_AuctionHouse = {
	MakeItemKey = function(itemID, itemLevel, itemSuffix, battlePetSpeciesID)
		return { itemID = itemID, itemLevel = itemLevel or 0, itemSuffix = itemSuffix or 0, battlePetSpeciesID = battlePetSpeciesID or 0 }
	end,
	-- nil until the client has the item; ITEM_KEY_ITEM_INFO_RECEIVED says when.
	GetItemKeyInfo = function(key)
		local itemID = key.itemID
		if not client.ah.keyInfo[itemID] then
			C_Timer.After(0, function()
				client.ah.keyInfo[itemID] = true
				client.Fire("ITEM_KEY_ITEM_INFO_RECEIVED", itemID)
			end)
			return nil
		end
		return { itemID = itemID, battlePetSpeciesID = 0, itemName = client.itemNames[itemID] or "", quality = 1,
			iconFileID = 134400, isPet = false, isCommodity = Listing(itemID).commodity == true, isEquipment = false }
	end,
	IsThrottledMessageSystemReady = function() return #client.ah.messages == 0 end,
	SendSearchQuery = function(key, sorts, separateOwnerItems)
		assert(type(key) == "table" and type(sorts) == "table" and type(separateOwnerItems) == "boolean", "bad arguments")
		Search(key, sorts, "addon")
	end,
	SendBrowseQuery = function() error("the addon must not replace the player's browse list") end,
	SearchForItemKeys = function() error("the addon must not replace the player's browse list") end,
	SearchForFavorites = function() Browse(client.ah.favorites or {}) end,
	GetBrowseResults = function() return Copy(client.ah.browse) end,
	HasSearchResults = function(key) return client.ah.results[key.itemID] ~= nil end,

	GetNumCommoditySearchResults = function(itemID) return #Results(itemID) end,
	GetCommoditySearchResultInfo = function(itemID, i)
		local r = Results(itemID)[i]
		if not r then return nil end
		return { itemID = itemID, quantity = r.quantity, unitPrice = r.price, auctionID = 1000 + i, owners = {},
			totalNumberOfOwners = 1, numOwnerItems = r.own or 0, containsOwnerItem = (r.own or 0) > 0, containsAccountItem = false }
	end,
	GetCommoditySearchResultsQuantity = function(itemID)
		local total = 0
		for _, r in ipairs(Results(itemID)) do total = total + r.quantity end
		return total
	end,
	HasFullCommoditySearchResults = function() return true end,

	GetNumItemSearchResults = function(key) return #Results(key.itemID) end,
	GetItemSearchResultInfo = function(key, i)
		local r = Results(key.itemID)[i]
		if not r then return nil end
		return { itemKey = key, owners = {}, totalNumberOfOwners = 1, timeLeft = 2, auctionID = 2000 + i, quantity = r.quantity,
			containsOwnerItem = r.own == true, containsAccountItem = false, containsSocketedItem = false,
			minBid = r.bid, bidAmount = nil, buyoutAmount = r.buyout }
	end,
	GetItemSearchResultsQuantity = function(key)
		local total = 0
		for _, r in ipairs(Results(key.itemID)) do total = total + r.quantity end
		return total
	end,
	HasFullItemSearchResults = function() return true end,

	ReplicateItems = Replicate,
	GetNumReplicateItems = function() return #client.ah.replicate.list end,
	GetReplicateItemInfo = ReplicateItemInfo,
	GetReplicateItemLink = ReplicateItemLink,

	QueryOwnedAuctions = function(sorts)
		assert(type(sorts) == "table", "bad arguments")
		QueryOwned(sorts, "addon")
	end,
	GetNumOwnedAuctions = function() return #client.ah.ownedList end,
	GetOwnedAuctionInfo = OwnedInfo,
	GetOwnedAuctions = function()
		local list = {}
		for i = 1, #client.ah.ownedList do list[i] = OwnedInfo(i) end
		return list
	end,
	HasFullOwnedAuctionResults = function() return true end,
	GetTimeLeftBandInfo = function(band)
		local b = assert(BAND_SECONDS[band], "bad band")
		return b[1], b[2]
	end,
}

---------------------------------------------------------------------------
-- Blizzard's window, as far as what it shows goes
---------------------------------------------------------------------------

local VIEW_EVENTS = { "COMMODITY_SEARCH_RESULTS_UPDATED", "COMMODITY_SEARCH_RESULTS_RECEIVED", "ITEM_SEARCH_RESULTS_UPDATED",
	"AUCTION_HOUSE_BROWSE_RESULTS_UPDATED", "OWNED_AUCTIONS_UPDATED", "AUCTION_CANCELED" }

local function CreateWindow()
	local f = CreateFrame("Frame", "AuctionHouseFrame", UIParent)
	f.mode = AuctionHouseFrameDisplayMode.Buy
	f.view = {} -- the open view's item and whether it shows its results yet
	function f:GetDisplayMode() return self.mode end
	function f:GetSortsForContext(context) return client.ah.sorts[context] or {} end
	for _, event in ipairs(VIEW_EVENTS) do f:RegisterEvent(event) end
	f:SetScript("OnEvent", function(self, event, arg1)
		if not self:IsShown() then return end
		local mode, view = self.mode, self.view
		if event == "OWNED_AUCTIONS_UPDATED" or event == "AUCTION_CANCELED" then
			-- Only the Auctions view listens: it asks again after a cancel, and every answer clears the player's
			-- selection there; one the addon asked for, once the player had the list, is a disturbance.
			if mode ~= AuctionHouseFrameDisplayMode.Auctions then return end
			if event == "AUCTION_CANCELED" then
				return QueryOwned(self:GetSortsForContext(AuctionHouseSearchContext.AllAuctions), "player")
			end
			if view.loaded and client.ah.ownedBy == "addon" then
				table.insert(client.ah.disturbed, { event = event, view = "auctions" })
			end
			view.loaded = true
			return
		end
		if event == "AUCTION_HOUSE_BROWSE_RESULTS_UPDATED" then
			if mode == AuctionHouseFrameDisplayMode.Buy then self.browse = C_AuctionHouse.GetBrowseResults() end
			return
		end
		local itemID = event == "ITEM_SEARCH_RESULTS_UPDATED" and arg1.itemID or arg1
		local commodityEvent = event ~= "ITEM_SEARCH_RESULTS_UPDATED"
		local M = AuctionHouseFrameDisplayMode
		local listens
		if commodityEvent then
			listens = mode == M.CommoditiesBuy or mode == M.CommoditiesSell
		else
			listens = mode == M.ItemBuy or mode == M.ItemSell or mode == M.Auctions
		end
		if not listens then return end
		if itemID == view.item then
			view.loaded = true
		elseif itemID ~= nil and view.loaded then
			-- A list reset, or a price picked again, on a view the player was looking at.
			table.insert(client.ah.disturbed, { event = event, item = itemID, view = view.item })
		end
	end)
	return f
end

-- The player talks to an auctioneer: the window opens on the Browse view and looks up their favourites.
function client.OpenAuctionHouse()
	local f = AuctionHouseFrame or CreateWindow()
	f.mode, f.view, f.browse = AuctionHouseFrameDisplayMode.Buy, {}, nil
	f:Show()
	client.Fire("PLAYER_INTERACTION_MANAGER_FRAME_SHOW", Enum.PlayerInteractionType.Auctioneer)
	client.Fire("AUCTION_HOUSE_SHOW")
	if client.ah.favorites then C_AuctionHouse.SearchForFavorites({}) end
end

function client.CloseAuctionHouse()
	AuctionHouseFrame:Hide()
	client.ah.replicate.list = {}
	client.Fire("AUCTION_HOUSE_CLOSED")
	client.Fire("PLAYER_INTERACTION_MANAGER_FRAME_HIDE", Enum.PlayerInteractionType.Auctioneer)
end

local function PlayerSearch(itemID, context)
	local f = AuctionHouseFrame
	local key = C_AuctionHouse.MakeItemKey(itemID)
	Search(key, f:GetSortsForContext(context), "player")
end

-- The player clicks an item in the browse list (AuctionHouseFrameMixin:SelectBrowseResult).
function client.ViewAuctionItem(itemID)
	local f = AuctionHouseFrame
	local commodity = Listing(itemID).commodity
	f.mode = commodity and AuctionHouseFrameDisplayMode.CommoditiesBuy or AuctionHouseFrameDisplayMode.ItemBuy
	f.view = { item = itemID }
	PlayerSearch(itemID, commodity and AuctionHouseSearchContext.BuyCommodities or AuctionHouseSearchContext.BuyItems)
end

-- The player puts an item from their bags up for sale.
function client.SellAuctionItem(itemID)
	local f = AuctionHouseFrame
	local commodity = Listing(itemID).commodity
	f.mode = commodity and AuctionHouseFrameDisplayMode.CommoditiesSell or AuctionHouseFrameDisplayMode.ItemSell
	f.view = { item = itemID }
	PlayerSearch(itemID, commodity and AuctionHouseSearchContext.SellCommodities or AuctionHouseSearchContext.SellItems)
end

-- The player searches by name in the Browse view (a browse query, which replaces its list).
function client.PlayerBrowse(items)
	Browse(items)
end

function client.BackToBrowse()
	AuctionHouseFrame.mode, AuctionHouseFrame.view = AuctionHouseFrameDisplayMode.Buy, {}
end

-- The player opens the Auctions tab: its list asks for their auctions (AuctionHouseItemListMixin:OnShow).
function client.ViewOwnAuctions()
	local f = AuctionHouseFrame
	f.mode, f.view = AuctionHouseFrameDisplayMode.Auctions, {}
	QueryOwned(f:GetSortsForContext(AuctionHouseSearchContext.AllAuctions), "player")
end

-- Another auction addon takes a full scan: the account's one for a while, if the server allows it.
function client.OtherAddonScan()
	local r = client.ah.replicate
	if r.at and time() - r.at < SCAN_INTERVAL then return false end
	r.at = time()
	C_Timer.After(r.latency, function()
		r.list = Auctions()
		client.Fire("REPLICATE_ITEM_LIST_UPDATE")
	end)
	return true
end

-- The full scans asked for (the addon's: nothing else in these tests calls ReplicateItems), in order: { at, mode }.
function client.AddonScans()
	return client.ah.replicate.calls
end

-- The addon's searches, in order: item IDs.
function client.AddonSearches()
	local list = {}
	for _, s in ipairs(client.ah.searches) do
		if s.by == "addon" then table.insert(list, s.item) end
	end
	return list
end
