-- WowMCP's commands while typing them: a list over the chat box of the addon's /mcp and /ai
-- commands, each with what it does, that narrows down as the player types. "/" alone lists them
-- all; "/mcp r" the /mcp commands that start with r; "/mcp route " what /mcp route takes. Anything
-- else (/s, /w, /cast, a plain line) hides it. /mcp help prints the same list in the chat.
--
-- Under them, other addons' commands that start with what's typed (Prat's /clear, Questie's
-- /questie, ...), by name and addon only: an addon doesn't say what its commands do. Read from the
-- game's list of slash commands, so no addon is needed; Blizzard's own commands are left out.
--
-- Like the AI tab's typing (Chat.lua), it leaves Blizzard's chat box alone: no hooks on its
-- methods or scripts. It hears when a box gets focus and loses it (EventRegistry's chat box events)
-- and meanwhile reads the box's text once a frame, from a frame of its own. The list is a frame of
-- its own too, only anchored over the box.

local _, ns = ...

local SlashHelp = {}
ns.SlashHelp = SlashHelp

local COMMAND = "|cffffd200" -- the command, in the gold of the game's own help
local NOTE = "|cffaaaaaa"
local TITLE = "|cff8fc8ffWowMCP|r"
local MAX_NOTES = 8              -- more matches than this: the commands only, a line each
local MAX_OTHERS = 10            -- other addons' commands listed at most
local OURS = { WOWMCP = true, WOWMCPAI = true }
local WIDTH, PAD, GAP = 400, 6, 4 -- px: when the box can't say how wide it is; padding; space over the box

-- Each command name and the names that also run it.
local SLASHES = { ["/mcp"] = { "/mcp", "/wowmcp" }, ["/ai"] = { "/ai", "/ask" } }
local ORDER = { "/ai", "/mcp" }

-- The commands as entries: { slash = "/mcp", sub = "route" (or ""), arg = "<n>" (if the first word is
-- a placeholder), head = "/mcp route [show, ...]", note = "the route and its navigator" }.
local function Entry(usage)
	local head, note = usage:match("^(.-) %- (.*)$")
	head = head or usage
	local slash, first = head:match("^(/%S+)%s*(%S*)")
	local entry = { slash = slash and slash:lower(), sub = "", head = head, note = note or "" }
	if first:match("^[<%[]") then entry.arg = first else entry.sub = first:lower() end
	return entry
end

function SlashHelp.Entries()
	local entries = {}
	for _, usage in ipairs(ns.AIChat and ns.AIChat.Usages() or {}) do entries[#entries + 1] = Entry(usage) end
	for _, usage in ipairs(ns.CommandUsages()) do entries[#entries + 1] = Entry(usage) end
	return entries
end

-- The command name (/mcp, /ai) a typed one is, or starts; nil if neither. A start of an alias counts
-- from three characters, so /w (whisper) isn't taken for /wowmcp.
local function Slashes(typed)
	local found = {}
	for _, slash in ipairs(ORDER) do
		for i, name in ipairs(SLASHES[slash]) do
			if name:sub(1, #typed) == typed and (i == 1 or #typed >= 3) then
				found[#found + 1] = slash
				break
			end
		end
	end
	return found
end

local function Whole(typed)
	for slash, names in pairs(SLASHES) do
		for _, name in ipairs(names) do
			if name == typed then return slash end
		end
	end
end

-- The entries to show for the text in the chat box, or nil.
function SlashHelp.Matches(text, entries)
	if type(text) ~= "string" then return nil end
	text = text:lower():match("^%s*(.-)$")
	if text:sub(1, 1) ~= "/" then return nil end
	local typed, rest = text:match("^(/%S*)(.*)$")
	local found = {}
	if rest == "" then -- still typing the command name
		local slashes = Slashes(typed)
		for _, slash in ipairs(slashes) do
			for _, entry in ipairs(entries) do
				if entry.slash == slash then found[#found + 1] = entry end
			end
		end
		return #found > 0 and found or nil
	end
	local slash = Whole(typed)
	if not slash then return nil end
	local word, after = rest:match("^%s+(%S*)(.*)$")
	local numeric = word:match("^%d+$") ~= nil
	local function Takes(entry) -- a placeholder first word: <n> takes a number, <question> anything
		if not entry.arg then return false end
		if entry.arg == "<n>" then return numeric or word == "" end
		return true
	end
	local mine = {}
	for _, entry in ipairs(entries) do
		if entry.slash == slash then mine[#mine + 1] = entry end
	end
	if after == "" then -- still typing the first word: the commands it starts
		for _, entry in ipairs(mine) do
			if entry.sub ~= "" and entry.sub:sub(1, #word) == word or entry.sub == "" and word == "" then
				found[#found + 1] = entry
			end
		end
	else -- past it: that command, with what it takes
		for _, entry in ipairs(mine) do
			if entry.sub ~= "" and entry.sub == word then found[#found + 1] = entry end
		end
	end
	if #found == 0 or word == "" then
		for _, entry in ipairs(mine) do
			if entry.sub == "" and entry.arg and Takes(entry) then
				local listed = false
				for _, f in ipairs(found) do listed = listed or f == entry end
				if not listed then found[#found + 1] = entry end
			end
		end
	end
	return #found > 0 and found or nil
end

-- Other addons' commands: { names = { "/clear", "/cls" }, addon = "Prat-3.0" }, by first name. Blizzard's
-- own are secure; one an addon added says which (issecurevariable). A client without it: all of them.
function SlashHelp.Others()
	local others = {}
	for key in pairs(SlashCmdList) do
		local secure, addon = true, nil
		if issecurevariable then secure, addon = issecurevariable(SlashCmdList, key) else secure = false end
		if not secure and not OURS[key] then
			local names, i = {}, 1
			while type(_G["SLASH_" .. key .. i]) == "string" do
				local name = _G["SLASH_" .. key .. i]:lower()
				local seen = false
				for _, n in ipairs(names) do seen = seen or n == name end
				if not seen then names[#names + 1] = name end
				i = i + 1
			end
			if #names > 0 then others[#others + 1] = { names = names, addon = addon } end
		end
	end
	table.sort(others, function(a, b) return a.names[1] < b.names[1] end)
	return others
end

-- The other addons' commands a command name being typed starts (from two characters: "/" alone
-- would be all of them).
function SlashHelp.OtherMatches(text, others)
	if type(text) ~= "string" then return {} end
	local typed = text:lower():match("^%s*(/%S+)$")
	local found = {}
	if not typed then return found end
	for _, other in ipairs(others) do
		for _, name in ipairs(other.names) do
			if name:sub(1, #typed) == typed then
				found[#found + 1] = other
				break
			end
		end
	end
	return found
end

-- The list's text: with what each does while there are few, the commands only while there are many;
-- then the other addons' commands that match (more, while only "/" is typed).
function SlashHelp.Text(found, others, more)
	found = found or {}
	local lines = { TITLE }
	local notes = #found <= MAX_NOTES
	for _, entry in ipairs(found) do
		-- Many: the command alone ("/mcp plan"), without the long list of what it takes.
		local head = notes and entry.head or entry.slash .. (entry.sub ~= "" and " " .. entry.sub or entry.arg and " " .. entry.arg or "")
		local line = COMMAND .. head .. "|r"
		if notes and entry.note ~= "" then line = line .. NOTE .. " - " .. entry.note .. "|r" end
		lines[#lines + 1] = line
	end
	if not notes then lines[#lines + 1] = NOTE .. "Keep typing to see what each does.|r" end
	if others and #others > 0 then
		lines[#lines + 1] = NOTE .. "Other addons:|r"
		for i, other in ipairs(others) do
			if i > MAX_OTHERS then
				lines[#lines + 1] = NOTE .. "... and " .. (#others - MAX_OTHERS) .. " more, keep typing.|r"
				break
			end
			lines[#lines + 1] = COMMAND .. table.concat(other.names, ", ") .. "|r"
				.. (other.addon and NOTE .. " (" .. other.addon .. ")|r" or "")
		end
	elseif more and more > 0 then
		lines[#lines + 1] = NOTE .. "And " .. more .. " commands of other addons: type their first letter.|r"
	end
	return table.concat(lines, "\n")
end

---------------------------------------------------------------------------
-- The list over the chat box
---------------------------------------------------------------------------

local list    -- the frame, made when first needed
local watched -- the chat box with focus
local others  -- other addons' commands, read when a chat box gets focus
local shownFor -- the text the list was made for
local watcher = CreateFrame("Frame")

local function Text(box)
	local text = box:GetText()
	if issecretvalue and issecretvalue(text) then return nil end
	return text
end

local function Make()
	list = CreateFrame("Frame", "WowMCPSlashHelp", UIParent)
	list:SetFrameStrata("FULLSCREEN") -- above the chat box (DIALOG)
	local background = list:CreateTexture(nil, "BACKGROUND")
	background:SetAllPoints()
	background:SetColorTexture(0, 0, 0, 0.8)
	list.text = list:CreateFontString(nil, "OVERLAY", "ChatFontNormal")
	list.text:SetJustifyH("LEFT")
	list.text:SetWordWrap(true)
	list.text:SetPoint("TOPLEFT", list, "TOPLEFT", PAD, -PAD)
	list:Hide()
end

local function Hide()
	shownFor = nil
	if list then list:Hide() end
end

-- Shows the list for the box's text, or hides it.
local function Update(box)
	local text = Text(box)
	if text == shownFor then return end
	local found = SlashHelp.Matches(text, SlashHelp.Entries())
	local theirs = SlashHelp.OtherMatches(text, others or {})
	if not found and #theirs == 0 then return Hide() end
	local more = type(text) == "string" and text:match("^%s*/$") and #(others or {}) or nil
	if not list then Make() end
	local width = box.GetWidth and box:GetWidth() or 0
	if width < 200 then width = WIDTH end
	list.text:SetWidth(width - 2 * PAD)
	list.text:SetText(SlashHelp.Text(found, theirs, more))
	list:SetSize(width, list.text:GetStringHeight() + 2 * PAD)
	list:ClearAllPoints()
	list:SetPoint("BOTTOMLEFT", box, "TOPLEFT", 0, GAP)
	list:Show()
	shownFor = text
end

local function Watch(_, elapsed)
	if not watched then return end
	Update(watched)
end

local function OnFocus(_, box)
	watched = box
	others = SlashHelp.Others()
	shownFor = false -- not any text: update now
	Update(box)
	watcher:SetScript("OnUpdate", Watch)
end

local function OnLeave(_, box)
	if box and watched and box ~= watched then return end
	watched = nil
	watcher:SetScript("OnUpdate", nil)
	Hide()
end

if EventRegistry then
	EventRegistry:RegisterCallback("ChatFrame.OnEditBoxFocusGained", OnFocus, SlashHelp)
	EventRegistry:RegisterCallback("ChatFrame.OnEditBoxFocusLost", OnLeave, SlashHelp)
	EventRegistry:RegisterCallback("ChatFrame.OnEditBoxHide", OnLeave, SlashHelp)
end

-- Whether the list shows, and its text (for tests).
function SlashHelp.Shown()
	return list and list:IsShown() and list.text:GetText() or nil
end

---------------------------------------------------------------------------
-- /mcp help
---------------------------------------------------------------------------

ns.RegisterCommand("help", "/mcp help - every /ai and /mcp command (typing / in the chat box lists them too)", function()
	for _, entry in ipairs(SlashHelp.Entries()) do
		ns.Print(COMMAND .. entry.head .. "|r" .. (entry.note ~= "" and " - " .. entry.note or ""))
	end
end)
