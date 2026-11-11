-- WowMCP status: addon build, live channel and slots left (request kind STATUS), so the
-- server can tell whether the game runs the installed code and whether updates get through.

local _, ns = ...

-- "Interface action failed because of an AddOn" doesn't say which addon or what it was doing. The
-- game raises these events synchronously, inside the blocked call, so the stack here shows the code
-- path that led to it. The last few are kept (any addon's) for get_status.
local BLOCKED_KEPT, STACK_LENGTH = 10, 1200
local blocked = CreateFrame("Frame")
for _, event in ipairs({ "ADDON_ACTION_BLOCKED", "ADDON_ACTION_FORBIDDEN", "MACRO_ACTION_BLOCKED", "MACRO_ACTION_FORBIDDEN" }) do
	pcall(blocked.RegisterEvent, blocked, event)
end
blocked:SetScript("OnEvent", function(_, event, addon, func)
	if type(WowMCPDB) ~= "table" then return end
	local list = type(WowMCPDB.blocked) == "table" and WowMCPDB.blocked or {}
	WowMCPDB.blocked = list
	local ok, stack = pcall(debugstack, 2, 20, 0)
	table.insert(list, 1, {
		at = time(), event = event, addon = tostring(addon), func = tostring(func),
		stack = ok and type(stack) == "string" and stack:sub(1, STACK_LENGTH) or nil,
	})
	for i = #list, BLOCKED_KEPT + 1, -1 do list[i] = nil end
end)

local function Status()
	local run = ns.run
	return {
		build = ns.BUILD,               -- nil if Build.lua wasn't loaded (new file: restart WoW)
		live = run.live,
		liveProblem = run.liveProblem,
		slotsLeft = ns.SlotsLeft(),
		slotCount = ns.SLOT_COUNT,
		slotProblem = run.slotProblem,  -- LoadAddOn's reason (e.g. "MISSING", "DISABLED") or "EXHAUSTED"
		pendingLoad = run.pendingLoad,  -- an update is waiting for combat to end
		seq = run.seq,
		req = run.req,
		blocked = WowMCPDB and WowMCPDB.blocked,  -- newest first
	}
end

ns.RegisterSection(ns.Kinds.STATUS, "status", Status)

---------------------------------------------------------------------------
-- /mcp probe: research for a server-to-game channel without per-launch limits (see
-- server/probes/cache_probe.py, which prepares probe/ and changes its files when go.wav is opened).
-- Does the game remember a file per path as written, or per file? s1 and s2 are valid sounds;
-- after go.wav, s1 is empty. A spelling that reads afresh finds s1 silent; s2 shows the spelling
-- reaches the file at all. The font and texture get new versions too: do other spellings load them?
---------------------------------------------------------------------------

do
	local DIR = "Interface\\AddOns\\WowMCP\\probe\\"
	local GLYPHS = { "A", "B", "K", "L", "M" }
	local SPELLINGS = {
		{ "as written", function(p) return p end },
		{ "lower case", string.lower },
		{ "upper case", string.upper },
		{ "forward slashes", function(p) return (p:gsub("\\", "/")) end },
		{ "with .\\", function(p) return (p:gsub("\\([^\\]+)$", "\\.\\%1")) end },
		{ "with ..\\probe\\", function(p) return (p:gsub("\\probe\\", "\\probe\\..\\probe\\")) end },
		{ "doubled \\", function(p) return (p:gsub("^Interface\\", "Interface\\\\")) end },
		{ "one capital", function(p) return (p:gsub("\\(%l)([^\\]*)$", function(a, b) return "\\" .. a:upper() .. b end)) end },
	}

	local function Play(path)
		local ok, willPlay, handle = pcall(PlaySoundFile, path, "Master")
		return ok and willPlay and handle or nil
	end

	local function Playing(handle)
		if not handle then return "no handle" end
		local playing = C_Sound.IsPlaying(handle)
		StopSound(handle)
		return playing and "PLAYS" or "silent"
	end

	local text = UIParent:CreateFontString(nil, "ARTWORK")
	text:SetAlpha(0)
	local tex = UIParent:CreateTexture(nil, "ARTWORK")
	tex:SetAlpha(0)

	-- The font's glyph widths: which version was loaded, and how finely the game measures.
	local function Widths(path)
		local ok, loaded = pcall(text.SetFont, text, path, 64, "")
		if not (ok and loaded) then return "didn't load" end
		local widths = {}
		for _, glyph in ipairs(GLYPHS) do
			text:SetText(glyph)
			widths[#widths + 1] = string.format("%s=%.3f", glyph, text:GetUnboundedStringWidth())
		end
		return table.concat(widths, " ")
	end

	local function Texture(path)
		local ok = pcall(tex.SetTexture, tex, path)
		if not ok then return "didn't load" end
		local okSize, w, h = pcall(tex.GetSize, tex)
		text:SetFontObject(GameFontNormal)
		text:SetText("|T" .. path .. ":32|t")
		return string.format("size %s, in text %.1f wide", okSize and string.format("%.0fx%.0f", w or 0, h or 0) or "?",
			text:GetUnboundedStringWidth())
	end

	-- A step that fails says so: an error in a timer would otherwise go unseen.
	local function Safely(fn)
		return function()
			local ok, err = pcall(fn)
			if not ok then ns.Print("Probe failed: " .. tostring(err)) end
		end
	end

	local function Run()
		local results = { at = time(), sounds = {}, fonts = {}, textures = {} }
		WowMCPDB.probe = results
		ns.Print("Probe: playing and loading the probe files…")
		local first = { s1 = Play(DIR .. "s1.wav"), s2 = Play(DIR .. "s2.wav") }
		results.fontBefore = Widths(DIR .. "f.ttf")
		results.textureBefore = Texture(DIR .. "t.tga")
		C_Timer.After(0.6, Safely(function()
			results.before = { s1 = Playing(first.s1), s2 = Playing(first.s2) }
			Play(DIR .. "go.wav") -- the helper sees this open and changes the files
			C_Timer.After(1.5, Safely(function()
				local handles = {}
				for i, s in ipairs(SPELLINGS) do
					handles[i] = { s1 = Play(s[2](DIR .. "s1.wav")), s2 = Play(s[2](DIR .. "s2.wav")) }
					results.fonts[i] = { s[1], Widths(s[2](DIR .. "f.ttf")) }
					results.textures[i] = { s[1], Texture(s[2](DIR .. "t.tga")) }
				end
				C_Timer.After(0.6, Safely(function()
					ns.Print(string.format("Before the change: s1 %s, s2 %s (both should play)", results.before.s1, results.before.s2))
					for i, s in ipairs(SPELLINGS) do
						results.sounds[i] = { s[1], Playing(handles[i].s1), Playing(handles[i].s2) }
						ns.Print(string.format("%s: s1 (emptied) %s, s2 %s", s[1], results.sounds[i][2], results.sounds[i][3]))
					end
					ns.Print("Font before: " .. results.fontBefore)
					for _, f in ipairs(results.fonts) do ns.Print("Font " .. f[1] .. ": " .. f[2]) end
					ns.Print("Texture before: " .. results.textureBefore)
					for _, t in ipairs(results.textures) do ns.Print("Texture " .. t[1] .. ": " .. t[2]) end
					ns.Print("Probe done. Saved in WowMCPDB.probe; /reload writes it to disk.")
				end))
			end))
		end))
	end

	ns.RegisterCommand("probe", "/mcp probe - research: how the game remembers files (needs the server's probe helper)", function()
		local ok, err = pcall(Run)
		if not ok then ns.Print("Probe failed: " .. tostring(err)) end
	end)
end
