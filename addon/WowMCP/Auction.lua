-- WowMCP auction prices: while the auction house is open, looks up what the items the AI cares about
-- cost there: what active goals and the current plan still need, then a watch list the AI sets
-- (payload key "auctionWatch", watch_auction_prices tool). Prices are cached per realm with when
-- they were seen (section "auction" of request kind KNOWLEDGE, get_auction_prices tool). Results
-- of the player's own item searches are noted too.
--
-- The lookups must not disturb the player's own browsing. They're item searches
-- (C_AuctionHouse.SendSearchQuery), and in Blizzard's auction house window (Blizzard_AuctionHouseUI,
-- Shared and Mainline code on Forever) the Browse view only listens to browse queries, while the
-- item, commodity and sell views redraw on every search result, whichever item it's for, and the
-- sell views pick a price from it. So searches only go out while the window shows its Browse view,
-- one at a time, when the game's request throttle is ready, at most MAX_PER_MINUTE a minute (the
-- game allows 100), and with the sorts the window itself uses for that kind of search, so its own
-- later refresh of the item looks the same. If the player opens an item or starts selling, the
-- lookups wait until they're back on the Browse view.

local _, ns = ...

local Auction = {}
ns.Auction = Auction

local STEP_INTERVAL = 0.5
local START_DELAY = 1     -- the window's own opening searches (bids, favorites) go first
local TIMEOUT = 5         -- seconds without an answer: that search is given up
local MAX_PER_MINUTE = 50 -- half of the game's limit, so the player's searches never wait for ours
local MAX_PER_VISIT = 40
local FRESH = 10 * 60     -- prices this recent aren't looked up again on the next visit
local MAX_CACHED = 100    -- per realm; the oldest go first
local MAX_TIERS = 6       -- the cheapest price points kept per item
local SORT = Enum.AuctionHouseSortOrder or { Bid = 3, Buyout = 4 }
local ITEM_SORTS = { -- the window's defaults for item searches
	{ sortOrder = SORT.Buyout, reverseSort = false }, { sortOrder = SORT.Bid, reverseSort = false },
}
local GREY = "|cffaaaaaa"

local visit   -- while the auction house is open: { queue, groups = { [item] = "goal" | "plan" | "watch" }, ... }
local current -- our search in flight: { item, at }
local sent = {} -- GetTime() of our searches in the last minute
local watch = {} -- item IDs from the payload
local problem

-- WowMCPDB.auction[realm][item ID] = { price (cheapest, per item, copper), listed, tiers = { { price,
-- count } }, commodity, bidOnly, name, at (unix time), zone, subzone, faction }.
local function Prices()
	WowMCPDB.auction = WowMCPDB.auction or {}
	local realm = GetRealmName()
	WowMCPDB.auction[realm] = WowMCPDB.auction[realm] or {}
	return WowMCPDB.auction[realm]
end

local function ItemName(itemID)
	local ok, name = pcall(C_Item.GetItemNameByID, itemID)
	return ok and type(name) == "string" and name or nil
end

local function Report(err)
	problem = tostring(err)
end

function Auction.Price(itemID)
	return Prices()[itemID]
end

function Auction.IsOpen()
	return visit ~= nil
end

-- What the n cheapest cost, from the price points kept: copper, and how many they cover.
function Auction.Cost(entry, n)
	local total, got = 0, 0
	for _, tier in ipairs(entry.tiers or {}) do
		local take = math.min(tier.count, n - got)
		if take <= 0 then break end
		total, got = total + take * tier.price, got + take
	end
	return total, got
end

local function Store(itemID, entry)
	local prices = Prices()
	entry.at, entry.name = time(), ItemName(itemID) or (prices[itemID] and prices[itemID].name)
	entry.zone, entry.faction = GetRealZoneText(), UnitFactionGroup("player")
	local subzone = GetSubZoneText()
	entry.subzone = subzone ~= "" and subzone or nil
	prices[itemID] = entry
	local count, oldest = 0, nil
	for id, e in pairs(prices) do
		count = count + 1
		if not oldest or e.at < prices[oldest].at then oldest = id end
	end
	if count > MAX_CACHED then prices[oldest] = nil end
end

---------------------------------------------------------------------------
-- Reading results
---------------------------------------------------------------------------

-- Commodities come cheapest first, a price point each; the player's own are left out.
local function ReadCommodity(itemID)
	local entry = { commodity = true, tiers = {}, listed = 0 }
	for i = 1, C_AuctionHouse.GetNumCommoditySearchResults(itemID) do
		local r = C_AuctionHouse.GetCommoditySearchResultInfo(itemID, i)
		local available = r and r.quantity - (r.numOwnerItems or 0) or 0
		if available > 0 then
			entry.listed = entry.listed + available
			if #entry.tiers < MAX_TIERS then table.insert(entry.tiers, { price = r.unitPrice, count = available }) end
		end
	end
	if not C_AuctionHouse.HasFullCommoditySearchResults(itemID) then
		-- Only the cheapest have arrived; the total is the whole auction house's.
		entry.listed = math.max(entry.listed, C_AuctionHouse.GetCommoditySearchResultsQuantity(itemID) or 0)
	end
	entry.price = entry.tiers[1] and entry.tiers[1].price
	return entry
end

-- Other items are single auctions, maybe with only a bid: priced per item by buyout.
local function ReadItems(itemKey)
	local entry, points = { tiers = {}, listed = 0 }, {}
	for i = 1, C_AuctionHouse.GetNumItemSearchResults(itemKey) do
		local r = C_AuctionHouse.GetItemSearchResultInfo(itemKey, i)
		if r and not r.containsOwnerItem and (r.quantity or 0) > 0 then
			entry.listed = entry.listed + r.quantity
			if r.buyoutAmount then
				table.insert(points, { price = math.floor(r.buyoutAmount / r.quantity + 0.5), count = r.quantity })
			else
				entry.bidOnly = (entry.bidOnly or 0) + r.quantity
			end
		end
	end
	table.sort(points, function(a, b) return a.price < b.price end)
	for _, p in ipairs(points) do
		local last = entry.tiers[#entry.tiers]
		if last and last.price == p.price then
			last.count = last.count + p.count
		elseif #entry.tiers < MAX_TIERS then
			table.insert(entry.tiers, p)
		end
	end
	entry.price = entry.tiers[1] and entry.tiers[1].price
	return entry
end

---------------------------------------------------------------------------
-- Looking up
---------------------------------------------------------------------------

-- Whether a search now leaves what the player sees alone: Blizzard's window on its Browse view.
local function Safe()
	local f = AuctionHouseFrame
	if not (f and f:IsShown() and f.GetDisplayMode and AuctionHouseFrameDisplayMode) then
		return false, "the game's auction house window isn't showing"
	end
	if f:GetDisplayMode() ~= AuctionHouseFrameDisplayMode.Buy then
		return false, "waiting for the Browse view, so the lookups don't change what you're looking at"
	end
	return true
end

-- Goals first, then the plan, then the watch list: { { item, group } }, each item once. Watched items
-- priced longest ago come first, so a long list gets round over a few visits.
local function Wanted()
	local list, seen, watched = {}, {}, {}
	local function Add(itemID, group, into)
		itemID = tonumber(itemID)
		if itemID and not seen[itemID] then
			seen[itemID] = true
			table.insert(into or list, { item = itemID, group = group, n = #(into or list) })
		end
	end
	for _, w in ipairs(ns.Goals.Items()) do Add(w.item, "goal") end
	for _, w in ipairs(ns.Plans.Items()) do Add(w.item, "plan") end
	for _, itemID in ipairs(watch) do Add(itemID, "watch", watched) end
	local prices = Prices()
	local function At(w) return prices[w.item] and prices[w.item].at or 0 end
	table.sort(watched, function(a, b)
		if At(a) ~= At(b) then return At(a) < At(b) end
		return a.n < b.n
	end)
	for _, w in ipairs(watched) do list[#list + 1] = w end
	return list
end

-- force: look everything up again (/mcp prices), recent prices too.
local function Start(force)
	visit = { queue = {}, groups = {}, notBefore = GetTime() + (force and 0 or START_DELAY), count = 0 }
	local prices = Prices()
	for _, w in ipairs(Wanted()) do
		visit.groups[w.item] = w.group
		local cached = prices[w.item]
		if (force or not (cached and time() - cached.at < FRESH)) and #visit.queue < MAX_PER_VISIT then
			table.insert(visit.queue, w.item)
		end
	end
	return #visit.queue
end

local function Send(itemID)
	local key = C_AuctionHouse.MakeItemKey(itemID)
	local ok, info = pcall(C_AuctionHouse.GetItemKeyInfo, key)
	-- Unknown until the client has the item; an item search's sorts are right for both then.
	local commodity = ok and type(info) == "table" and info.isCommodity
	local context = AuctionHouseSearchContext
		and (commodity and AuctionHouseSearchContext.BuyCommodities or AuctionHouseSearchContext.BuyItems)
	local sorts
	if context and AuctionHouseFrame.GetSortsForContext then
		local got, list = pcall(AuctionHouseFrame.GetSortsForContext, AuctionHouseFrame, context)
		sorts = got and type(list) == "table" and list or nil
	end
	if not sorts or (#sorts == 0 and not commodity) then sorts = commodity and {} or ITEM_SORTS end
	current = { item = itemID, at = GetTime() }
	table.insert(sent, GetTime())
	visit.count = visit.count + 1
	C_AuctionHouse.SendSearchQuery(key, sorts, false)
end

local function Pending(group)
	if current and visit.groups[current.item] == group then return true end
	for _, itemID in ipairs(visit.queue) do
		if visit.groups[itemID] == group then return true end
	end
	return false
end

-- Once per visit, when the goals' and the plan's items have their prices: Goals.lua points out the
-- ones listed here.
local function Tell()
	if visit.told or Pending("goal") or Pending("plan") then return end
	visit.told = true
	local prices, mine = Prices(), {}
	for itemID, group in pairs(visit.groups) do
		if group ~= "watch" then mine[itemID] = prices[itemID] end
	end
	if next(mine) then ns.Goals.AuctionPrices(mine) end
end

local function Step()
	if not visit then return end
	local now = GetTime()
	if current and now - current.at > TIMEOUT then current = nil end -- no answer: on to the next
	if current then return end
	Tell()
	if #visit.queue == 0 then
		if visit.count > 0 and not visit.finished then
			visit.finished = true
			ns.Print(string.format("%sNoted the auction prices of %d item%s for your AI.|r", GREY, visit.count,
				visit.count > 1 and "s" or ""))
		end
		return
	end
	local safe, why = Safe()
	visit.waiting = not safe and why or nil
	if not safe or now < visit.notBefore or not C_AuctionHouse.IsThrottledMessageSystemReady() then return end
	for i = #sent, 1, -1 do
		if now - sent[i] >= 60 then table.remove(sent, i) end
	end
	if #sent >= MAX_PER_MINUTE then return end
	Send(table.remove(visit.queue, 1))
end

local function Result(itemID, read, arg)
	local ok, entry = pcall(read, arg)
	if not ok then
		Report(entry)
		return
	end
	-- The player's own searches are noted too; they cost nothing.
	Store(itemID, entry)
	if current and current.item == itemID then current = nil end
end

---------------------------------------------------------------------------

local function Line(itemID, entry)
	local name = ns.Escape(entry.name or ItemName(itemID) or ("item " .. itemID))
	if not entry.price then
		return string.format("%s: none listed %s(%s)|r", name, GREY, date("%d %b %H:%M", entry.at))
	end
	return string.format("%s: %s each, %d listed %s(%s)|r", name, ns.Goals.Money(entry.price), entry.listed, GREY,
		date("%d %b %H:%M", entry.at))
end

function Auction.Command()
	if visit then
		local count = Start(true)
		if count == 0 then
			ns.Print("Nothing to look up: your goals, plan and AI don't need any items right now.")
			return
		end
		local safe, why = Safe()
		ns.Print(string.format("Looking up the auction prices of %d item%s%s.", count, count > 1 and "s" or "",
			safe and "" or (" (" .. why .. ")")))
		return
	end
	local prices, lines = Prices(), {}
	for _, w in ipairs(Wanted()) do
		if prices[w.item] then lines[#lines + 1] = Line(w.item, prices[w.item]) end
	end
	if #lines == 0 then
		ns.Print("No auction prices for what your goals, plan and AI need yet: they're looked up when you open the auction house.")
		return
	end
	ns.Print("Auction prices last seen:")
	for _, line in ipairs(lines) do ns.Print(line) end
end

-- For get_auction_prices: this realm's cached prices, newest first.
local function Section()
	local list = {}
	for itemID, e in pairs(Prices()) do
		list[#list + 1] = {
			item = itemID, name = e.name or ItemName(itemID), price = e.price, listed = e.listed, tiers = e.tiers,
			commodity = e.commodity, bidOnly = e.bidOnly, at = e.at, zone = e.zone, subzone = e.subzone, faction = e.faction,
		}
	end
	table.sort(list, function(a, b) return a.at > b.at end)
	return {
		realm = GetRealmName(), prices = list, watch = watch, open = visit ~= nil,
		left = visit and #visit.queue or nil, waiting = visit and visit.waiting or nil, problem = problem,
	}
end

ns.RegisterReceiver("auctionWatch", function(payload)
	if type(payload) ~= "table" then return end
	watch = {}
	for _, w in ipairs(type(payload.items) == "table" and payload.items or {}) do
		if type(w) == "table" and tonumber(w.item) then watch[#watch + 1] = tonumber(w.item) end
	end
end)
ns.RegisterSection(ns.Kinds.KNOWLEDGE, "auction", Section)
ns.RegisterCommand("prices", "/mcp prices - auction prices of what your goals, plan and AI need: look them up "
	.. "again (at the auction house) or list the last ones seen", Auction.Command)
ns.OnLogin(function()
	C_Timer.NewTicker(STEP_INTERVAL, function()
		local ok, err = pcall(Step)
		if not ok then Report(err) end
	end)
end)

local AUCTIONEER = Enum.PlayerInteractionType and Enum.PlayerInteractionType.Auctioneer or 21

local function Open()
	if visit then return end -- AUCTION_HOUSE_SHOW and the interaction event both say so
	local ok, err = pcall(Start)
	if not ok then Report(err) end
end

local function Close()
	visit, current = nil, nil
end

local events = CreateFrame("Frame")
for _, event in ipairs({
	"AUCTION_HOUSE_SHOW", "AUCTION_HOUSE_CLOSED", "PLAYER_INTERACTION_MANAGER_FRAME_SHOW",
	"PLAYER_INTERACTION_MANAGER_FRAME_HIDE", "COMMODITY_SEARCH_RESULTS_UPDATED", "ITEM_SEARCH_RESULTS_UPDATED",
	"AUCTION_HOUSE_THROTTLED_MESSAGE_DROPPED",
}) do
	pcall(events.RegisterEvent, events, event)
end
events:SetScript("OnEvent", function(_, event, arg1)
	if event == "AUCTION_HOUSE_SHOW" or (event == "PLAYER_INTERACTION_MANAGER_FRAME_SHOW" and arg1 == AUCTIONEER) then
		Open()
	elseif event == "AUCTION_HOUSE_CLOSED" or (event == "PLAYER_INTERACTION_MANAGER_FRAME_HIDE" and arg1 == AUCTIONEER) then
		Close()
	elseif event == "COMMODITY_SEARCH_RESULTS_UPDATED" and tonumber(arg1) then
		Result(arg1, ReadCommodity, arg1)
	elseif event == "ITEM_SEARCH_RESULTS_UPDATED" and type(arg1) == "table" and arg1.itemID then
		Result(arg1.itemID, ReadItems, arg1)
	elseif event == "AUCTION_HOUSE_THROTTLED_MESSAGE_DROPPED" and current and visit then
		-- The game dropped a search (too many at once): try that item once more, a little later.
		if not visit.retried then
			visit.retried = true
			table.insert(visit.queue, current.item)
		end
		visit.notBefore = GetTime() + TIMEOUT
		current = nil
	end
end)
