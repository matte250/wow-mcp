-- WowMCP auction prices: keeps a picture of what things cost on the player's realm that's always there for
-- the AI (section "auction" of request kind KNOWLEDGE, get_auction_prices tool), and for goals and plans.
-- Prices come from the auction house while the player has it open, two ways:
--   * A full scan (C_AuctionHouse.ReplicateItems) whenever the game allows one (every 15 minutes): every
--     auction at once. Per item ID it keeps the cheapest buyout per item, how many are listed and in how
--     many auctions, with the scan's time, for every item any scan has seen (one that's gone keeps its last
--     price, marked as not listed at the latest scan). Priority items also keep their cheapest few price
--     points. The scan is read a little per frame, so the game never stutters.
--   * Item searches (C_AuctionHouse.SendSearchQuery) for the priority items the full scan didn't cover
--     recently: the game didn't allow one this visit, or it's more than 10 minutes old. Stalest first.
-- The priority list (at most 100) is what the AI tracks: goal and plan items, items watched with
-- watch_auction_prices and items it asked prices for (payload key "auctionWatch"). Adding one again
-- refreshes it; the oldest go first. Room left is filled with what the player could sell from their bags
-- and bank (as last seen): not soulbound, not quest items, and worth something to a vendor.
--
-- The lookups must not disturb the player's own browsing. Searches are item searches, and in Blizzard's
-- auction house window (Blizzard_AuctionHouseUI, Shared and Mainline code on Forever) the Browse view only
-- listens to browse queries, while the item, commodity and sell views redraw on every search result,
-- whichever item it's for, and the sell views pick a price from it. So searches only go out while the
-- window shows its Browse view, one at a time, when the game's request throttle is ready, with at most
-- MAX_PER_MINUTE auction house messages a minute counting the player's own (the game allows 100), and with
-- the sorts the window itself uses for that kind of search, so its own later refresh of the item looks the
-- same. If the player opens an item or starts selling, the lookups wait until they're back on the Browse
-- view. No part of the window uses ReplicateItems or listens to its event (REPLICATE_ITEM_LIST_UPDATE);
-- it's only asked for on the Browse view too, in case it holds up the throttled channel for a moment.

local _, ns = ...

local Auction = {}
ns.Auction = Auction

local STEP_INTERVAL = 0.5
local START_DELAY = 1     -- the window's own opening searches (bids, favorites) go first
local TIMEOUT = 5         -- seconds without an answer: that search is given up
local MAX_PER_MINUTE = 80 -- auction house messages, the player's too: the game allows 100
local FRESH = 10 * 60     -- prices this recent (scan or search) aren't looked up again
local MAX_PRIORITY = 100
local MAX_DETAILED = 300  -- items with their price points kept per realm; the oldest go first
local MAX_TIERS = 6       -- the cheapest price points kept per item
local MAX_LOOKUP = 100
local SCAN_INTERVAL = 15 * 60 -- how often the game allows a full scan
local SCAN_WAIT = 10      -- seconds without the full scan's results: the game didn't allow it, so search
local BUDGET_MS = 1       -- frame time per frame for reading a full scan (Stream.lua sends with 1.5)
local RANK = { goal = 1, plan = 2, watch = 3, asked = 4 } -- why the AI tracks an item, for ties
local QUEST_CLASS = 12    -- Enum.ItemClass.Questitem
local BOUND = { [1] = true, [4] = true, [7] = true, [8] = true, [9] = true } -- Enum.ItemBind: picked up, quest, account
local SORT = Enum.AuctionHouseSortOrder or { Bid = 3, Buyout = 4 }
local ITEM_SORTS = { -- the window's defaults for item searches
	{ sortOrder = SORT.Buyout, reverseSort = false }, { sortOrder = SORT.Bid, reverseSort = false },
}
local GREY = "|cffaaaaaa"

local visit   -- while the auction house is open: { priority, searched, count, scan = { state, at }, ... }
local current -- our search in flight: { item, at }
local sent = {}     -- GetTime() of our messages in the last minute
local messages = {} -- GetTime() of every auction house message in the last minute, the player's too
local reader  -- a full scan being read (see StartRead)
local lookup  -- what get_auction_prices asks for: { id, items }
local problem

-- WowMCPDB.auction[realm] = {
--   items = { [item ID] = { price (cheapest, per item, copper), listed, n (auctions), tiers = { { price, count } },
--     commodity, bidOnly, name, at (unix time), src ("search" or "scan"), zone, subzone, faction } }: an item's last
--     search, or the last full scan of a priority item, with its cheapest price points;
--   scan = { [item ID] = "price,listed,auctions,scan" }: every item a full scan has seen, as the last scan that saw
--     it did (scan: its number in scans; no price if there were only bids). Kept compact: that's thousands;
--   scans = { { at, items, listings, zone, subzone, faction } }, current = the number of the newest complete one;
--   tracked = { [item ID] = { at, why, n } }: what the AI tracks (why: goal, plan, watch, asked).
-- }
local function Realm()
	WowMCPDB.auction = WowMCPDB.auction or {}
	local name = GetRealmName()
	local realm = WowMCPDB.auction[name] or {}
	WowMCPDB.auction[name] = realm
	if not realm.items then
		-- Before full scans, this table held the searched items themselves.
		local items = {}
		for k, v in pairs(realm) do
			if type(k) == "number" then items[k] = v end
		end
		for k in pairs(items) do realm[k] = nil end
		realm.items = items
	end
	realm.scan, realm.scans, realm.tracked = realm.scan or {}, realm.scans or {}, realm.tracked or {}
	return realm
end

-- When a full scan was last asked for, ours or another addon's; kept for the account, in case the game's
-- limit is.
local function Asked()
	return WowMCPDB.auctionScan and WowMCPDB.auctionScan.asked or 0
end

local function SetAsked(at)
	WowMCPDB.auctionScan = { asked = at }
end

local function ItemName(itemID)
	local ok, name = pcall(C_Item.GetItemNameByID, itemID)
	return ok and type(name) == "string" and name or nil
end

local function Report(err)
	problem = tostring(err)
end

local function Here()
	local subzone = GetSubZoneText()
	return { zone = GetRealZoneText(), subzone = subzone ~= "" and subzone or nil, faction = UnitFactionGroup("player") }
end

local function CurrentScan(realm)
	return realm.current and realm.scans[realm.current]
end

-- A full scan's entry: price (nil if there were only bids), listed, auctions, scan number.
local function Decode(entry)
	local price, listed, n, k = tostring(entry or ""):match("^(%d*),(%d+),(%d+),(%d+)$")
	if not k then return nil end
	return tonumber(price), tonumber(listed), tonumber(n), tonumber(k)
end

-- What's known of an item's price, from whichever looked last: { price, listed, n, tiers, bidOnly, commodity,
-- name, at, src, zone, subzone, faction }, plus last = { price, listed, at } when none were listed then but
-- were before. A full scan covers every item: one it didn't see wasn't listed. nil if nothing has looked.
local function Known(realm, itemID)
	local detail, latest = realm.items[itemID], CurrentScan(realm)
	local price, listed, n, k = Decode(realm.scan[itemID])
	local entry
	if detail and not (latest and latest.at > detail.at) then
		entry = {}
		for key, value in pairs(detail) do entry[key] = value end
		entry.src = detail.src or "search"
	elseif latest then
		-- A scan's entries with a number past current are being stored right now.
		local seen = k and k >= realm.current and realm.scans[k]
		local scan = seen or latest
		entry = { listed = 0, at = scan.at, src = "scan", zone = scan.zone, subzone = scan.subzone, faction = scan.faction,
			name = detail and detail.name }
		if seen then entry.price, entry.listed, entry.n = price, listed, n end
	else
		return nil
	end
	if (entry.listed or 0) == 0 then
		-- The last time some were, by a scan or a search.
		local scanned = k and price and realm.scans[k]
		if scanned and scanned.at < entry.at then entry.last = { price = price, listed = listed, at = scanned.at } end
		if detail and detail.price and detail.at < entry.at and not (entry.last and entry.last.at >= detail.at) then
			entry.last = { price = detail.price, listed = detail.listed, at = detail.at }
		end
	end
	return entry
end

function Auction.Price(itemID)
	return Known(Realm(), itemID)
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

local function Store(itemID, entry, at, where)
	local items = Realm().items
	entry.at, entry.name = at, ItemName(itemID) or (items[itemID] and items[itemID].name)
	entry.zone, entry.subzone, entry.faction = where.zone, where.subzone, where.faction
	items[itemID] = entry
	local count, oldest = 0, nil
	for id, e in pairs(items) do
		count = count + 1
		if not oldest or e.at < items[oldest].at then oldest = id end
	end
	if count > MAX_DETAILED then items[oldest] = nil end
end

---------------------------------------------------------------------------
-- The priority list
---------------------------------------------------------------------------

-- Adds an item to what the AI tracks, or refreshes it. Asking about an item doesn't change why it was
-- tracked before.
local function Track(tracked, itemID, why, at, n)
	itemID, at = tonumber(itemID), tonumber(at) or 0
	if not itemID then return end
	local t = tracked[itemID]
	if not t or t.at < at or (t.at == at and RANK[why] < (RANK[t.why] or 9)) then
		tracked[itemID] = { at = at, why = (why == "asked" and t) and t.why or why, n = n }
	end
end

-- What the AI tracks, newest first: { { item, why } }. The goals' and the plan's items are refreshed, as
-- they're what the player needs now; past MAX_PRIORITY the oldest are dropped.
local function Tracked()
	local tracked, now = Realm().tracked, time()
	for i, w in ipairs(ns.Goals.Items()) do Track(tracked, w.item, "goal", now, i) end
	for i, w in ipairs(ns.Plans.Items()) do Track(tracked, w.item, "plan", now, i) end
	local list = {}
	for itemID, t in pairs(tracked) do
		list[#list + 1] = { item = itemID, why = t.why, at = t.at or 0, rank = RANK[t.why] or 9, n = t.n or 0 }
	end
	table.sort(list, function(a, b)
		if a.at ~= b.at then return a.at > b.at end
		if a.rank ~= b.rank then return a.rank < b.rank end
		if a.n ~= b.n then return a.n < b.n end
		return a.item < b.item
	end)
	for i = #list, MAX_PRIORITY + 1, -1 do
		tracked[list[i].item] = nil
		list[i] = nil
	end
	return list
end

-- Whether the auction house could take an item: not bound to the player, not a quest item, and worth
-- something to a vendor (what isn't, e.g. conjured food or keys, can't go up for auction either).
local function Sellable(itemID, bound, sell)
	if bound then return false end
	local ok, name, _, _, _, _, _, _, _, _, _, sellPrice, class, _, bind = pcall(C_Item.GetItemInfo, itemID)
	if ok and name and sellPrice then
		return not BOUND[bind] and class ~= QUEST_CLASS and sellPrice > 0
	end
	-- Not loaded yet: what the bag said, and the item's class from the client's own data.
	local _, _, _, _, _, class2 = C_Item.GetItemInfoInstant(itemID)
	return (sell or 0) > 0 and class2 ~= QUEST_CLASS
end

-- What the player could sell, from their bags and bank (as Inventory.lua last saw it), most valuable first:
-- { { item, why = "bags" | "bank" } }.
local function Baseline(realm)
	local found, list = {}, {}
	local function Add(itemID, count, why, bound, sell)
		if type(itemID) ~= "number" then return end
		local f = found[itemID]
		if not f then
			if found[itemID] == false then return end
			local ok, yes = pcall(Sellable, itemID, bound, sell)
			if not (ok and yes) then
				found[itemID] = false
				return
			end
			f = { item = itemID, why = why, count = 0 }
			found[itemID], list[#list + 1] = f, f
		end
		f.count = f.count + (count or 1)
	end
	for bag = Enum.BagIndex.Backpack, Enum.BagIndex.ReagentBag do
		for slot = 1, C_Container.GetContainerNumSlots(bag) do
			local info = C_Container.GetContainerItemInfo(bag, slot)
			if info and info.itemID then
				Add(info.itemID, info.stackCount, "bags", info.isBound, not info.hasNoValue and 1 or 0)
			end
		end
	end
	local bank = WowMCPDB.bank and WowMCPDB.bank[tostring(UnitName("player")) .. "-" .. tostring(GetRealmName())]
	for _, container in ipairs(bank and bank.bags or {}) do
		for _, item in ipairs(container.items or {}) do Add(item.id, item.count, "bank", item.bound, item.sell) end
	end
	-- Worth: the auction price as last seen, else what a vendor pays.
	for _, f in ipairs(list) do
		local known = Known(realm, f.item)
		local each = known and (known.price or (known.last and known.last.price))
		if not each then
			local ok, _, _, _, _, _, _, _, _, _, _, sellPrice = pcall(C_Item.GetItemInfo, f.item)
			each = ok and sellPrice or 0
		end
		f.worth = each * f.count
	end
	table.sort(list, function(a, b)
		if a.worth ~= b.worth then return a.worth > b.worth end
		if a.why ~= b.why then return a.why == "bags" end
		return a.item < b.item
	end)
	return list
end

-- What's checked at every visit: the AI's items first, then the bags' and bank's: { { item, why, ai } }.
local function Priority()
	local list, seen = {}, {}
	for _, t in ipairs(Tracked()) do
		list[#list + 1] = { item = t.item, why = t.why, ai = true }
		seen[t.item] = true
	end
	for _, b in ipairs(Baseline(Realm())) do
		if #list >= MAX_PRIORITY then break end
		if not seen[b.item] then list[#list + 1] = { item = b.item, why = b.why } end
	end
	return list
end

---------------------------------------------------------------------------
-- Reading search results
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

-- Price points (price per item, count) cheapest first, with equal prices merged: the cheapest MAX_TIERS.
local function Tiers(points)
	table.sort(points, function(a, b) return a.price < b.price end)
	local tiers = {}
	for _, p in ipairs(points) do
		local last = tiers[#tiers]
		if last and last.price == p.price then
			last.count = last.count + p.count
		elseif #tiers < MAX_TIERS then
			table.insert(tiers, p)
		else
			break
		end
	end
	return tiers
end

-- Other items are single auctions, maybe with only a bid: priced per item by buyout.
local function ReadItems(itemKey)
	local entry, points = { listed = 0, n = 0 }, {}
	for i = 1, C_AuctionHouse.GetNumItemSearchResults(itemKey) do
		local r = C_AuctionHouse.GetItemSearchResultInfo(itemKey, i)
		if r and not r.containsOwnerItem and (r.quantity or 0) > 0 then
			entry.listed, entry.n = entry.listed + r.quantity, entry.n + 1
			if r.buyoutAmount then
				table.insert(points, { price = math.floor(r.buyoutAmount / r.quantity + 0.5), count = r.quantity })
			else
				entry.bidOnly = (entry.bidOnly or 0) + r.quantity
			end
		end
	end
	entry.tiers = Tiers(points)
	entry.price = entry.tiers[1] and entry.tiers[1].price
	return entry
end

---------------------------------------------------------------------------
-- Reading a full scan, a little per frame
---------------------------------------------------------------------------

local scanner = CreateFrame("Frame")
local StartRead

local function StopReading(state)
	reader = nil
	scanner:SetScript("OnUpdate", nil)
	if visit and visit.scan then visit.scan.state = state end
end

-- One auction (the index counts from 0). Its item ID, count, buyout and owner are there before the item's
-- own data has loaded (hasAllInfo), which only the name, texture and link need.
local function ReadOne(r)
	local i = r.index
	r.index = i + 1
	-- name, texture, count, qualityID, usable, level, levelType, minBid, minIncrement, buyoutPrice, bidAmount,
	-- highBidder, bidderFullName, owner, ownerFullName, saleStatus, itemID, hasAllInfo
	local ok, _, _, count, _, _, _, _, _, _, buyout, _, _, _, owner, _, _, itemID =
		pcall(C_AuctionHouse.GetReplicateItemInfo, i)
	if not (ok and type(itemID) == "number" and type(count) == "number" and count > 0) or owner == r.me then return end
	if not r.listed[itemID] then
		r.ids[#r.ids + 1] = itemID
		r.listed[itemID], r.n[itemID] = 0, 0
	end
	r.listed[itemID], r.n[itemID] = r.listed[itemID] + count, r.n[itemID] + 1
	local points = r.points[itemID]
	if type(buyout) == "number" and buyout > 0 then
		local each = buyout / count
		if not r.price[itemID] or each < r.price[itemID] then r.price[itemID] = each end
		if points then points[#points + 1] = { price = math.floor(each + 0.5), count = count } end
	elseif points then
		r.bids[itemID] = (r.bids[itemID] or 0) + count
	end
end

local function StoreOne(r)
	local itemID = r.ids[r.index]
	r.index = r.index + 1
	local price = r.price[itemID]
	r.realm.scan[itemID] = string.format("%s,%d,%d,%d", price and tostring(math.floor(price + 0.5)) or "",
		r.listed[itemID], r.n[itemID], r.k)
end

-- A priority item seen in the scan gets its price points too, as a search would.
local function DetailOne(r)
	local itemID = table.remove(r.queue)
	local tiers = Tiers(r.points[itemID])
	Store(itemID, { price = tiers[1] and tiers[1].price, listed = r.listed[itemID], n = r.n[itemID], tiers = tiers,
		bidOnly = r.bids[itemID], src = "scan" }, r.at, r.realm.scans[r.k])
end

local function ReadSome()
	local r, started = reader, debugprofilestop()
	if r.phase == "read" and C_AuctionHouse.GetNumReplicateItems() ~= r.total then
		-- The list changed under us (it went with the window?): this scan is lost.
		return StopReading("lost")
	end
	while reader == r and debugprofilestop() - started < BUDGET_MS do
		if r.phase == "read" then
			if r.index < r.total then
				ReadOne(r)
			else
				-- Read: the scan's own entry first, so stored items have a time.
				r.realm.scans[r.k] = { at = r.at, items = #r.ids, listings = r.total, zone = r.where.zone,
					subzone = r.where.subzone, faction = r.where.faction }
				r.phase, r.index = "store", 1
			end
		elseif r.phase == "store" then
			if r.ids[r.index] then
				StoreOne(r)
			else
				r.phase, r.queue = "detail", {}
				for itemID in pairs(r.points) do
					if r.listed[itemID] then r.queue[#r.queue + 1] = itemID end
				end
			end
		elseif r.queue[1] then
			DetailOne(r)
		else
			r.realm.current = math.max(r.realm.current or 0, r.k)
			if visit and visit.scan then visit.scanned = #r.ids end
			local again = r.again
			StopReading("done")
			if again then StartRead() end
		end
	end
end

-- REPLICATE_ITEM_LIST_UPDATE: the full scan's auctions are there, whoever asked for them.
function StartRead()
	if reader then
		reader.again = true -- the same scan's event again: read it once more when done
		return
	end
	local total = C_AuctionHouse.GetNumReplicateItems() or 0
	if total == 0 then return end -- nothing to read (the window closed first?), not an empty auction house
	local realm = Realm()
	local latest = CurrentScan(realm)
	local ours = visit and visit.scan and visit.scan.state == "asked"
	local k, at = realm.current, latest and latest.at
	if ours or not latest or time() - latest.at >= SCAN_INTERVAL then
		k, at = #realm.scans + 1, time()
		if not ours then SetAsked(time()) end -- another addon's: the game won't allow one for a while
	end
	local points = {}
	for _, p in ipairs(visit and visit.priority or Priority()) do points[p.item] = {} end
	reader = {
		realm = realm, k = k, at = at, where = Here(), total = total, index = 0, me = UnitName("player"),
		phase = "read", ids = {}, price = {}, listed = {}, n = {}, bids = {}, points = points,
	}
	if visit then
		visit.scan = visit.scan or {}
		visit.scan.state = "reading"
	end
	scanner:SetScript("OnUpdate", function()
		local ok, err = pcall(ReadSome)
		if not ok then
			Report(err)
			StopReading("lost")
		end
	end)
end

---------------------------------------------------------------------------
-- Looking up
---------------------------------------------------------------------------

-- Whether a lookup now leaves what the player sees alone: Blizzard's window on its Browse view.
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

local function Scanning()
	return reader ~= nil or (visit and visit.scan and visit.scan.state == "asked") or false
end

local function ScanAllowed()
	return C_AuctionHouse.ReplicateItems ~= nil and not reader and time() - Asked() >= SCAN_INTERVAL
end

-- When an item's price was last looked at: a search of it, or a full scan (which covers every item).
local function Checked(realm, itemID)
	local detail, latest = realm.items[itemID], CurrentScan(realm)
	return math.max(detail and detail.at or 0, latest and latest.at or 0)
end

local function Due(realm, itemID)
	return not visit.searched[itemID] and (visit.force or Checked(realm, itemID) < time() - FRESH)
end

-- The priority item to search next: the stalest, the AI's before the bags' and bank's.
local function NextSearch()
	local realm, best, bestAt = Realm(), nil, nil
	for _, p in ipairs(visit.priority) do
		if Due(realm, p.item) then
			local at = Checked(realm, p.item)
			if not best or at < bestAt then best, bestAt = p, at end
		end
	end
	return best
end

local function Left()
	local realm, left = Realm(), 0
	for _, p in ipairs(visit.priority) do
		if Due(realm, p.item) then left = left + 1 end
	end
	return left
end

local function Start()
	visit = { priority = Priority(), searched = {}, notBefore = GetTime() + START_DELAY, count = 0 }
end

-- /mcp prices at the auction house: the whole priority list again, recent prices too.
local function Again()
	visit.priority, visit.searched, visit.force = Priority(), {}, true
	visit.notBefore, visit.told, visit.finished = GetTime(), nil, nil
	return #visit.priority
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
	visit.searched[itemID] = true
	visit.count = visit.count + 1
	C_AuctionHouse.SendSearchQuery(key, sorts, false)
end

local function Replicate()
	SetAsked(time())
	visit.scan = { state = "asked", at = GetTime() }
	table.insert(sent, GetTime())
	C_AuctionHouse.ReplicateItems()
end

-- Once per visit, when the goals' and the plan's items have their prices: Goals.lua points out the ones
-- listed here.
local function Tell()
	if visit.told or Scanning() then return end
	local realm, mine, listed, needed = Realm(), {}, {}, {}
	for _, p in ipairs(visit.priority) do listed[p.item] = true end
	for _, w in ipairs(ns.Goals.Items()) do needed[#needed + 1] = w.item end
	for _, w in ipairs(ns.Plans.Items()) do needed[#needed + 1] = w.item end
	for _, itemID in ipairs(needed) do
		if (current and current.item == itemID) or (listed[itemID] and Due(realm, itemID)) then return end
		mine[itemID] = Known(realm, itemID)
	end
	visit.told = true
	if next(mine) then ns.Goals.AuctionPrices(mine) end
end

local function Finished()
	if visit.finished or (visit.count == 0 and not visit.scanned) then return end
	visit.finished = true
	local how = ""
	if visit.scanned then
		how = visit.count > 0 and string.format(" (a full scan of the auction house, and %d lookup%s)", visit.count,
			visit.count > 1 and "s" or "") or " (a full scan of the auction house)"
	end
	local items = math.max(visit.scanned or 0, visit.count)
	ns.Print(string.format("%sNoted the auction prices of %d item%s for your AI%s.|r", GREY, items,
		items > 1 and "s" or "", how))
end

local function Prune(list, now)
	for i = #list, 1, -1 do
		if now - list[i] >= 60 then table.remove(list, i) end
	end
end

local function Step()
	if not visit then return end
	local now = GetTime()
	if current and now - current.at > TIMEOUT then current = nil end -- no answer: on to the next
	if current then return end
	local scan = visit.scan
	if scan and scan.state == "asked" and now - scan.at > SCAN_WAIT and not reader then
		scan.state = "refused" -- the game didn't send it (another addon's scan used it up?): search instead
	end
	Tell()
	local safe, why = Safe()
	visit.waiting = not safe and why or nil
	if not safe or now < visit.notBefore or not C_AuctionHouse.IsThrottledMessageSystemReady() then return end
	Prune(sent, now)
	Prune(messages, now)
	if math.max(#sent, #messages) >= MAX_PER_MINUTE then return end
	if ScanAllowed() then return Replicate() end
	if Scanning() then return end
	local wanted = NextSearch()
	if wanted then return Send(wanted.item) end
	Finished()
end

local function Result(itemID, read, arg)
	local ok, entry = pcall(read, arg)
	if not ok then
		Report(entry)
		return
	end
	-- The player's own searches are noted too; they cost nothing.
	entry.src = "search"
	Store(itemID, entry, time(), Here())
	if current and current.item == itemID then current = nil end
end

---------------------------------------------------------------------------
-- /mcp prices, and get_auction_prices
---------------------------------------------------------------------------

local function Ago(at)
	local minutes = math.floor((time() - at) / 60)
	if minutes < 1 then return "just now" end
	if minutes < 120 then return string.format("%d min ago", minutes) end
	if minutes < 48 * 60 then return string.format("%d hours ago", math.floor(minutes / 60)) end
	return string.format("%d days ago", math.floor(minutes / 1440))
end

-- The full scan, the priority list and the searches still to come, in a line.
local function State(priority)
	local latest, ai = CurrentScan(Realm()), 0
	for _, p in ipairs(priority) do
		if p.ai then ai = ai + 1 end
	end
	local scan = latest and string.format("last full scan %s (%d items)", Ago(latest.at), latest.items)
		or "no full scan yet"
	local wait = Asked() + SCAN_INTERVAL - time()
	if reader then
		scan = scan .. ", reading a new one"
	elseif wait > 0 then
		scan = scan .. string.format(", next one possible in %d min", math.ceil(wait / 60))
	else
		scan = scan .. ", next one at the auction house"
	end
	local searches = visit and Left()
	local left = searches and string.format("; %d search%s left this visit", searches, searches == 1 and "" or "es") or ""
	return string.format("Auction prices: %s. Priority list: %d items (%d your AI tracks, %d from your bags and bank)%s.",
		scan, #priority, ai, #priority - ai, left)
end

local function Line(itemID, e)
	local name = ns.Escape(e.name or ItemName(itemID) or ("item " .. itemID))
	local when = string.format("%s(%s%s)|r", GREY, date("%d %b %H:%M", e.at), e.src == "scan" and ", full scan" or "")
	if e.price then
		return string.format("%s: %s each, %d listed %s", name, ns.Goals.Money(e.price), e.listed, when)
	end
	if (e.listed or 0) > 0 then return string.format("%s: %d listed, only bids %s", name, e.listed, when) end
	local last = e.last and string.format(", last %s at %s", date("%d %b", e.last.at), ns.Goals.Money(e.last.price)) or ""
	return string.format("%s: none listed%s %s", name, last, when)
end

local MAX_LINES = 20

function Auction.Command()
	if visit then
		local count = Again()
		ns.Print(State(visit.priority))
		if count == 0 then
			ns.Print("Nothing to look up: your goals, plan, AI and bags don't need any items right now.")
			return
		end
		local safe, why = Safe()
		ns.Print(string.format("Looking up the auction prices of %d item%s%s.", count, count > 1 and "s" or "",
			safe and "" or (" (" .. why .. ")")))
		return
	end
	local realm, priority, lines = Realm(), Priority(), {}
	ns.Print(State(priority))
	for _, p in ipairs(priority) do
		local known = p.ai and Known(realm, p.item)
		if known and #lines < MAX_LINES then lines[#lines + 1] = Line(p.item, known) end
	end
	if #lines == 0 then
		ns.Print("No auction prices for what your goals, plan and AI need yet: they're looked up when you open the auction house.")
		return
	end
	ns.Print("Auction prices last seen:")
	for _, line in ipairs(lines) do ns.Print(line) end
end

-- Which auction house, as a number in places (the reply names each once).
local function Place(places, where)
	local key = table.concat({ tostring(where.zone), tostring(where.subzone), tostring(where.faction) }, "|")
	if not places[key] then
		places[#places + 1] = { zone = where.zone, subzone = where.subzone, faction = where.faction }
		places[key] = #places
	end
	return places[key]
end

-- A price for get_auction_prices.
local function Answer(realm, itemID, places, why)
	local e = Known(realm, itemID)
	if not e then return nil end
	local tiers = {}
	for i, t in ipairs(e.tiers or {}) do tiers[i] = { t.price, t.count } end
	return {
		item = itemID, name = e.name or ItemName(itemID), price = e.price, listed = e.listed, n = e.n,
		tiers = tiers[1] and tiers or nil, bidOnly = e.bidOnly, commodity = e.commodity, at = e.at, src = e.src,
		place = Place(places, e), last = e.last, why = why,
	}
end

-- The items asked for (the lookup), or else the priority list; never everything, as replies stream slowly.
local function Section()
	local realm, priority, places, list, why = Realm(), Priority(), {}, {}, {}
	for _, p in ipairs(priority) do why[p.item] = p.why end
	local asked = lookup and lookup.items[1] and lookup.items
	for _, itemID in ipairs(asked or {}) do list[#list + 1] = Answer(realm, itemID, places, why[itemID]) end
	for _, p in ipairs(asked and {} or priority) do list[#list + 1] = Answer(realm, p.item, places, p.why) end
	local ai = 0
	for _, p in ipairs(priority) do
		if p.ai then ai = ai + 1 end
	end
	local latest, scan = CurrentScan(realm), nil
	if latest then
		scan = { at = latest.at, items = latest.items, listings = latest.listings, place = Place(places, latest) }
	end
	local placeList = {}
	for i, p in ipairs(places) do placeList[i] = p end
	return {
		realm = GetRealmName(), lookup = lookup and lookup.id, prices = list, places = placeList, scan = scan,
		nextScan = Asked() + SCAN_INTERVAL, reading = reader ~= nil, priority = #priority, tracked = ai,
		open = visit ~= nil, left = visit and Left() or nil, waiting = visit and visit.waiting or nil, problem = problem,
	}
end

-- { id, items = { { item, at } }, cleared, lookup = { id, at, items } }: what the AI watches (at: when it last
-- asked), when it last dropped what it watched before, and what get_auction_prices asks about now. Every
-- payload carries it, so taking it in again changes nothing.
ns.RegisterReceiver("auctionWatch", function(payload)
	if type(payload) ~= "table" then return end
	local tracked = Realm().tracked
	local cleared = tonumber(payload.cleared)
	if cleared then
		for itemID, t in pairs(tracked) do
			if (t.why == "watch" or t.why == "asked") and (t.at or 0) <= cleared then tracked[itemID] = nil end
		end
	end
	for i, w in ipairs(type(payload.items) == "table" and payload.items or {}) do
		if type(w) == "table" then Track(tracked, w.item, "watch", w.at, i) end
	end
	local l = payload.lookup
	if type(l) == "table" and tonumber(l.id) and type(l.items) == "table" then
		lookup = { id = tonumber(l.id), items = {} }
		for i, itemID in ipairs(l.items) do
			if tonumber(itemID) and #lookup.items < MAX_LOOKUP then
				lookup.items[#lookup.items + 1] = tonumber(itemID)
				Track(tracked, itemID, "asked", l.at, i)
			end
		end
	end
	if visit then
		visit.priority = Priority()
	else
		Tracked() -- drops the oldest past MAX_PRIORITY
	end
end)
ns.RegisterSection(ns.Kinds.KNOWLEDGE, "auction", Section)
ns.RegisterCommand("prices", "/mcp prices - the auction prices your AI has: the full scan, the priority list, and "
	.. "at the auction house, look the list up again", Auction.Command)
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
	"AUCTION_HOUSE_THROTTLED_MESSAGE_DROPPED", "AUCTION_HOUSE_THROTTLED_MESSAGE_SENT", "REPLICATE_ITEM_LIST_UPDATE",
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
	elseif event == "AUCTION_HOUSE_THROTTLED_MESSAGE_SENT" then
		table.insert(messages, GetTime())
	elseif event == "REPLICATE_ITEM_LIST_UPDATE" then
		local ok, err = pcall(StartRead)
		if not ok then Report(err) end
	elseif event == "AUCTION_HOUSE_THROTTLED_MESSAGE_DROPPED" and current and visit then
		-- The game dropped a search (too many at once): try that item once more, a little later.
		if not visit.retried then
			visit.retried = true
			visit.searched[current.item] = nil
		end
		visit.notBefore = GetTime() + TIMEOUT
		current = nil
	end
end)
