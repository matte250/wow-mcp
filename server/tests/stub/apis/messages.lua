-- On-screen text, UI sounds and the clock (Messages.lua, Reminders.lua).

-- PlaySound(soundKitID, channel): the IDs played, in order.
SOUNDKIT = SOUNDKIT or {}
SOUNDKIT.TELL_MESSAGE, SOUNDKIT.RAID_WARNING, SOUNDKIT.ALARM_CLOCK_WARNING_2 = 3081, 8959, 12867
client.sounds = {}
function PlaySound(id)
	table.insert(client.sounds, id)
	return true, #client.sounds
end

-- Player.lua's stubs say where the player is; these follow them.
function GetZoneText() return GetRealZoneText() end
function GetMinimapZoneText() return GetSubZoneText() end

-- Texts of the visible font strings (shown, not faded out), top to bottom: the top one is
-- anchored to its frame, each next one to the one above. Errors if they aren't laid out so.
function client.CenterText()
	local visible = {}
	for _, fs in ipairs(client.fontStrings) do
		if fs:IsShown() and fs:GetAlpha() > 0 then visible[fs] = true end
	end
	local texts, above = {}, nil
	while next(visible) do
		local below
		for fs in pairs(visible) do
			local anchor = fs.points[#fs.points]
			if anchor and anchor[2] == (above or fs.parent) then below = fs end
		end
		assert(below, "visible font strings aren't stacked")
		table.insert(texts, below.text)
		visible[below], above = nil, below
	end
	return texts
end
