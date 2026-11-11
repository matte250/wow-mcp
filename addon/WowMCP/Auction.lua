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
-- Weapons and armor can have random suffixes ("of the Bear"), each with its own stats but the same item ID, so
-- the full scan also keeps their cheapest price per suffix, from each auction's link.
--
-- The upgrade finder (find_upgrades) searches all of it, plus quest rewards and vendors, for gear that beats what
-- the player wears.
--
-- The player's own auctions (get_my_auctions) are kept too: what they have listed, as the auction house last listed
-- it, and what sold or expired since, from the game's notices and the mail.
--
-- While the window is open, a status panel under it (at the end) shows what's going on: the full scan, the searches
-- and how many are left, and why they wait.
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
local MAX_VARIANTS = 12   -- random suffixes kept per item from a full scan, the cheapest
local WEAPON, ARMOR, QUEST_CLASS = 2, 4, 12 -- Enum.ItemClass
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
local scanAdded = 0 -- items new to the scan table, for the upgrade finder walking it
local lookup  -- what get_auction_prices asks for: { id, items }
local problem
local Notify, PanelCommand -- the status panel (at the end): Notify() whenever what it shows may have changed
local Mine = {} -- the player's own auctions (after the lookups): Due, Ask

-- WowMCPDB.auction[realm] = {
--   items = { [item ID] = { price (cheapest, per item, copper), listed, n (auctions), tiers = { { price, count } },
--     commodity, bidOnly, name, at (unix time), src ("search" or "scan"), zone, subzone, faction } }: an item's last
--     search, or the last full scan of a priority item, with its cheapest price points;
--   scan = { [item ID] = "price,listed,auctions,scan" }: every item a full scan has seen, as the last scan that saw
--     it did (scan: its number in scans; no price if there were only bids). Kept compact: that's thousands;
--   scans = { { at, items, listings, zone, subzone, faction } }, current = the number of the newest complete one;
--   tracked = { [item ID] = { at, why, n } }: what the AI tracks (why: goal, plan, watch, asked);
--   variants = { [item ID] = "scan;suffix,price,listed;..." }: gear with random suffixes as the last scan that saw
--     it did, the cheapest buyout per suffix (suffix as in the item link; empty price: only bids), ";?" if some
--     auctions' links weren't there yet. Gear seen without suffixes has none.
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
	realm.variants = realm.variants or {}
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
	if visit then visit.problem = problem end -- the panel says so
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
	Notify()
end

-- Whether an item is a weapon or armor, from the client's static data; asked once per item per read.
local function Gear(r, itemID)
	local gear = r.gear[itemID]
	if gear == nil then
		local ok, _, _, _, _, _, class = pcall(C_Item.GetItemInfoInstant, itemID)
		gear = ok and (class == WEAPON or class == ARMOR) or false
		r.gear[itemID] = gear
	end
	return gear
end

-- An auction of gear: the random suffix in its link, if any, with the cheapest buyout per item and how many are
-- listed per suffix. The link only comes once the item's data has loaded, like the name.
local function Variant(r, index, itemID, count, each)
	local ok, link = pcall(C_AuctionHouse.GetReplicateItemLink, index)
	local suffix, unique
	if ok and type(link) == "string" then
		suffix, unique = link:match("item:%d+:[^:]*:[^:]*:[^:]*:[^:]*:[^:]*:(%-?%d*):?(%-?%d*)")
	end
	if not suffix then
		r.nolink[itemID] = true
		return
	end
	local id = tonumber(suffix)
	if not id or id == 0 then return end
	-- A negative suffix scales with the item, which the link's unique ID carries.
	local key = id > 0 and tostring(id) or (id .. ":" .. unique)
	local variants = r.variants[itemID] or {}
	r.variants[itemID] = variants
	local v = variants[key] or { listed = 0 }
	variants[key] = v
	v.listed = v.listed + count
	if each and (not v.price or each < v.price) then v.price = each end
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
	local each = type(buyout) == "number" and buyout > 0 and buyout / count or nil
	if each then
		if not r.price[itemID] or each < r.price[itemID] then r.price[itemID] = each end
		if points then points[#points + 1] = { price = math.floor(each + 0.5), count = count } end
	elseif points then
		r.bids[itemID] = (r.bids[itemID] or 0) + count
	end
	if Gear(r, itemID) then Variant(r, i, itemID, count, each) end
end

-- A gear item's suffixes for realm.variants, cheapest first; nil if its auctions had none.
local function EncodeVariants(r, itemID)
	local list = {}
	for key, v in pairs(r.variants[itemID] or {}) do list[#list + 1] = { key = key, price = v.price, listed = v.listed } end
	if #list == 0 and not r.nolink[itemID] then return nil end
	table.sort(list, function(a, b)
		local pa, pb = a.price or math.huge, b.price or math.huge
		if pa ~= pb then return pa < pb end
		return a.key < b.key
	end)
	local parts = { tostring(r.k) }
	for i = 1, math.min(#list, MAX_VARIANTS) do
		local v = list[i]
		parts[#parts + 1] = string.format("%s,%s,%d", v.key, v.price and tostring(math.floor(v.price + 0.5)) or "", v.listed)
	end
	if r.nolink[itemID] then parts[#parts + 1] = "?" end
	return table.concat(parts, ";")
end

local function StoreOne(r)
	local itemID = r.ids[r.index]
	r.index = r.index + 1
	local price = r.price[itemID]
	if not r.realm.scan[itemID] then scanAdded = scanAdded + 1 end
	r.realm.scan[itemID] = string.format("%s,%d,%d,%d", price and tostring(math.floor(price + 0.5)) or "",
		r.listed[itemID], r.n[itemID], r.k)
	if r.gear[itemID] then r.realm.variants[itemID] = EncodeVariants(r, itemID) end
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
				r.queued = #r.queue -- for the panel's progress
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
	if reader == r then Notify() end -- cheap: the panel redraws from a timer, a few times a second
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
		gear = {}, variants = {}, nolink = {},
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
	Notify()
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

-- mine: the player's own auctions, asked for once a visit (due), and again after they post or cancel one (at: when
-- asked, until the answer).
local function Start()
	visit = { priority = Priority(), searched = {}, notBefore = GetTime() + START_DELAY, count = 0, mine = { due = true } }
end

-- /mcp prices at the auction house: the whole priority list again, recent prices too.
local function Again()
	visit.priority, visit.searched, visit.force = Priority(), {}, true
	visit.notBefore, visit.told, visit.finished, visit.last = GetTime(), nil, nil, nil
	Notify()
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
	visit.last = itemID -- the panel names it until the next one goes
	C_AuctionHouse.SendSearchQuery(key, sorts, false)
	Notify()
end

local function Replicate()
	SetAsked(time())
	visit.scan = { state = "asked", at = GetTime() }
	table.insert(sent, GetTime())
	C_AuctionHouse.ReplicateItems()
	Notify()
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
	Notify() -- also keeps the panel's countdowns and pauses current
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
	if Mine.Due() then return Mine.Ask() end -- one message: while the full scan comes, or before the searches
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
	Notify()
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

-- 5218 as "5,218", as the client writes large numbers.
local function Thousands(n)
	n = math.floor(n)
	if BreakUpLargeNumbers then
		local ok, text = pcall(BreakUpLargeNumbers, n)
		if ok and type(text) == "string" then return text end
	end
	local text, k = tostring(n), nil
	repeat text, k = text:gsub("^(%d+)(%d%d%d)", "%1,%2") until k == 0
	return text
end

-- The full scans: when the last one was, and when the next one can be.
local function ScanState()
	local latest = CurrentScan(Realm())
	local scan = latest and string.format("last full scan %s (%s items)", Ago(latest.at), Thousands(latest.items))
		or "no full scan yet"
	local wait = Asked() + SCAN_INTERVAL - time()
	if reader then return scan .. ", reading a new one" end
	if wait > 0 then return scan .. string.format(", next one possible in %d min", math.ceil(wait / 60)) end
	return scan .. ", next one at the auction house"
end

-- The full scan, the priority list and the searches still to come, in a line.
local function State(priority)
	local ai = 0
	for _, p in ipairs(priority) do
		if p.ai then ai = ai + 1 end
	end
	local scan = ScanState()
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

function Auction.Command(rest)
	local word, arg = strtrim(tostring(rest or "")):lower():match("^(%S*)%s*(.-)$")
	if word == "panel" then return PanelCommand(arg) end
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

-- A price for get_auction_prices. scanned: some full scan saw it listed (last only has a price that had a buyout).
local function Answer(realm, itemID, places, why)
	local e = Known(realm, itemID)
	if not e then return nil end
	local tiers = {}
	for i, t in ipairs(e.tiers or {}) do tiers[i] = { t.price, t.count } end
	return {
		item = itemID, name = e.name or ItemName(itemID), price = e.price, listed = e.listed, n = e.n,
		tiers = tiers[1] and tiers or nil, bidOnly = e.bidOnly, commodity = e.commodity, at = e.at, src = e.src,
		place = Place(places, e), last = e.last, why = why, scanned = realm.scan[itemID] and true or nil,
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
		-- first and scans: how far back the full scans go, for items none of them saw listed.
		scan = { at = latest.at, items = latest.items, listings = latest.listings, place = Place(places, latest),
			first = realm.scans[1] and realm.scans[1].at, scans = realm.current }
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
		Notify()
	else
		Tracked() -- drops the oldest past MAX_PRIORITY
	end
end)
ns.RegisterSection(ns.Kinds.KNOWLEDGE, "auction", Section)
ns.RegisterCommand("prices", "/mcp prices [panel on, off] - the auction prices your AI has: the full scan, the "
	.. "priority list, and at the auction house, look the list up again; panel: the status panel there", Auction.Command)
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
	Notify()
end

local function Close()
	visit, current = nil, nil
	Notify()
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

---------------------------------------------------------------------------
-- The player's own auctions (get_my_auctions)
---------------------------------------------------------------------------

-- What the player has listed, as the auction house last listed it, and what became of it since: per character in
-- WowMCPDB.myAuctions, for the AI (section "myAuctions" of request kind KNOWLEDGE, with the game's prices of those
-- items and the auction house mail as last seen).
-- The auction house only lists them when asked (C_AuctionHouse.QueryOwnedAuctions, answered by OWNED_AUCTIONS_UPDATED).
-- Blizzard's Auctions tab asks whenever its list shows, and every answer is read, whoever asked. The addon asks too,
-- once a visit and again after the player posts or cancels one (AUCTION_HOUSE_AUCTION_CREATED, AUCTION_CANCELED), the
-- way it searches: only on the Browse view, when the game's throttle is ready, counted in the same pace, with the sorts
-- the window uses for that list. On the Auctions tab an answer would clear what the player selected there and take them
-- back to all their auctions (AuctionHouseAuctionsFrameMixin:OnEvent); that list only listens while it shows, and the
-- rest of the window only resets its price columns' widths.
-- Sales and expiries are noticed anywhere: the game's notice (AUCTION_HOUSE_SHOW_FORMATTED_NOTIFICATION, which names
-- the auction) and the same as a system chat line, if the server sends one too (ERR_AUCTION_SOLD_S); one sale that
-- comes both ways counts once. Auction house mail the mailbox showed (Inventory.lua's cache) settles what's still open.
-- In its own block: Auction.lua is near Lua's limit of 200 locals.
do
	local RECENT = 7 * 86400 -- ended auctions and notices are kept this long
	local MAX_KEPT = 300
	local MAX_NOTICES = 30
	local MAX_SENT = 150     -- auctions in a reply, the listed ones first
	local PAIR_WITHIN = 10   -- seconds: the notice and the chat line for one sale come this close
	local AH_MAIL_DAYS = 30  -- auction house mail lasts this long, so its expiry says when it came (assumed)
	local SLACK = 3600       -- seconds: how much before the list last had an auction its mail may seem to have come
	local SOLD = Enum.AuctionStatus and Enum.AuctionStatus.Sold or 1
	local NOTICE = Enum.AuctionHouseNotification
		or { AuctionWon = 2, AuctionOutbid = 3, AuctionSold = 4, AuctionExpired = 5 }
	local NOTICES = { [NOTICE.AuctionSold] = "sold", [NOTICE.AuctionExpired] = "expired",
		[NOTICE.AuctionOutbid] = "outbid", [NOTICE.AuctionWon] = "won" } -- outbid, won: the player's own bids
	-- Enum.AuctionHouseTimeLeftBand in seconds, should C_AuctionHouse.GetTimeLeftBandInfo not say: retail's.
	local BANDS = { [0] = { 0, 1800 }, [1] = { 1800, 7200 }, [2] = { 7200, 43200 }, [3] = { 43200, 172800 } }
	local OWNED_SORTS = { -- the window's defaults for that list
		{ sortOrder = SORT.Name or 1, reverseSort = false }, { sortOrder = SORT.Price or 0, reverseSort = false },
	}
	local MAIL_STATUS = { sold = "sold", pending = "sold", expired = "expired", cancelled = "cancelled" }
	local OPEN = { active = true, gone = true } -- what a notice or a mail can settle

	-- The system chat lines for the same notices, as patterns capturing the item.
	local LINES = {}
	for kind, template in pairs({
		sold = ERR_AUCTION_SOLD_S or "A buyer has been found for your auction of %s.",
		expired = ERR_AUCTION_EXPIRED_S or "Your auction of %s has expired.",
		outbid = ERR_AUCTION_OUTBID_S or "You have been outbid on %s.",
		won = ERR_AUCTION_WON_S or "You won an auction for %s",
	}) do
		LINES[kind] = "^" .. (template:gsub("[%^%$%(%)%.%[%]%*%+%-%?]", "%%%0"):gsub("%%s", "(.+)")) .. "$"
	end

	local trouble -- what last went wrong, for the reply

	-- WowMCPDB.myAuctions["Name-Realm"] = {
	--   seen = when the auction house last listed them all (unix time); nil: not since the addon was installed;
	--   auctions = { { id (auctionID), item, name, count, price (buyout per item, copper; nil: only a bid), bid (the
	--     highest bid, for all of them), status, first, seen, expires, soonest, band, left, due, at, how, mail, mailAt,
	--     noticed } }:
	--     status "active", "sold-pending" (sold, its money on its way), "sold", "expired", "cancelled", or "gone" (no
	--     longer listed, and nothing said why); first, seen: when the list first and last had it; expires: when it ends,
	--     from the time left the list gave, which left says: "seconds" (exact) or "band" (only band, an
	--     Enum.AuctionHouseTimeLeftBand: soonest is its start and expires its end, narrowed by each look); due: when a
	--     sold one's money comes, if the list said; at, how: when it ended and what said so ("list", "notice", "chat",
	--     "mail" or "cancel"); mail, mailAt: the mail that settled it, what it says and when it expires (a mail settles
	--     one auction); noticed: a notice was about it;
	--   notices = { { kind ("sold", "expired", "outbid" or "won"), name, at, src ("notice" or "chat"), id (the auction, if
	--     the notice said), auction (the one it settled), was (that one's status before), paired (it came both ways) } },
	--     newest last.
	-- }
	local function Key()
		return tostring(UnitName("player")) .. "-" .. tostring(GetRealmName())
	end

	local function Saved()
		WowMCPDB.myAuctions = WowMCPDB.myAuctions or {}
		local saved = WowMCPDB.myAuctions[Key()] or { auctions = {}, notices = {} }
		WowMCPDB.myAuctions[Key()] = saved
		return saved
	end

	local function Find(saved, id)
		for _, a in ipairs(saved.auctions) do
			if a.id == id then return a end
		end
	end

	local function Money(value)
		return type(value) == "number" and value > 0 and value or nil
	end

	-- The name in an item link; nil without one.
	local function LinkName(link)
		local name = type(link) == "string" and link:match("|h%[(.-)%]|h")
		return name ~= "" and name or nil
	end

	-- Whether an auction is of the item a notice or a mail names, with its random suffix ("of the Bear") or without.
	local function Named(a, name)
		return name ~= nil and (a.name == name or ItemName(a.item) == name)
	end

	-- When a listed auction ends, from the time left the list gave: exact in seconds, else its band.
	local function Estimate(a, info, now)
		if type(info.timeLeftSeconds) == "number" then
			a.expires, a.soonest, a.left, a.band = now + info.timeLeftSeconds, nil, "seconds", nil
			return
		end
		local band = info.timeLeft
		if type(band) ~= "number" then return end
		local ok, low, high = pcall(C_AuctionHouse.GetTimeLeftBandInfo, band)
		if not (ok and type(low) == "number" and type(high) == "number") then
			local b = BANDS[band] or BANDS[3]
			low, high = b[1], b[2]
		end
		local soonest, latest = now + low, now + high
		if a.left == "band" and a.soonest and a.expires then
			-- It can't end before an earlier look's soonest, nor after its latest.
			local s, l = math.max(soonest, a.soonest), math.min(latest, a.expires)
			if s <= l then soonest, latest = s, l end
		end
		a.expires, a.soonest, a.left, a.band = latest, soonest, "band", band
	end

	-- The list as the auction house answered, whoever asked. What's no longer in it ended: a sold one's money has gone
	-- out by mail; for the rest the list doesn't say how.
	local function Read()
		local saved, now = Saved(), time()
		local listed, new = {}, {}
		for i = 1, C_AuctionHouse.GetNumOwnedAuctions() do
			local info = C_AuctionHouse.GetOwnedAuctionInfo(i)
			local itemID = type(info) == "table" and type(info.itemKey) == "table" and info.itemKey.itemID
			if type(itemID) == "number" and type(info.auctionID) == "number" then
				local a = Find(saved, info.auctionID)
				if not a then
					a = { id = info.auctionID, item = itemID, first = now }
					saved.auctions[#saved.auctions + 1] = a
					new[#new + 1] = a
				end
				listed[a] = true
				a.name = LinkName(info.itemLink) or ItemName(itemID) or a.name
				a.count, a.price, a.bid, a.seen = info.quantity, Money(info.buyoutAmount), Money(info.bidAmount), now
				if info.status == SOLD then
					-- A notice may have said when it sold.
					if a.status ~= "sold" and a.status ~= "sold-pending" then a.at, a.how = now, "list" end
					a.status = "sold-pending"
					if type(info.timeLeftSeconds) == "number" then a.due = now + info.timeLeftSeconds end
				else
					a.status, a.at, a.how = "active", nil, nil
					Estimate(a, info, now)
				end
			end
		end
		if C_AuctionHouse.HasFullOwnedAuctionResults and not C_AuctionHouse.HasFullOwnedAuctionResults() then return end
		saved.seen = now
		for i = #saved.auctions, 1, -1 do
			local a = saved.auctions[i]
			if not listed[a] then
				local twin
				if a.status == "active" or a.status == "sold" then
					-- A whole auction that sold may be listed under another ID once sold: the same auction.
					for k, b in ipairs(new) do
						if b.status == "sold-pending" and b.item == a.item and b.count == a.count and b.price == a.price then
							twin = table.remove(new, k)
							break
						end
					end
				end
				if twin then
					twin.first, twin.noticed = a.first, a.noticed or twin.noticed
					if a.status == "sold" then twin.at, twin.how = a.at, a.how end
					table.remove(saved.auctions, i)
				elseif a.status == "sold-pending" then
					a.status = "sold" -- its money has gone out
				elseif a.status == "active" then
					a.status, a.at, a.how = "gone", now, "list"
				end
			end
		end
	end

	-- The auction a notice is about: the one it names, else a listed one of the item it names, for a sale the cheapest
	-- (buyers take those first), for an expiry the one ending first; else a sale the list already had as sold.
	local function About(saved, kind, name, id)
		local named = id and Find(saved, id)
		if named then return named end
		local bare = name and name:match("^(.-)%s*%(%d+%)$") -- "Linen Cloth (20)"
		local best
		for _, a in ipairs(saved.auctions) do
			if a.status == "active" and (Named(a, name) or Named(a, bare)) then
				if not best then
					best = a
				elseif kind == "sold" and (a.price or math.huge) < (best.price or math.huge) then
					best = a
				elseif kind == "expired" and (a.expires or math.huge) < (best.expires or math.huge) then
					best = a
				end
			end
		end
		if best or kind ~= "sold" then return best end
		for _, a in ipairs(saved.auctions) do
			if a.status == "sold-pending" and not a.noticed and Named(a, name) then return a end
		end
	end

	local function Settle(n, a)
		n.auction, a.noticed = a.id, true
		if OPEN[a.status] then
			n.was = a.status
			a.status, a.at, a.how = n.kind, n.at, n.src
		end
	end

	local function Unsettle(saved, n)
		local a = n.auction and Find(saved, n.auction)
		if a then
			a.noticed = nil
			if n.was and a.status == n.kind and a.how == n.src then a.status, a.at, a.how = n.was, nil, nil end
		end
		n.auction, n.was = nil, nil
	end

	-- A notice (src "notice") or chat line ("chat") that an auction sold or expired, or about the player's bids. The
	-- same one the other way within PAIR_WITHIN seconds is the same news.
	local function Notice(kind, name, src, id)
		local saved, now = Saved(), time()
		name = LinkName(name) or name
		local notices = saved.notices
		for i = #notices, 1, -1 do
			local n = notices[i]
			if now - n.at > PAIR_WITHIN then break end
			if n.kind == kind and n.name == name and n.src ~= src and not n.paired then
				n.paired = true
				-- Only the game's notice names the auction: a chat line that came first may have picked another.
				local named = id and n.auction ~= id and Find(saved, id)
				if named then
					Unsettle(saved, n)
					n.id, n.src = id, src
					Settle(n, named)
				end
				return
			end
		end
		local n = { kind = kind, name = name, at = now, src = src, id = id }
		notices[#notices + 1] = n
		while #notices > MAX_NOTICES do table.remove(notices, 1) end
		if kind == "sold" or kind == "expired" then
			local a = About(saved, kind, name, id)
			if a then Settle(n, a) end
		end
	end

	local function ChatLine(text)
		if type(text) ~= "string" or (issecretvalue and issecretvalue(text)) then return end
		for kind, pattern in pairs(LINES) do
			local name = text:match(pattern)
			if name then return Notice(kind, name, "chat") end
		end
	end

	local function Ended(id, status, how)
		local a = Find(Saved(), id)
		if a and OPEN[a.status] then a.status, a.at, a.how = status, time(), how end
	end

	-- Auction house mail the mailbox showed (Inventory.lua's cache) settles what's still open: a sale's invoice names the
	-- item and how many, an expired or cancelled auction's mail has it attached. Only mail that came after the list last
	-- had the auction counts (mail says when it expires, AH_MAIL_DAYS after it came), and one mail settles one auction:
	-- a sale at the auction's price first. Mail has no ID, so it's known again by what it says and when it expires
	-- (each read of the inbox may put that a second off); mail alike that came together settles as many auctions.
	local function Reconcile(saved)
		local inbox = WowMCPDB.mail and WowMCPDB.mail[Key()]
		if not (inbox and type(inbox.mails) == "table") then return end
		local claimed = {}
		for _, m in ipairs(inbox.mails) do
			local status, expires, sale = MAIL_STATUS[m.ah], tonumber(m.expires), m.sale
			local attached = type(m.items) == "table" and m.items[1]
			local name = sale and sale.item or (attached and attached.name)
			local count = sale and (sale.count or 1) or (attached and (attached.count or 1))
			local says = table.concat({ tostring(m.ah), tostring(name or (attached and attached.id)), tostring(count),
				tostring(sale and sale.price) }, "|")
			local settled
			for _, a in ipairs(saved.auctions) do
				if not settled and a.mail == says and not claimed[a] and math.abs((a.mailAt or 0) - (expires or 0)) <= 2 then
					settled = a
				end
			end
			if settled then
				claimed[settled] = true
			elseif status and count and expires then
				local came = expires - AH_MAIL_DAYS * 86400
				local best, exact
				for _, a in ipairs(saved.auctions) do
					local open = OPEN[a.status] or (status == "sold" and a.status == "sold-pending")
					local same = (attached and not sale) and a.item == attached.id or Named(a, name)
					-- A sold one's money mail comes after the sale, which may be before the list last had it.
					local since = a.status == "sold-pending" and (a.at or a.seen) or a.seen or 0
					if open and same and a.count == count and not a.mail and came >= since - SLACK then
						local price = sale and (sale.price == (a.price or 0) * count or sale.price == a.bid)
						if not best or (price and not exact) then best, exact = a, price end
					end
				end
				if best then
					best.status, best.at, best.how, best.mail, best.mailAt = status, math.min(came, time()), "mail", says, expires
					claimed[best] = true
				end
			end
		end
	end

	-- Ended more than RECENT ago: gone. A listed one goes RECENT after it was due to end.
	local function Prune(saved, now)
		local kept, notices = {}, {}
		for _, a in ipairs(saved.auctions) do
			local last = a.status == "active" and (a.expires or a.seen) or (a.at or a.seen)
			if now - (last or now) < RECENT then kept[#kept + 1] = a end
		end
		table.sort(kept, function(x, y) return (x.at or x.seen or 0) > (y.at or y.seen or 0) end)
		for i = #kept, MAX_KEPT + 1, -1 do kept[i] = nil end
		saved.auctions = kept
		for _, n in ipairs(saved.notices) do
			if now - n.at < RECENT then notices[#notices + 1] = n end
		end
		saved.notices = notices
	end

	-- The auction house's mail as last seen: { at, sales = { { kind (Inventory.lua's ah), name, item (attached), count,
	-- money (attached), price, fee, deposit, eta, expires } } }; nil if the mailbox wasn't seen.
	local function MailSales()
		local inbox = WowMCPDB.mail and WowMCPDB.mail[Key()]
		if not inbox then return nil end
		local sales = {}
		for _, m in ipairs(type(inbox.mails) == "table" and inbox.mails or {}) do
			if MAIL_STATUS[m.ah] then
				local attached, sale = type(m.items) == "table" and m.items[1] or {}, m.sale or {}
				sales[#sales + 1] = { kind = m.ah, name = sale.item or attached.name, item = attached.id,
					count = sale.count or attached.count or 1, money = m.money, price = sale.price, fee = sale.fee,
					deposit = sale.deposit, eta = sale.eta, expires = m.expires }
			end
		end
		return { at = inbox.time, sales = sales }
	end

	-- Listed first, those ending soonest first; then what ended, the latest first.
	local function Order(x, y)
		local xl, yl = x.status == "active", y.status == "active"
		if xl ~= yl then return xl end
		if xl then return (x.expires or math.huge) < (y.expires or math.huge) end
		return (x.at or x.seen or 0) > (y.at or y.seen or 0)
	end

	-- { seen, open, reading, realm, faction, auctions, more, notices (those about no auction kept), prices (the game's,
	-- of the listed items, as get_auction_prices has them), places, mail, problem }.
	local function Section()
		local saved, now = Saved(), time()
		Reconcile(saved)
		Prune(saved, now)
		local sorted, list, prices, priced, places, realm = {}, {}, {}, {}, {}, Realm()
		for _, a in ipairs(saved.auctions) do sorted[#sorted + 1] = a end
		table.sort(sorted, Order)
		for i = 1, math.min(#sorted, MAX_SENT) do
			local a = sorted[i]
			list[i] = { id = a.id, item = a.item, name = a.name or ItemName(a.item), count = a.count, price = a.price,
				bid = a.bid, status = a.status, first = a.first, seen = a.seen, expires = a.expires, soonest = a.soonest,
				left = a.left, due = a.due, at = a.at, how = a.how }
			if a.status == "active" and not priced[a.item] then
				priced[a.item] = true
				prices[#prices + 1] = Answer(realm, a.item, places)
			end
		end
		local notices, placeList = {}, {}
		for _, n in ipairs(saved.notices) do
			if not n.auction then notices[#notices + 1] = { kind = n.kind, name = n.name, at = n.at, src = n.src } end
		end
		for i, p in ipairs(places) do placeList[i] = p end
		return {
			seen = saved.seen, open = visit ~= nil, reading = visit and visit.mine.at and true or nil, realm = GetRealmName(),
			faction = UnitFactionGroup("player"), auctions = list, more = #sorted > #list and #sorted - #list or nil,
			notices = notices, prices = prices, places = placeList, mail = MailSales(), problem = trouble,
		}
	end

	-- Asked for when Step allows a message (the Browse view, the throttle, the pace); an answer that doesn't come is
	-- given up after TIMEOUT.
	function Mine.Due()
		local m = visit.mine
		if m.at and GetTime() - m.at > TIMEOUT then m.at = nil end
		return m.due and not m.at and C_AuctionHouse.QueryOwnedAuctions ~= nil
	end

	function Mine.Ask()
		local context, sorts = AuctionHouseSearchContext and AuctionHouseSearchContext.AllAuctions, nil
		if context and AuctionHouseFrame.GetSortsForContext then
			local ok, list = pcall(AuctionHouseFrame.GetSortsForContext, AuctionHouseFrame, context)
			sorts = ok and type(list) == "table" and list[1] and list or nil
		end
		visit.mine.due, visit.mine.at = false, GetTime()
		table.insert(sent, GetTime())
		C_AuctionHouse.QueryOwnedAuctions(sorts or OWNED_SORTS)
		Notify()
	end

	local function Try(fn, ...)
		local ok, err = pcall(fn, ...)
		if not ok then trouble = tostring(err) end
	end

	local reconciling -- MAIL_INBOX_UPDATE comes in bursts
	local frame = CreateFrame("Frame")
	for _, event in ipairs({ "OWNED_AUCTIONS_UPDATED", "AUCTION_HOUSE_AUCTION_CREATED", "AUCTION_CANCELED",
		"AUCTION_HOUSE_AUCTIONS_EXPIRED", "AUCTION_HOUSE_SHOW_NOTIFICATION", "AUCTION_HOUSE_SHOW_FORMATTED_NOTIFICATION",
		"CHAT_MSG_SYSTEM", "AUCTION_HOUSE_THROTTLED_MESSAGE_DROPPED", "MAIL_INBOX_UPDATE" }) do
		pcall(frame.RegisterEvent, frame, event)
	end
	frame:SetScript("OnEvent", function(_, event, arg1, arg2, arg3)
		if type(WowMCPDB) ~= "table" then return end
		if event == "OWNED_AUCTIONS_UPDATED" then
			Try(Read)
			if visit then visit.mine.due, visit.mine.at = false, nil end
			Notify()
		elseif event == "AUCTION_HOUSE_AUCTION_CREATED" then
			if visit then visit.mine.due = true end
		elseif event == "AUCTION_CANCELED" then
			if visit then visit.mine.due = true end
			Try(Ended, arg1, "cancelled", "cancel")
		elseif event == "AUCTION_HOUSE_AUCTIONS_EXPIRED" then
			Try(Ended, arg1, "expired", "notice")
		elseif event == "AUCTION_HOUSE_SHOW_FORMATTED_NOTIFICATION" or event == "AUCTION_HOUSE_SHOW_NOTIFICATION" then
			-- notification, text (the item), auctionID: the formatted one only.
			local id = type(arg3) == "number" and arg3 or nil
			if NOTICES[arg1] and (type(arg2) == "string" or id) then
				Try(Notice, NOTICES[arg1], type(arg2) == "string" and arg2 or nil, "notice", id)
			end
		elseif event == "CHAT_MSG_SYSTEM" then
			Try(ChatLine, arg1)
		elseif event == "AUCTION_HOUSE_THROTTLED_MESSAGE_DROPPED" and visit and visit.mine.at then
			-- The game dropped it (too many at once): asked once more, a little later.
			visit.mine.due, visit.mine.at, visit.mine.retried = not visit.mine.retried, nil, true
			visit.notBefore = GetTime() + TIMEOUT
		elseif event == "MAIL_INBOX_UPDATE" and not reconciling then
			reconciling = true
			C_Timer.After(0, function() -- once Inventory.lua has saved the inbox
				reconciling = nil
				Try(function() Reconcile(Saved()) end)
			end)
		end
	end)

	ns.RegisterSection(ns.Kinds.KNOWLEDGE, "myAuctions", Section)
end

---------------------------------------------------------------------------
-- Gear upgrades (find_upgrades)
---------------------------------------------------------------------------

-- What could beat the player's gear: auction house items (the full scans, and searches), rewards of the quests in
-- their log and what the vendors they've seen sell (Knowledge.lua), kept to what this character can use and ranked
-- with the stat weights the server picked for them. Requests carry no parameters, so the server publishes the query
-- (payload key "upgradeQuery"), then asks (section "upgrades" of request kind ITEM_DETAILS) until the answer is
-- there. Only the client has the items' data, and the full scan has thousands of item IDs, so the search runs here,
-- a little per frame, in stages (a coroutine, so they read in order):
--   1. every candidate's equip slot, class and subclass from the client's static data (GetItemInfoInstant, nothing
--      to load): only gear for the slots asked for, of armor and weapon types the character has the skill for;
--   2. those loaded, at most LOAD_BATCH at a time; then the required level, stats, armor and a score, against
--      what they'd replace;
--   3. per slot, best first, whether the character can use it: no red tooltip line but the level (they may be a
--      level or two short), and the game's own check where it agrees with the gear they wear.
-- Each random suffix the latest full scan saw is a candidate of its own, with that suffix's stats and price. An
-- item whose suffixes weren't seen goes as it is, marked random, so its price is never shown with a suffix's stats.

local FIND_BUDGET_MS = 1.5 -- frame time per frame for the search, as much as Stream.lua sends with
local LOAD_BATCH = 50      -- items loading at once
local LOAD_TIMEOUT = 10    -- seconds an item may take to load; then it's left out
local KEEP = 120           -- seconds an answer is there for the server; then it's gone
local MAX_PER_SLOT = 5
local MAX_ASKED = 10
local MAX_OFFERS = 2       -- where to get each item: free first, then the cheapest
local MIN_GAIN = 0.05      -- a score this much better counts as an upgrade
local DPS = "ITEM_MOD_DAMAGE_PER_SECOND_SHORT"
local TWO_HAND = "INVTYPE_2HWEAPON"
local SOURCES = { "auction", "quests", "vendors" }

-- Slot groups, in the order they're answered, and the inventory slots each compares with.
local GROUPS = { "head", "neck", "shoulder", "back", "chest", "wrist", "hands", "waist", "legs", "feet", "finger",
	"trinket", "main hand", "off hand", "ranged" }
local GROUP_SLOTS = {
	head = { 1 }, neck = { 2 }, shoulder = { 3 }, chest = { 5 }, waist = { 6 }, legs = { 7 }, feet = { 8 }, wrist = { 9 },
	hands = { 10 }, finger = { 11, 12 }, trinket = { 13, 14 }, back = { 15 }, ["main hand"] = { 16 }, ["off hand"] = { 17 },
	ranged = { 18 },
}
local LOCS = { -- equip location -> slot group
	INVTYPE_HEAD = "head", INVTYPE_NECK = "neck", INVTYPE_SHOULDER = "shoulder", INVTYPE_CLOAK = "back",
	INVTYPE_CHEST = "chest", INVTYPE_ROBE = "chest", INVTYPE_WRIST = "wrist", INVTYPE_HAND = "hands",
	INVTYPE_WAIST = "waist", INVTYPE_LEGS = "legs", INVTYPE_FEET = "feet", INVTYPE_FINGER = "finger",
	INVTYPE_TRINKET = "trinket", INVTYPE_WEAPON = "main hand", INVTYPE_2HWEAPON = "main hand",
	INVTYPE_WEAPONMAINHAND = "main hand", INVTYPE_WEAPONOFFHAND = "off hand", INVTYPE_SHIELD = "off hand",
	INVTYPE_HOLDABLE = "off hand", INVTYPE_RANGED = "ranged", INVTYPE_RANGEDRIGHT = "ranged", INVTYPE_THROWN = "ranged",
	INVTYPE_RELIC = "ranged",
}
local RANGED = { INVTYPE_RANGED = true, INVTYPE_RANGEDRIGHT = true, INVTYPE_THROWN = true }
-- The skill line each armor and weapon type needs (Enum.ItemArmorSubclass, Enum.ItemWeaponSubclass). Forever keeps
-- vanilla's weapon skills (Camelot/SkillsFrame.lua lists them, fist weapons as 162); armor proficiencies are
-- vanilla's skill lines too. Types not listed (misc, cosmetic, relics) need none.
local SKILLS = {
	[ARMOR] = { [1] = { 415 }, [2] = { 414 }, [3] = { 413 }, [4] = { 293 }, [6] = { 433 } },
	[WEAPON] = { [0] = { 44 }, [1] = { 172 }, [2] = { 45 }, [3] = { 46 }, [4] = { 54 }, [5] = { 160 }, [6] = { 229 },
		[7] = { 43 }, [8] = { 55 }, [10] = { 136 }, [13] = { 162, 473 }, [15] = { 173 }, [16] = { 176 }, [18] = { 226 },
		[19] = { 228 } },
}
local FISHING_POLE = 20

-- A tooltip line printed from a format string, e.g. "Requires Level %d", as a pattern.
local function LinePattern(format)
	return "^" .. format:gsub("%%d", "\1"):gsub("[%^%$%(%)%%%.%[%]%*%+%-%?]", "%%%0"):gsub("\1", "%%d+") .. "$"
end
local LEVEL_LINE = LinePattern(ITEM_MIN_LEVEL or "Requires Level %d")

local finder -- the search for the latest query (see Search)
local finderFrame = CreateFrame("Frame")
local loggingIn -- the stored payload (Inbox.lua) is being applied: its query is an old one

-- Hands the frame back once this one's time is used up; true if it did.
local function Pause(f)
	if debugprofilestop() - f.started < FIND_BUDGET_MS then return false end
	coroutine.yield()
	return true
end

-- A gear item's suffixes as the full scan that last saw it: the scan's number, { { suffix, price, listed } }
-- cheapest first, and whether some of its auctions' suffixes weren't known.
local function Variants(realm, itemID)
	local k, rest = tostring(realm.variants[itemID] or ""):match("^(%d+)(.*)$")
	if not k then return nil end
	local list, unknown = {}, false
	for part in rest:gmatch(";([^;]+)") do
		local suffix, price, listed = part:match("^([^,]+),(%d*),(%d+)$")
		if suffix then
			list[#list + 1] = { suffix = suffix, price = tonumber(price), listed = tonumber(listed) }
		elseif part == "?" then
			unknown = true
		end
	end
	return tonumber(k), list, unknown
end

local function Cached(itemID)
	local ok, cached = pcall(C_Item.IsItemDataCachedByID, itemID)
	return ok and cached
end

-- The weights the server sent: { stats = { [GetItemStats key] = weight }, armor, melee, ranged } (weapon DPS).
local function Weights(w)
	w = type(w) == "table" and w or {}
	local stats = {}
	for key, value in pairs(type(w.stats) == "table" and w.stats or {}) do
		if type(key) == "string" and tonumber(value) then stats[key] = tonumber(value) end
	end
	return { stats = stats, armor = tonumber(w.armor) or 0, melee = tonumber(w.melee) or 0, ranged = tonumber(w.ranged) or 0 }
end

-- The server scores the same way (tools/upgrades.py), to explain it.
local function Score(w, stats, armor, loc)
	local score = (armor or 0) * w.armor
	for key, value in pairs(stats or {}) do
		local weight
		if key == DPS then weight = RANGED[loc] and w.ranged or w.melee else weight = w.stats[key] end
		score = score + value * (weight or 0)
	end
	return score
end

-- The armor and weapon skills the character has, by skill line ID, and for which item classes the client
-- listed any (where it listed none, that class isn't checked this way; the tooltip still is). Lines under a
-- collapsed header in the Skills window only answer by ID.
local function Skills()
	local known, listed = {}, {}
	for class, types in pairs(SKILLS) do
		for _, ids in pairs(types) do
			for _, id in ipairs(ids) do
				local ok, info = pcall(C_SkillInfo.GetSkillLineInfoByID, id)
				if ok and type(info) == "table" and not info.isHeader and (info.maxRank or 0) > 0 then
					known[id], listed[class] = true, true
				end
			end
		end
	end
	return known, listed
end

local function Proficient(f, class, subclass)
	if class == WEAPON and subclass == FISHING_POLE then return false end
	local ids = SKILLS[class] and SKILLS[class][subclass]
	-- What they wear they can use, whatever the skill lines say.
	if not ids or not f.listed[class] or f.wearing[class .. ":" .. subclass] then return true end
	for _, id in ipairs(ids) do
		if f.skills[id] then return true end
	end
	return false
end

-- The equipped gear with its stats (Inventory.lua) and score, by inventory slot.
local function Equipped(f)
	f.eq, f.wearing = {}, {}
	for _, item in ipairs(ns.Inventory.Equipment()) do
		local ok, _, _, _, loc, _, class, subclass = pcall(C_Item.GetItemInfoInstant, item.id)
		if ok then
			item.loc = loc
			if class == WEAPON or class == ARMOR then
				item.gear = true
				f.wearing[class .. ":" .. subclass] = true
			end
		end
		item.score = Score(f.weights, item.stats, item.armor, item.loc)
		f.eq[item.slot] = item
	end
	f.twoHand = f.eq[16] ~= nil and f.eq[16].loc == TWO_HAND
end

-- C_PlayerInfo.CanUseItem doesn't say what it checks (the level too?), so it's only asked about items the
-- character has the level for, and only if it says yes to all the gear they wear.
local function Trusted(f)
	local yes = false
	for _, item in pairs(f.eq) do
		if item.gear then
			local ok, usable = pcall(C_PlayerInfo.CanUseItem, item.id)
			if not (ok and usable == true) then return false end
			yes = true
		end
	end
	return yes
end

local function Worth(f, slot)
	return f.eq[slot] and f.eq[slot].score or 0
end

-- What a candidate in a slot group would replace: its score and inventory slots. A two-hander replaces the off
-- hand too; a ring or trinket the weaker of the two. nil for an off hand while a two-hander is equipped.
local function Baseline(f, group, loc)
	if group == "main hand" and loc == TWO_HAND and f.eq[17] then return Worth(f, 16) + Worth(f, 17), { 16, 17 } end
	if group == "off hand" and f.twoHand then return nil end
	local slots = GROUP_SLOTS[group]
	local slot = slots[1]
	if slots[2] and Worth(f, slots[2]) < Worth(f, slot) then slot = slots[2] end
	return Worth(f, slot), { slot }
end

local function GroupsFor(f, loc)
	local groups = {}
	if LOCS[loc] and f.groups[LOCS[loc]] then groups[1] = LOCS[loc] end
	if loc == "INVTYPE_WEAPON" and f.dualWield and f.groups["off hand"] then groups[#groups + 1] = "off hand" end
	return groups
end

-- Stage 1: whether an item could go in a slot group asked for: { groups, loc, class, subclass, subtype }, or nil.
-- Only what fits is kept: a table of every item ID would grow in steps too big for a frame.
local function Fits(f, itemID)
	local fit = f.fits[itemID]
	if fit then return fit end
	f.checked = f.checked + 1
	local ok, _, _, subtype, loc, _, class, subclass = pcall(C_Item.GetItemInfoInstant, itemID)
	if ok and (class == WEAPON or class == ARMOR) and (loc ~= "INVTYPE_WEAPONOFFHAND" or f.dualWield) then
		local groups = GroupsFor(f, loc)
		if groups[1] and Proficient(f, class, subclass) then
			fit = { groups = groups, loc = loc, class = class, subclass = subclass, subtype = subtype }
			f.fits[itemID] = fit
		end
	end
	return fit
end

-- An item, or one random suffix of it, and where to get it (offers).
local function Candidate(f, itemID, fit, suffix)
	local key = suffix and (itemID .. ":" .. suffix) or itemID
	local c = f.cands[key]
	if not c then
		c = { id = itemID, suffix = suffix, link = "item:" .. itemID .. (suffix and ("::::::" .. suffix) or ""), fit = fit,
			offers = {} }
		f.cands[key] = c
		f.queue[#f.queue + 1] = c
	end
	return c
end

local function Offer(f, itemID, offer, suffix)
	local c = f.cands[suffix and (itemID .. ":" .. suffix) or itemID]
	if not (c and c.asked) then
		if offer.price and f.maxPrice and offer.price > f.maxPrice then return end
		local fit = Fits(f, itemID)
		if not fit then return end
		c = Candidate(f, itemID, fit, suffix)
	end
	c.offers[#c.offers + 1] = offer
	return c
end

-- An item the server asked about ("is X an upgrade?"): compared in its own slot whatever the query's slots,
-- prices and levels, and answered with why the character can't use it if they can't.
local function AskedItem(f, itemID)
	local ok, _, _, subtype, loc, _, class, subclass = pcall(C_Item.GetItemInfoInstant, itemID)
	if not (ok and LOCS[loc] and (class == WEAPON or class == ARMOR)) then
		f.notGear[#f.notGear + 1] = itemID
		return
	end
	local fit = { groups = { LOCS[loc] }, loc = loc, class = class, subclass = subclass, subtype = subtype }
	if loc == "INVTYPE_WEAPON" and f.dualWield then fit.groups[2] = "off hand" end
	local c = Candidate(f, itemID, fit)
	c.asked = true
	if not Proficient(f, class, subclass) then c.usable, c.cant = false, subtype end
end

-- Rewards of the quests in the log, given or to choose from. The quest log's own reward functions, by questID,
-- so its selection stays as it is (as in Quests.lua). Runs in one go: a quest log is short.
local function QuestRewards(f)
	for i = 1, C_QuestLog.GetNumQuestLogEntries() do
		local info = C_QuestLog.GetInfo(i)
		local questID = info and not info.isHeader and info.questID or 0
		local ok, shown = pcall(C_QuestLog.ShouldShowQuestRewards, questID)
		if questID > 0 and not (ok and shown == false) then
			local items, choices = GetNumQuestLogRewards(questID), GetNumQuestLogChoices(questID)
			local got, loaded = pcall(HaveQuestRewardData, questID)
			if items + choices == 0 and not (got and loaded) then
				pcall(C_QuestLog.RequestLoadQuestByID, questID)
				f.questsLoading = true
			end
			local quest = { src = "quest", quest = info.title, questID = questID,
				ready = C_QuestLog.IsComplete(questID) and true or nil }
			for k = 1, items do
				local itemID = select(6, GetQuestLogRewardInfo(k, questID))
				if itemID then Offer(f, itemID, quest) end
			end
			local choice = { src = "quest", quest = quest.quest, questID = questID, ready = quest.ready,
				choice = choices > 1 and choices or nil }
			for k = 1, choices do
				local itemID = select(6, GetQuestLogChoiceInfo(k, questID))
				if itemID then Offer(f, itemID, choice) end
			end
		end
	end
end

-- What the vendors the player has seen sell (Knowledge.lua's cache, for all characters), but what they wouldn't
-- sell them then or had sold out.
local function VendorItems(f)
	local knowledge = WowMCPDB.knowledge
	for _, vendor in ipairs(knowledge and knowledge.vendors or {}) do
		for _, item in ipairs(vendor.items or {}) do
			if type(item.id) == "number" and not item.locked and item.stock ~= 0 then
				Offer(f, item.id, { src = "vendor", vendor = vendor.name, zone = vendor.zone, subzone = vendor.subzone,
					x = vendor.x, y = vendor.y, at = vendor.at, price = math.floor((item.price or 0) / (item.stack or 1) + 0.5),
					cost = item.cost })
			end
			Pause(f)
		end
	end
end

local function AuctionItem(f, realm, itemID)
	if f.auctioned[itemID] or not Fits(f, itemID) then return end
	f.auctioned[itemID] = true
	local known = Known(realm, itemID)
	if not (known and (known.listed or 0) > 0 and known.price) then return end
	local k, variants, unknown = Variants(realm, itemID)
	if variants and variants[1] and k == realm.current and known.src == "scan" then
		for _, v in ipairs(variants) do
			if v.price then Offer(f, itemID, { src = "auction", price = v.price, listed = v.listed, at = known.at }, v.suffix) end
		end
	else
		-- Suffixes not seen: the cheapest of any, which the server shows as such.
		Offer(f, itemID, { src = "auction", price = known.price, listed = known.listed, at = known.at,
			search = known.src == "search" or nil, random = (variants or unknown) and true or nil })
	end
end

-- Searched items first (a few hundred at most), then the full scan's thousands. Items new to the scan may be
-- stored in between (a scan being read): then the walk starts over once that's done (what fits isn't offered
-- twice).
local function AuctionItems(f)
	local realm = Realm()
	local latest = CurrentScan(realm)
	f.scan = latest and { at = latest.at, items = latest.items }
	local searched = {}
	for itemID in pairs(realm.items) do searched[#searched + 1] = itemID end
	for _, itemID in ipairs(searched) do
		AuctionItem(f, realm, itemID)
		Pause(f)
	end
	local again
	repeat
		while reader and reader.phase == "store" do coroutine.yield() end
		local added = scanAdded
		again = false
		for itemID in pairs(realm.scan) do
			AuctionItem(f, realm, itemID)
			if Pause(f) and scanAdded ~= added then
				again = true
				break
			end
		end
	until not again
end

-- Stage 2 for one candidate whose data is there: the level, stats and score, and the slot groups where it beats
-- what it would replace.
local function Evaluate(f, c)
	local _, link, quality, ilvl, minLevel = C_Item.GetItemInfo(c.id)
	if not quality then
		f.notLoaded = f.notLoaded + 1
		return
	end
	minLevel = minLevel or 0
	if minLevel > f.level + f.levelAhead and not c.asked then return end
	c.quality, c.ilvl, c.minLevel = quality, ilvl, minLevel
	-- A suffix's link is made from its ID (see Candidate); the plain item has its own.
	if not c.suffix and type(link) == "string" then c.link = link end
	c.stats, c.armor = ns.Inventory.GearStats(c.link, c.fit.class, c.fit.subclass)
	c.score = Score(f.weights, c.stats, c.armor, c.fit.loc)
	local price -- the cheapest way to get it, for ties
	for _, offer in ipairs(c.offers) do price = math.min(price or math.huge, offer.price or 0) end
	for _, group in ipairs(c.fit.groups) do
		local base, vs = Baseline(f, group, c.fit.loc)
		if not base and c.asked then base, vs = Worth(f, 17), { 17 } end
		if base and (c.asked or c.score > base + MIN_GAIN) then
			f.ranked[group] = f.ranked[group] or {}
			table.insert(f.ranked[group], { c = c, delta = c.score - base, vs = vs, price = price or math.huge })
		end
	end
end

local function TryEvaluate(f, c)
	local ok, err = pcall(Evaluate, f, c)
	if not ok then f.problem = tostring(err) end
end

-- Stage 2: the candidates loaded, at most LOAD_BATCH waiting at a time; an item that doesn't come is left out.
local function Load(f)
	local queue, loading, i = f.queue, {}, 1
	while i <= #queue or loading[1] do
		while i <= #queue and #loading < LOAD_BATCH do
			local c = queue[i]
			i = i + 1
			f.loaded = i - 1 -- for the panel's progress
			if Cached(c.id) then
				TryEvaluate(f, c)
				Pause(f)
			else
				pcall(C_Item.RequestLoadItemDataByID, c.id)
				loading[#loading + 1] = { c = c, at = GetTime() }
			end
		end
		if loading[1] then
			coroutine.yield()
			local still = {}
			for _, l in ipairs(loading) do
				if Cached(l.c.id) then
					TryEvaluate(f, l.c)
					Pause(f)
				elseif GetTime() - l.at > LOAD_TIMEOUT then
					f.notLoaded = f.notLoaded + 1
				else
					still[#still + 1] = l
				end
			end
			loading = still
		end
	end
end

-- Stage 3 for one candidate: whether the character can use it (c.cant says why not). Its tooltip has no red
-- line but the level, and the game agrees, where it's asked (see Trusted). The name with a suffix is read there.
local function Usable(f, c)
	if c.usable ~= nil then return c.usable end
	local data = c.suffix and C_TooltipInfo.GetHyperlink(c.link) or C_TooltipInfo.GetItemByID(c.id)
	local lines = ns.Inventory.Lines(data)
	if not lines[1] or lines[1].left == RETRIEVING_ITEM_INFO then
		c.usable, c.cant = false, "not loaded"
		return false
	end
	c.name = lines[1].left
	c.usable = true
	for _, line in ipairs(lines) do
		if ITEM_RANDOM_ENCHANT and line.left == ITEM_RANDOM_ENCHANT then c.random = true end
		if line.red and c.usable and not line.left:match(LEVEL_LINE) then
			c.usable, c.cant = false, line.right or line.left
		end
	end
	if c.usable and f.canUse and c.minLevel <= f.level then
		local ok, yes = pcall(C_PlayerInfo.CanUseItem, c.id)
		if ok and yes == false then c.usable, c.cant = false, "the game says this character can't use it" end
	end
	if not c.usable then f.unusable = f.unusable + 1 end
	return c.usable
end

-- Best first: the bigger gain, then the cheaper, then by ID.
local function Before(a, b)
	if a.delta ~= b.delta then return a.delta > b.delta end
	if a.price ~= b.price then return a.price < b.price end
	if a.c.id ~= b.c.id then return a.c.id < b.c.id end
	return (a.c.suffix or "") < (b.c.suffix or "")
end

-- Takes the best entry out of a list. A slot can have hundreds, and only the first few are needed, so they're
-- picked one at a time (a frame can end between picks) rather than all sorted at once.
local function TakeBest(list)
	local best
	for i = 1, #list do
		if not best or Before(list[i], list[best]) then best = i end
	end
	if not best then return nil end
	local e = list[best]
	list[best] = list[#list]
	list[#list] = nil
	return e
end

-- Stage 3: per slot group, the best that the character can use, up to perSlot; asked items always.
local function Choose(f)
	for g, group in ipairs(GROUPS) do
		f.group = g - 1 -- for the panel's progress
		local list, kept, n = f.ranked[group] or {}, {}, 0
		for i = #list, 1, -1 do
			local e = list[i]
			if e.c.asked then
				kept[#kept + 1] = e
				list[i] = list[#list]
				list[#list] = nil
			end
		end
		while n < f.perSlot do
			local e = TakeBest(list)
			if not e then break end
			local ok, usable = pcall(Usable, f, e.c)
			if ok and usable then
				kept[#kept + 1] = e
				n = n + 1
			end
			Pause(f)
		end
		for _, e in ipairs(kept) do
			if e.c.asked then pcall(Usable, f, e.c) end
		end
		table.sort(kept, Before)
		if kept[1] then f.kept[group] = kept end
	end
end

local function Item(e)
	local c = e.c
	table.sort(c.offers, function(a, b) return (a.price or 0) < (b.price or 0) end)
	local offers = {}
	for i = 1, math.min(#c.offers, MAX_OFFERS) do offers[i] = c.offers[i] end
	return {
		id = c.id, name = c.name, q = c.quality, ilvl = c.ilvl, req = c.minLevel, loc = c.fit.loc, sub = c.fit.subtype,
		suffix = c.suffix, random = c.random, stats = c.stats, armor = c.armor, vs = e.vs, offers = offers,
		asked = c.asked, cant = c.cant,
	}
end

-- The reply: the equipped gear, and per slot group the candidates kept, best first.
local function Answer(f)
	local equipped, slots, candidates = {}, {}, 0
	for _, group in ipairs(GROUPS) do
		for _, slot in ipairs(GROUP_SLOTS[group]) do
			local e = f.eq[slot]
			if e then
				equipped[#equipped + 1] = { slot = slot, id = e.id, name = e.name, q = e.quality, ilvl = e.ilvl, loc = e.loc,
					stats = e.stats, armor = e.armor, broken = e.durability == 0 or nil }
			end
		end
		if f.groups[group] or f.kept[group] then
			local items = {}
			for _, e in ipairs(f.kept[group] or {}) do items[#items + 1] = Item(e) end
			slots[#slots + 1] = { slot = group, items = items, twoHand = group == "off hand" and f.twoHand or nil }
		end
	end
	for _ in pairs(f.cands) do candidates = candidates + 1 end
	return {
		query = f.id, status = "done", level = f.level, dualWield = f.dualWield, equipped = equipped, slots = slots,
		scan = f.scan, checked = f.checked, candidates = candidates, notLoaded = f.notLoaded, unusable = f.unusable,
		questsLoading = f.questsLoading, notGear = f.notGear[1] and f.notGear or nil, problem = f.problem,
	}
end

local function Prepare(f, q)
	f.level = UnitLevel("player")
	f.levelAhead = math.max(0, math.floor(tonumber(q.levelAhead) or 2))
	f.maxPrice = tonumber(q.maxPrice)
	f.perSlot = math.max(1, math.min(MAX_PER_SLOT, math.floor(tonumber(q.perSlot) or 3)))
	f.groups, f.sources, f.asked, f.notGear = {}, {}, {}, {}
	for _, group in ipairs(type(q.slots) == "table" and q.slots or GROUPS) do
		if GROUP_SLOTS[group] then f.groups[group] = true end
	end
	for _, source in ipairs(type(q.sources) == "table" and q.sources or SOURCES) do f.sources[source] = true end
	for _, itemID in ipairs(type(q.items) == "table" and q.items or {}) do
		if type(itemID) == "number" and #f.asked < MAX_ASKED then f.asked[#f.asked + 1] = itemID end
	end
	f.weights = Weights(q.weights)
	local ok, dual = pcall(CanDualWield)
	f.dualWield = ok and dual == true
	f.skills, f.listed = Skills()
	f.fits, f.auctioned, f.cands, f.queue, f.ranked, f.kept = {}, {}, {}, {}, {}, {}
	f.checked, f.notLoaded, f.unusable = 0, 0, 0
	Equipped(f)
	f.canUse = Trusted(f)
end

-- The whole search, run as a coroutine a little per frame; f.answer is the reply.
local function Search(f, q)
	Prepare(f, q)
	f.stage = "gear"
	for _, itemID in ipairs(f.asked) do AskedItem(f, itemID) end
	if f.sources.quests then
		local ok, err = pcall(QuestRewards, f)
		if not ok then f.problem = tostring(err) end
	end
	if f.sources.vendors then VendorItems(f) end
	if f.sources.auction then AuctionItems(f) end
	f.stage = "stats"
	Load(f)
	f.stage = "usable"
	Choose(f)
	f.answer = Answer(f)
end

local function Resume()
	local f = finder
	if f and f.co then
		f.started = debugprofilestop()
		local ok, err = coroutine.resume(f.co)
		if not ok then
			f.status, f.problem, f.co = "error", tostring(err), nil
		elseif coroutine.status(f.co) == "dead" then
			-- Only the answer is kept.
			f.status, f.doneAt, f.co = "done", GetTime(), nil
			f.fits, f.auctioned, f.cands, f.queue, f.ranked, f.kept, f.eq = nil, nil, nil, nil, nil, nil, nil
		end
	end
	if not (f and f.co) then finderFrame:SetScript("OnUpdate", nil) end
	Notify() -- its progress, at the auction house
end

local function Upgrades()
	local f = finder
	if not f then return {} end
	if f.status == "done" and (f.stale or GetTime() - f.doneAt > KEEP) then
		f.answer = nil
		return { query = f.id, expired = true }
	end
	if f.status == "done" then return f.answer end
	return { query = f.id, status = f.status, stage = f.stage, checked = f.checked, error = f.problem }
end

-- { id, at, slots, sources, maxPrice, levelAhead, perSlot, items (asked about), weights }: a new id starts a new
-- search. Every later payload carries it too, which changes nothing. The one stored for login is from before the
-- /reload: the server sends it again if it still wants it.
ns.RegisterReceiver("upgradeQuery", function(q)
	if type(q) ~= "table" or type(q.id) ~= "number" or (finder and finder.id == q.id) or loggingIn then return end
	local f = { id = q.id, status = "working", stage = "start", checked = 0 }
	f.co = coroutine.create(function() Search(f, q) end)
	finder = f
	finderFrame:SetScript("OnUpdate", Resume)
end)
ns.RegisterSection(ns.Kinds.ITEM_DETAILS, "upgrades", Upgrades)
-- Login hooks run just before the stored payload is applied, in the same event.
ns.OnLogin(function()
	loggingIn = true
	C_Timer.After(0, function() loggingIn = nil end)
end)

-- New gear on: an answer from before is out of date.
finderFrame:RegisterEvent("PLAYER_EQUIPMENT_CHANGED")
finderFrame:SetScript("OnEvent", function()
	if finder and finder.status == "done" then finder.stale = true end
end)

---------------------------------------------------------------------------
-- The status panel
---------------------------------------------------------------------------

-- A line and a thin bar under Blizzard's auction house window while there's something to say: the full scan
-- (waiting for it, reading it), the searches (how many of the AI's and of the bags', which one, how long still), why
-- they wait, and the upgrade finder; then how things stand, for a moment, and it fades out. The tooltip lists what's
-- still to search. Our own frame, parented to UIParent and only anchored to Blizzard's window. The code above calls
-- Notify when something changes; the panel redraws from a timer, at most every REDRAW_EVERY seconds, so the scan
-- reader's frame budget stays its own. /mcp prices panel off hides it (WowMCPDB.auctionPanel, for the account).

local REDRAW_EVERY = 0.25 -- seconds
local HOLD, FADE = 4, 1.5 -- seconds "Prices up to date" stays, then fades out over
local PANEL_MIN, PANEL_MAX, PANEL_PAD = 220, 460, 6 -- width: it grows with its line, right of the window's tabs
local MAX_QUEUE_LINES = 8 -- per group, in the tooltip
local COLORS = { scan = { 0.3, 0.6, 1 }, search = { 1, 0.82, 0 }, paused = { 0.5, 0.5, 0.5 }, done = { 0.25, 0.8, 0.25 },
	problem = { 1, 0.3, 0.3 } }
local WHY = { goal = "goal", plan = "plan", watch = "watched", asked = "asked about", bags = "in your bags",
	bank = "in your bank" }

local panel -- the frame, made the first time it shows
local draw = { at = 0 } -- pending: a redraw is coming; at: the last one; idleAt: since when nothing's going on

local function PanelSaved()
	WowMCPDB.auctionPanel = WowMCPDB.auctionPanel or {}
	return WowMCPDB.auctionPanel
end

-- An item with its icon and name in quality colour (Core.lua); the name the prices have while the client loads it.
local function Shown(realm, itemID)
	local known = realm.items[itemID]
	local name = (ItemName(itemID) or (known and known.name) or ("item " .. itemID)):gsub("[{}|\n]", "")
	return ns.RenderItems(string.format("{item:%d:%s}", itemID, name))
end

-- How far the full scan's reading is, 0 to 1: going through the auctions is most of the work.
local function ReadProgress(r)
	if r.phase == "read" then return 0.75 * r.index / math.max(r.total, 1) end
	if r.phase == "store" then return 0.75 + 0.2 * (r.index - 1) / math.max(#r.ids, 1) end
	return 0.95 + 0.05 * (1 - #r.queue / math.max(r.queued or 0, 1))
end

-- How far the upgrade finder is, 0 to 1, or nil if it isn't searching: going through the items is most of it. Their
-- number is a guess, the latest full scan's and the searched ones.
local function FinderProgress()
	local f = finder
	if not (f and f.co) then return nil end
	if f.stage == "stats" then return 0.6 + 0.3 * (f.loaded or 0) / math.max(#f.queue, 1) end
	if f.stage == "usable" then return 0.9 + 0.1 * (f.group or 0) / #GROUPS end
	if not f.total then
		local realm, n = Realm(), 0
		for _ in pairs(realm.items) do n = n + 1 end
		local latest = CurrentScan(realm)
		f.total = n + (latest and latest.items or 0)
	end
	return 0.6 * math.min(1, f.checked / math.max(f.total, 1))
end

-- This visit's priority list in two groups, what the AI tracks (ai) and what's from the bags and bank (bags): how
-- many were searched (the one under way too) and how many in all; left: how many are still due. order, if given,
-- gets those, in the order they'll go (as NextSearch picks them: the stalest first, then as listed).
local function Queue(realm, order)
	local q = { ai = { done = 0, all = 0 }, bags = { done = 0, all = 0 }, left = 0 }
	for i, p in ipairs(visit.priority) do
		local group = p.ai and q.ai or q.bags
		if visit.searched[p.item] then
			group.done, group.all = group.done + 1, group.all + 1
		elseif Due(realm, p.item) then
			group.all, q.left = group.all + 1, q.left + 1
			if order then order[#order + 1] = { p = p, at = Checked(realm, p.item), i = i } end
		end
	end
	if order then
		table.sort(order, function(a, b)
			if a.at ~= b.at then return a.at < b.at end
			return a.i < b.i
		end)
	end
	return q
end

-- Seconds per search from here on: the last minute's pace, never faster than MAX_PER_MINUTE allows.
local function Pace()
	local now, first, n = GetTime(), nil, 0
	for _, at in ipairs(sent) do
		if now - at < 60 then first, n = first or at, n + 1 end
	end
	local each = 60 / MAX_PER_MINUTE
	if n >= 3 then each = math.max(each, (now - first) / n) end
	return each
end

local function Eta(seconds)
	if seconds < 60 then return string.format("about %d s left", math.max(5, math.floor(seconds / 5 + 0.5) * 5)) end
	return string.format("about %d min left", math.floor(seconds / 60 + 0.5))
end

-- Why the searches wait (see Safe), by the window's view.
local function Paused()
	local f, M = AuctionHouseFrame, AuctionHouseFrameDisplayMode
	if not (f and f:IsShown() and f.GetDisplayMode and M) then
		return "Paused: the game's auction house window isn't showing (another addon's instead?)"
	end
	local mode = f:GetDisplayMode()
	if mode == M.ItemSell or mode == M.CommoditiesSell or mode == M.WoWTokenSell then return "Paused while you sell" end
	if mode == M.Auctions then return "Paused while you look at your auctions" end
	return "Paused while you look at an item"
end

-- Nothing going on: how this visit went.
local function Done()
	if visit.problem then return "|cffff5050Auction prices: " .. ns.Escape(visit.problem) .. "|r" end
	if visit.scanned then return string.format("Full scan: %s items · prices up to date", Thousands(visit.scanned)) end
	local text = "Prices up to date"
	if visit.scan and visit.scan.state == "refused" then text = text .. " · the game didn't send a full scan" end
	local wait = Asked() + SCAN_INTERVAL - time()
	if wait > 0 then text = text .. string.format(" · next full scan possible in %d min", math.ceil(wait / 60)) end
	return text
end

-- What the panel says: its line, the bar's fill (0 to 1) and colour, and true if nothing's going on.
local function Now()
	if reader then
		local done = ReadProgress(reader)
		return string.format("Reading the full scan… %d%%", math.floor(100 * done)), done, COLORS.scan
	end
	if visit.scan and visit.scan.state == "asked" then return "Full scan: waiting for the game…", 0, COLORS.scan end
	local realm = Realm()
	local q, scan = Queue(realm), ScanAllowed()
	if scan or q.left > 0 or current then
		local all = q.ai.all + q.bags.all
		local done = all > 0 and (q.ai.done + q.bags.done) / all or 0
		if not Safe() then
			return Paused() .. " · " .. (scan and "a full scan is next"
				or string.format("%d price%s left", q.left, q.left == 1 and "" or "s")), done, COLORS.paused
		end
		if scan then return "Full scan: asking the game…", 0, COLORS.scan end
		local parts = {}
		if q.ai.all > 0 then parts[#parts + 1] = string.format("watched %d/%d", q.ai.done, q.ai.all) end
		if q.bags.all > 0 then parts[#parts + 1] = string.format("bags %d/%d", q.bags.done, q.bags.all) end
		local item = current and current.item or visit.last
		if item then parts[#parts + 1] = Shown(realm, item) end
		parts[#parts + 1] = q.left > 0 and Eta(q.left * Pace()) or "almost done"
		return "Checking prices: " .. table.concat(parts, " · "), done, COLORS.search
	end
	local finding = FinderProgress()
	if finding then
		return string.format("Looking for upgrades: %d%%", math.floor(100 * finding)), finding, COLORS.scan
	end
	-- A moment's work: said if the panel is there anyway, but it doesn't bring it back once faded.
	if visit.mine.at then return "Reading your auctions…", 1, COLORS.done, true end
	return Done(), 1, visit.problem and COLORS.problem or COLORS.done, true
end

-- Fully there while something's going on, and HOLD seconds after; then fading out over FADE.
local function Alpha()
	if not draw.idleAt then return 1 end
	return math.max(0, math.min(1, 1 - (GetTime() - draw.idleAt - HOLD) / FADE))
end

local function Fade(self)
	if self.hovered then draw.idleAt = GetTime() end -- it stays while the mouse is on it
	local alpha = Alpha()
	if alpha <= 0 then return self:Hide() end
	self:SetAlpha(alpha)
end

local function TipLine(text, r, g, b)
	GameTooltip:AddLine(text, r or 0.8, g or 0.8, b or 0.8, true)
end

-- The full scans, what's still to search in the order it goes (the AI's, then the bags' and bank's), the commands.
local function Tooltip(f)
	local realm, order = Realm(), {}
	local q = Queue(realm, order)
	GameTooltip:SetOwner(f, "ANCHOR_TOP")
	GameTooltip:SetText("Auction prices for your AI", 1, 0.82, 0)
	TipLine((ScanState():gsub("^%l", string.upper)) .. ".")
	if visit.scan and visit.scan.state == "refused" then
		TipLine("The game didn't send the full scan asked for (another addon's may have used it up), so prices are "
			.. "searched one at a time.")
	end
	if visit.problem then TipLine("Problem: " .. ns.Escape(visit.problem), 1, 0.3, 0.3) end
	if current then TipLine("Checking now: " .. Shown(realm, current.item), 1, 1, 1) end
	for _, group in ipairs({ { ai = true, title = "For your AI (goals, plan, watched, asked about)" },
		{ ai = false, title = "From your bags and bank" } }) do
		local list = {}
		for _, e in ipairs(order) do
			if (e.p.ai == true) == group.ai then list[#list + 1] = e.p end
		end
		if list[1] then
			TipLine(string.format("%s, %d still to check:", group.title, #list), 1, 1, 1)
			for i = 1, math.min(#list, MAX_QUEUE_LINES) do
				TipLine("   " .. Shown(realm, list[i].item) .. GREY .. " (" .. (WHY[list[i].why] or list[i].why) .. ")|r")
			end
			if #list > MAX_QUEUE_LINES then TipLine(string.format("   and %d more", #list - MAX_QUEUE_LINES)) end
		end
	end
	if q.left == 0 then TipLine("Nothing left to check at this visit.") end
	TipLine("/mcp prices: the prices your AI has, and look the list up again. /mcp prices panel off: hide this.",
		0.4, 0.8, 1)
	GameTooltip:Show()
end

-- Under the window at its right: its tabs hang under the left side, and nothing else of it is there but the corner
-- of its frame, 8 px down on Forever (Camelot/NineSliceLayoutOverrides.lua). Without Blizzard's window (another
-- addon's instead), at the top of the screen.
local function Anchor(f)
	local ah = AuctionHouseFrame
	local to = ah and ah:IsShown() and ah or UIParent
	if f.anchoredTo == to then return end
	f.anchoredTo = to
	f:ClearAllPoints()
	if to == UIParent then
		f:SetPoint("TOP", UIParent, "TOP", 0, -40)
		f:SetFrameStrata("MEDIUM")
	else
		f:SetPoint("TOPRIGHT", ah, "BOTTOMRIGHT", -6, -10)
		f:SetFrameStrata(ah:GetFrameStrata())
	end
end

-- Wide enough for its line, up to PANEL_MAX; it doesn't narrow again while the window's open, so it doesn't jump.
local function Fit(f)
	local want = math.min(PANEL_MAX, math.max(PANEL_MIN, f.Text:GetUnboundedStringWidth() + 2 * PANEL_PAD))
	if want > (f.wide or 0) then
		f.wide = want
		f:SetWidth(want)
		f.Text:SetWidth(want - 2 * PANEL_PAD)
	end
end

local function CreatePanel()
	local f = CreateFrame("Frame", "WowMCPAuctionPanel", UIParent)
	f:SetSize(PANEL_MIN, 24)
	f:SetClampedToScreen(true) -- the window can be near the bottom of the screen
	f:EnableMouse(true) -- for the tooltip
	local bg = f:CreateTexture(nil, "BACKGROUND")
	bg:SetAllPoints()
	bg:SetColorTexture(0, 0, 0, 0.6)
	f.Text = f:CreateFontString(nil, "ARTWORK", "GameFontHighlightSmall")
	f.Text:SetPoint("TOPLEFT", f, "TOPLEFT", PANEL_PAD, -4)
	f.Text:SetJustifyH("LEFT")
	f.Text:SetWordWrap(false)
	f.Bar = CreateFrame("StatusBar", nil, f)
	f.Bar:SetPoint("BOTTOMLEFT", f, "BOTTOMLEFT", PANEL_PAD, 5)
	f.Bar:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", -PANEL_PAD, 5)
	f.Bar:SetHeight(3)
	f.Bar:SetStatusBarTexture("Interface\\TargetingFrame\\UI-StatusBar")
	f.Bar:SetMinMaxValues(0, 1)
	local track = f.Bar:CreateTexture(nil, "BACKGROUND")
	track:SetAllPoints()
	track:SetColorTexture(1, 1, 1, 0.12)
	f:SetScript("OnEnter", function(self)
		self.hovered = true
		if visit then Tooltip(self) end
	end)
	f:SetScript("OnLeave", function(self)
		self.hovered = nil
		GameTooltip:Hide()
	end)
	return f
end

local function Hide()
	if panel then
		panel:Hide()
		panel.wide = nil
		if panel.hovered then
			panel.hovered = nil
			GameTooltip:Hide()
		end
	end
	draw.idleAt = nil
end

local function Draw()
	draw.at = GetTime()
	if not visit or PanelSaved().hidden then return Hide() end
	local text, fill, color, idle = Now()
	if not idle then
		draw.idleAt = nil
	elseif not draw.idleAt then
		draw.idleAt = draw.at
	end
	if Alpha() <= 0 and not (panel and panel.hovered) then
		if panel then panel:Hide() end -- said, and faded out
		return
	end
	panel = panel or CreatePanel()
	local f = panel
	Anchor(f)
	f.Text:SetText(text)
	Fit(f)
	f.Bar:SetValue(fill)
	f.Bar:SetStatusBarColor(color[1], color[2], color[3])
	f:SetAlpha(Alpha())
	f:SetScript("OnUpdate", idle and Fade or nil)
	f:Show()
	if f.hovered then Tooltip(f) end
end

local function Redraw()
	draw.pending = false
	local ok, err = pcall(Draw)
	if not ok then Report(err) end
end

function Notify()
	if not visit then
		-- Gone with the window; also when it had faded out, so the next visit starts afresh.
		if draw.idleAt or (panel and panel:IsShown()) then Hide() end
		return
	end
	if draw.pending then return end
	draw.pending = true
	C_Timer.After(math.max(0, draw.at + REDRAW_EVERY - GetTime()), Redraw)
end

-- /mcp prices panel [on, off]
function PanelCommand(arg)
	if arg == "off" then
		PanelSaved().hidden = true
		Hide()
	elseif arg == "on" then
		PanelSaved().hidden = nil
		draw.idleAt = nil -- says how things stand once more
		Notify()
	end
	if PanelSaved().hidden then
		ns.Print("The auction house status panel is off. /mcp prices panel on brings it back.")
	else
		ns.Print("The auction house status panel is on: under the auction house window, what's being looked up and "
			.. "how much is left. /mcp prices panel off hides it.")
	end
end

ns.OnItemsLoaded(Notify) -- names shown before they loaded
