-- The mailbox (Inventory.lua). As on the real client, the inbox is only there once the game has
-- fetched it (CheckInbox, which Blizzard's mail window calls when it opens): until then
-- GetInboxNumItems() is 0. The inbox shows at most client.mail.max mails; the rest wait on the
-- server and count in GetInboxNumItems' total. Mail that arrives later also waits for the next
-- check, while the minimap's new-mail notice (HasNewMail, GetLatestThreeSenders) says so.
--
-- client.mail.all is every mail on the server, newest first:
--   { sender, subject, money, cod, expires (unix time), read, returned, player (it can be answered),
--     gm, body (has text: it stays once emptied; mail without, like the auction house's, goes),
--     items = { [attachment slot] = { id, count } },
--     invoice = { type, item, player, bid, buyout, deposit, fee, delay, etaHour, etaMin, count } }

Enum.PlayerInteractionType = Enum.PlayerInteractionType or {}
Enum.PlayerInteractionType.MailInfo = 17

ATTACHMENTS_MAX_RECEIVE = 16
AUCTION_SOLD_MAIL_SUBJECT = "Auction successful: %s"
AUCTION_WON_MAIL_SUBJECT = "Auction won: %s"
AUCTION_EXPIRED_MAIL_SUBJECT = "Auction expired: %s"
AUCTION_REMOVED_MAIL_SUBJECT = "Auction cancelled: %s"
AUCTION_OUTBID_MAIL_SUBJECT = "Outbid on %s"

client.mail = {
	all = {},
	inbox = {},    -- what the client fetched at the last check: the first max of all
	total = 0,     -- #all at the last check, less what was deleted since
	max = 50,
	new = {},      -- senders of mail that arrived since the last check, newest first
	open = false,
	latency = 0.5, -- seconds until the server answers
	checks = 0,
}

local function Shown(index)
	return client.mail.inbox[index]
end

local function Remove(list, mail)
	for i, m in ipairs(list) do
		if m == mail then return table.remove(list, i) end
	end
end

function CheckInbox()
	local m = client.mail
	m.checks = m.checks + 1
	C_Timer.After(m.latency, function()
		m.inbox = {}
		for i = 1, math.min(#m.all, m.max) do m.inbox[i] = m.all[i] end
		m.total = #m.all
		if #m.new > 0 then
			wipe(m.new)
			client.Fire("UPDATE_PENDING_MAIL")
		end
		client.Fire("MAIL_INBOX_UPDATE")
	end)
end

function GetInboxNumItems()
	return #client.mail.inbox, client.mail.total
end

local function FirstItem(mail)
	for slot = 1, ATTACHMENTS_MAX_RECEIVE do
		if mail.items and mail.items[slot] then return slot, mail.items[slot] end
	end
end

local function Count(mail)
	local n = 0
	for _ in pairs(mail.items or {}) do n = n + 1 end
	return n > 0 and n or nil
end

-- packageIcon, stationeryIcon, sender, subject, money, CODAmount, daysLeft, itemCount, wasRead, wasReturned,
-- textCreated, canReply, isGM, firstItemQuantity, firstItemLink.
function GetInboxHeaderInfo(index)
	local mail = Shown(index)
	if not mail then return nil end
	local _, first = FirstItem(mail)
	local link = first and select(2, C_Item.GetItemInfo(first.id))
	return Count(mail) and 132761 or nil, 134939, mail.sender, mail.subject or "", mail.money or 0, mail.cod or 0,
		(mail.expires - time()) / 86400, Count(mail), mail.read == true, mail.returned == true, false,
		mail.player == true, mail.gm == true, first and (first.count or 1), link
end

-- name, itemID, texture, count, quality, canUse, isCurrency. No name until the item's data has loaded.
function GetInboxItem(index, slot)
	local mail = Shown(index)
	local attached = mail and mail.items and mail.items[slot]
	if not attached then return nil end
	local item = client.items[attached.id]
	local name = C_Item.GetItemInfo(attached.id)
	return name, attached.id, item.icon or 134400, attached.count or 1, item.quality, true, false
end

function GetInboxItemLink(index, slot)
	local mail = Shown(index)
	local attached = mail and mail.items and mail.items[slot]
	return attached and select(2, C_Item.GetItemInfo(attached.id)) or nil
end

function HasInboxItem(index, slot)
	local mail = Shown(index)
	return mail ~= nil and mail.items ~= nil and mail.items[slot] ~= nil
end

-- invoiceType, itemName, playerName, bid, buyout, deposit, consignment, moneyDelay, etaHour, etaMin, count,
-- commerceAuction; nothing for mail that isn't an auction house invoice.
function GetInboxInvoiceInfo(index)
	local mail = Shown(index)
	local i = mail and mail.invoice
	if not i then return nil end
	return i.type, i.item, i.player, i.bid or 0, i.buyout or 0, i.deposit or 0, i.fee or 0, i.delay or 0,
		i.etaHour or 0, i.etaMin or 0, i.count or 1, false
end

-- false for what the mail window offers to return instead: another player's mail with something attached.
function InboxItemCanDelete(index)
	local mail = Shown(index)
	if not mail then return false end
	return not (mail.player and not mail.returned and (Count(mail) or (mail.money or 0) > 0))
end

function HasNewMail()
	return #client.mail.new > 0
end

function GetLatestThreeSenders()
	local m = client.mail.new
	return m[1], m[2], m[3]
end

-- Mail without text goes once there's nothing left in it (the auction house's does).
local function Emptied(mail)
	if mail.body or Count(mail) or (mail.money or 0) > 0 then return end
	Remove(client.mail.inbox, mail)
	Remove(client.mail.all, mail)
	client.mail.total = client.mail.total - 1
end

-- The server's answer comes a moment later, with the inbox as it is now. The attachment goes into the backpack.
function TakeInboxItem(index, slot)
	local mail = assert(client.mail.open and Shown(index), "not at a mailbox")
	local attached = assert(mail.items[slot], "nothing attached there")
	mail.items[slot] = nil
	mail.read = true
	C_Timer.After(client.mail.latency, function()
		client.AddItem(attached.id, attached.count)
		Emptied(mail)
		client.Fire("MAIL_SUCCESS", attached.id)
		client.Fire("MAIL_INBOX_UPDATE")
	end)
end

function TakeInboxMoney(index)
	local mail = assert(client.mail.open and Shown(index), "not at a mailbox")
	local money = mail.money
	mail.money, mail.read = 0, true
	C_Timer.After(client.mail.latency, function()
		client.stats.money = client.stats.money + (money or 0)
		Emptied(mail)
		client.Fire("MAIL_SUCCESS")
		client.Fire("MAIL_INBOX_UPDATE")
	end)
end

function DeleteInboxItem(index)
	local mail = assert(client.mail.open and Shown(index), "not at a mailbox")
	Remove(client.mail.inbox, mail)
	Remove(client.mail.all, mail)
	client.mail.total = client.mail.total - 1
	client.Fire("CLOSE_INBOX_ITEM", index)
	C_Timer.After(client.mail.latency, function()
		client.Fire("MAIL_SUCCESS")
		client.Fire("MAIL_INBOX_UPDATE")
	end)
end

-- The player clicks a mailbox: the interaction opens Blizzard's mail window, which checks the inbox.
function client.OpenMailbox()
	client.mail.open = true
	client.Fire("PLAYER_INTERACTION_MANAGER_FRAME_SHOW", Enum.PlayerInteractionType.MailInfo)
	client.Fire("MAIL_SHOW")
	CheckInbox()
end

function client.CloseMailbox()
	client.mail.open = false
	client.Fire("MAIL_CLOSED")
	client.Fire("PLAYER_INTERACTION_MANAGER_FRAME_HIDE", Enum.PlayerInteractionType.MailInfo)
end

-- Mail arrives: on the server, and in the new-mail notice, until the next check.
function client.Deliver(mail)
	mail.expires = mail.expires or time() + 30 * 86400
	table.insert(client.mail.all, 1, mail)
	table.insert(client.mail.new, 1, mail.sender)
	client.Fire("UPDATE_PENDING_MAIL")
end
