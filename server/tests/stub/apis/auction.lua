-- The auction house (Auction.lua): C_AuctionHouse as the client has it, and a stand-in for Blizzard's
-- auction house window that keeps what the player sees there, to check the addon's lookups leave it alone.
--
-- Like the real client: every search goes through one throttled channel (one message at a time, 100
-- searches a minute, more are dropped), and its results arrive a moment later through events.
-- Stackable items are commodities: a search gives price points (COMMODITY_SEARCH_RESULTS_UPDATED with
-- the item ID); other items are single auctions, maybe bid-only (ITEM_SEARCH_RESULTS_UPDATED with the
-- item key). A browse search (search box, favorites) replaces the Browse view's list.
--
-- The window (Shared/Blizzard_AuctionHouseFrame.lua and its views): the Browse view only listens to
-- browse results; the commodity and item views reset their list on any search result, whichever item
-- it's for, and the sell views pick their price (or clear the selection) again. Before a view has its
-- own item's results there's nothing to lose; after, client.ah.disturbed lists each result for another
-- item that reached it.

local LATENCY = 0.5 -- seconds for a search's answer
local THROTTLE = 100 -- searches a minute

client.ah = {
	-- item ID -> { commodity, results = { { price, quantity, own } } } (commodities) or
	-- { results = { { buyout, bid, quantity, own } } } (single auctions). Unlisted items have no results.
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
	browse = {},     -- what the Browse view lists
	favorites = nil, -- item IDs the player has favourited: searched for when the window opens
	disturbed = {},
	sorts = { [14] = { { sortOrder = 4, reverseSort = false }, { sortOrder = 3, reverseSort = false }, mark = "window" }, [15] = {} },
}

Enum.AuctionHouseSortOrder = { Price = 0, Name = 1, Level = 2, Bid = 3, Buyout = 4, TimeRemaining = 5 }
Enum.PlayerInteractionType = { Merchant = 5, Auctioneer = 21 }
-- From Blizzard_AuctionHouseUI, which loads the first time the auction house opens; the tests don't need that.
AuctionHouseSearchContext = { BuyItems = 14, BuyCommodities = 15, SellItems = 16, SellCommodities = 17 }
AuctionHouseFrameDisplayMode = {
	Buy = { "CategoriesList", "SearchBar", "BrowseResultsFrame" },
	CommoditiesBuy = { "CategoriesList", "SearchBar", "CommoditiesBuyFrame" },
	ItemBuy = { "CategoriesList", "SearchBar", "ItemBuyFrame" },
	CommoditiesSell = { "CommoditiesSellFrame", "CommoditiesSellList" },
	ItemSell = { "ItemSellFrame", "ItemSellList" },
	Auctions = { "AuctionsFrame" },
}

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
		C_Timer.After(LATENCY, function() Answer(message) end)
	end
end

local function Throttled(answer)
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
	table.insert(client.ah.messages, { answer = answer })
	Next()
end

local function Search(key, sorts, by)
	local itemID = key.itemID
	local mode
	for name, m in pairs(AuctionHouseFrameDisplayMode) do
		if AuctionHouseFrame and AuctionHouseFrame:IsShown() and AuctionHouseFrame.mode == m then mode = name end
	end
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
	end)
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
	end)
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
}

---------------------------------------------------------------------------
-- Blizzard's window, as far as what it shows goes
---------------------------------------------------------------------------

local VIEW_EVENTS = { "COMMODITY_SEARCH_RESULTS_UPDATED", "COMMODITY_SEARCH_RESULTS_RECEIVED", "ITEM_SEARCH_RESULTS_UPDATED",
	"AUCTION_HOUSE_BROWSE_RESULTS_UPDATED" }

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

function client.BackToBrowse()
	AuctionHouseFrame.mode, AuctionHouseFrame.view = AuctionHouseFrameDisplayMode.Buy, {}
end

-- The addon's searches, in order: item IDs.
function client.AddonSearches()
	local list = {}
	for _, s in ipairs(client.ah.searches) do
		if s.by == "addon" then table.insert(list, s.item) end
	end
	return list
end
