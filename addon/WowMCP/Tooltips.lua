-- WowMCP tooltips: notes from the AI on item tooltips (payload key "itemNotes").
--
-- Every tooltip built from tooltip data (bags, character frame, vendors, loot, chat links,
-- comparison tooltips) runs the post-calls registered for its data type after its own lines,
-- so one hook covers them all.

local _, ns = ...

local notes = {} -- itemID -> text

local function ItemID(data)
	if type(data.id) == "number" then return data.id end
	local link = data.hyperlink
	return type(link) == "string" and tonumber(link:match("item:(%d+)")) or nil
end

local function AddNote(tooltip, data)
	-- pcall: restricted content can hand out secret values that can't be used as keys.
	local ok, text = pcall(function() return type(data) == "table" and notes[ItemID(data)] end)
	if ok and text then
		tooltip:AddLine("|cff33ff99AI:|r " .. ns.ItemText(text), 0.4, 0.8, 1, true)
	end
end

local function Receive(payload)
	if type(payload) ~= "table" then return end
	wipe(notes)
	local count = 0
	for _, note in ipairs(type(payload.notes) == "table" and payload.notes or {}) do
		if type(note) == "table" and type(note.item) == "number" and note.text then
			if not notes[note.item] then count = count + 1 end
			notes[note.item] = tostring(note.text)
		end
	end
	-- Announce each update once; a /reload restores the notes quietly.
	if payload.id == WowMCPDB.itemNotesId then return end
	WowMCPDB.itemNotesId = payload.id
	if count == 0 then
		ns.Print("Item notes cleared.")
	else
		ns.Print(string.format("Item notes updated: %d item(s) have a note on their tooltip.", count))
	end
end

if TooltipDataProcessor and TooltipDataProcessor.AddTooltipPostCall and Enum.TooltipDataType then
	TooltipDataProcessor.AddTooltipPostCall(Enum.TooltipDataType.Item, AddNote)
end

ns.RegisterReceiver("itemNotes", Receive)
