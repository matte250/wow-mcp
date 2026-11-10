-- On-screen text, UI sounds and the clock (Messages.lua, Reminders.lua).

-- PlaySound(soundKitID, channel): the IDs played, in order.
SOUNDKIT = SOUNDKIT or {}
SOUNDKIT.TELL_MESSAGE, SOUNDKIT.RAID_WARNING, SOUNDKIT.ALARM_CLOCK_WARNING_2 = 3081, 8959, 12867
client.sounds = {}
function PlaySound(id)
	table.insert(client.sounds, id)
	return true, #client.sounds
end

-- Unix time follows the virtual clock (client.now); a test can move client.epoch.
client.epoch = os.time()
function time() return math.floor(client.epoch + client.now) end
date = os.date

-- Player.lua's stubs say where the player is; these follow them.
function GetZoneText() return GetRealZoneText() end
function GetMinimapZoneText() return GetSubZoneText() end

-- Frames get the region methods; font strings remember what they show (client.fontStrings).
local Region = {}
function Region:SetPoint(...) table.insert(self.points, { ... }) end
function Region:ClearAllPoints() self.points = {} end
function Region:SetSize(w, h) self.width, self.height = w, h end
function Region:SetWidth(w) self.width = w end
function Region:SetAlpha(a) self.alpha = a end
function Region:GetAlpha() return self.alpha or 1 end
function Region:Show() self.shown = true end
function Region:Hide() self.shown = false end
function Region:IsShown() return self.shown ~= false end
function Region:SetFrameStrata(strata) self.strata = strata end

local FontString = CreateFromMixins(Region)
function FontString:SetText(text) self.text = text end
function FontString:GetText() return self.text end
function FontString:SetTextColor(r, g, b) self.color = { r, g, b } end
function FontString:SetJustifyH(justify) self.justify = justify end
function FontString:SetMaxLines(n) self.maxLines = n end

client.fontStrings = {}
function Region:CreateFontString(_, _, template)
	local fs = CreateFromMixins(FontString)
	fs.template, fs.parent, fs.points = template, self, {}
	table.insert(client.fontStrings, fs)
	return fs
end

local BaseCreateFrame = CreateFrame
function CreateFrame(...)
	local frame = BaseCreateFrame(...)
	frame.points = frame.points or {}
	for name, method in pairs(Region) do
		if frame[name] == nil then frame[name] = method end
	end
	return frame
end

UIParent = CreateFromMixins(Region)
UIParent.points = {}

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
