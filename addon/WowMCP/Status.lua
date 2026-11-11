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
-- /mcp probe <name>: research into other channels between the server and the game. Each probe has
-- a server half in server/probes/channel_probes.py (see there); results print in chat and are kept
-- in WowMCPDB.probes[name] (a /reload writes them to disk).
--   files  does the game remember a file per path as written? (fewer files, no per-launch limit)
--   pad    a virtual gamepad's spare axes as numbers (no files at all)
--   audio  short quiet tones the server hears in WoW's own audio stream (no file opens)
---------------------------------------------------------------------------

do
	local DIR = "Interface\\AddOns\\WowMCP\\probe\\"

	-- A step that fails says so: an error in a timer would otherwise go unseen.
	local function Safely(fn)
		return function(...)
			local ok, err = pcall(fn, ...)
			if not ok then ns.Print("Probe failed: " .. tostring(err)) end
		end
	end

	local function Keep(name, results)
		WowMCPDB.probes = type(WowMCPDB.probes) == "table" and WowMCPDB.probes or {}
		results.at = time()
		WowMCPDB.probes[name] = results
		return results
	end

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

	-- files ----------------------------------------------------------------------------------------
	-- s1 and s2 are valid sounds; after go.wav, s1 is empty. A spelling that reads afresh finds s1
	-- silent; s2 shows the spelling reaches the file at all. The font and texture get new versions too.

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

	-- Made when a probe first runs: nothing of the probes exists otherwise.
	local text, tex
	local function Measuring()
		if not text then
			text = UIParent:CreateFontString(nil, "ARTWORK")
			text:SetAlpha(0)
			tex = UIParent:CreateTexture(nil, "ARTWORK")
			tex:SetAlpha(0)
		end
	end

	-- The font's glyph widths: which version was loaded, and how finely the game measures. Whatever
	-- SetFont says, and the font the string ends up with, are part of the result.
	local function Widths(path)
		local ok, loaded = pcall(text.SetFont, text, path, 32, "")
		local okGet, file = pcall(text.GetFont, text)
		local widths = {}
		for _, glyph in ipairs(GLYPHS) do
			text:SetText(glyph)
			widths[#widths + 1] = string.format("%s=%.3f", glyph, text:GetUnboundedStringWidth())
		end
		return string.format("%s; font %s; %s", ok and ("SetFont " .. tostring(loaded)) or ("error " .. tostring(loaded)),
			okGet and tostring(file) or "?", table.concat(widths, " "))
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

	local function Files()
		Measuring()
		local results = Keep("files", { sounds = {}, fonts = {}, textures = {} })
		ns.Print("Probe files: playing and loading the probe files…")
		local first = { s1 = Play(DIR .. "s1.wav"), s2 = Play(DIR .. "s2.wav") }
		results.fontControl = Widths("Fonts\\FRIZQT__.TTF") -- one of the game's own: does SetFont work at all?
		results.fontBefore = Widths(DIR .. "f.ttf")
		results.textureBefore = Texture(DIR .. "t.tga")
		C_Timer.After(0.6, Safely(function()
			results.before = { s1 = Playing(first.s1), s2 = Playing(first.s2) }
			Play(DIR .. "go.wav") -- the server half sees this open and changes the files
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
					ns.Print("Font control (the game's own): " .. results.fontControl)
					ns.Print("Font before: " .. results.fontBefore)
					for _, f in ipairs(results.fonts) do ns.Print("Font " .. f[1] .. ": " .. f[2]) end
					ns.Print("Texture before: " .. results.textureBefore)
					for _, t in ipairs(results.textures) do ns.Print("Texture " .. t[1] .. ": " .. t[2]) end
					ns.Print("Probe done. /reload writes the results to disk.")
				end))
			end))
		end))
	end

	-- pad -------------------------------------------------------------------------------------------
	-- Every controller the game sees, and the distinct values each raw axis took while the server half
	-- stepped one. Gamepad support is on only for the probe.

	local PAD_SECONDS = 15
	local watcher

	local function Pad()
		if not (C_GamePad and C_GamePad.GetAllDeviceIDs and C_CVar) then
			ns.Print("Probe pad: this client has no gamepad functions.")
			return
		end
		local results = Keep("pad", { devices = {} })
		local was = C_CVar.GetCVar("GamePadEnable")
		if was ~= "1" then C_CVar.SetCVar("GamePadEnable", "1") end
		ns.Print(string.format("Probe pad: gamepad support on for %d seconds (was %s). Stand still somewhere safe…",
			PAD_SECONDS, tostring(was)))
		local seen, stop = {}, GetTime() + PAD_SECONDS
		watcher = watcher or CreateFrame("Frame")
		watcher:SetScript("OnUpdate", Safely(function()
			for _, id in ipairs(C_GamePad.GetAllDeviceIDs() or {}) do
				local ok, raw = pcall(C_GamePad.GetDeviceRawState, id)
				if ok and raw then
					local d = seen[id] or { id = id, name = raw.name, vendor = raw.vendorID, product = raw.productID, axes = {} }
					seen[id] = d
					for i, v in ipairs(raw.rawAxes or {}) do
						local a = d.axes[i] or { values = {}, n = 0 }
						d.axes[i] = a
						local key = string.format("%.6f", v)
						if not a.values[key] then a.values[key], a.n = true, a.n + 1 end
					end
				end
			end
			if GetTime() < stop then return end
			watcher:SetScript("OnUpdate", nil)
			if was ~= "1" then C_CVar.SetCVar("GamePadEnable", was or "0") end
			local okActive, active = pcall(C_GamePad.GetActiveDeviceID)
			results.active = okActive and active or nil
			local any = false
			for _, d in pairs(seen) do
				any = true
				local moving = {}
				for i, a in pairs(d.axes) do
					if a.n > 1 then
						local values = {}
						for v in pairs(a.values) do values[#values + 1] = tonumber(v) end
						table.sort(values)
						a.list = values
						local shown = {}
						for k = 1, math.min(#values, 8) do shown[k] = string.format("%.5f", values[k]) end
						moving[#moving + 1] = string.format("axis %d: %d values (%s%s)", i, #values, table.concat(shown, ", "),
							#values > 8 and ", …" or "")
					end
					a.values = nil
				end
				table.insert(results.devices, d)
				ns.Print(string.format("Device %s: %s (vendor %s, product %s), %d axes", tostring(d.id), tostring(d.name),
					tostring(d.vendor), tostring(d.product), #d.axes))
				for _, line in ipairs(moving) do ns.Print("  " .. line) end
				if #moving == 0 then ns.Print("  no axis moved") end
			end
			if not any then ns.Print("No controllers seen.") end
			ns.Print(string.format("Active device: %s. Gamepad support back to %s. Probe done.", tostring(results.active), tostring(was)))
		end))
	end

	-- audio -----------------------------------------------------------------------------------------
	-- A known sequence of short tones; the server half says which it heard, and when.

	local SEQUENCE = { "a", "b", "a", "a", "b", "c", "b", "a" }

	local function Audio()
		local results = Keep("audio", { played = {} })
		ns.Print("Probe audio: playing 8 short tones (two very high, one quiet beep)…")
		local start = GetTime()
		for i, t in ipairs(SEQUENCE) do
			C_Timer.After(0.4 * i, Safely(function()
				local handle = Play(DIR .. "tone_" .. t .. ".wav")
				results.played[i] = { t, GetTime() - start, handle and true or false }
				if i == #SEQUENCE then
					local list = {}
					for _, p in ipairs(results.played) do list[#list + 1] = string.format("%s %.2fs%s", p[1], p[2], p[3] and "" or " (no handle)") end
					ns.Print("Played: " .. table.concat(list, ", ") .. ". Probe done.")
				end
			end))
		end
	end

	local PROBES = { files = Files, pad = Pad, audio = Audio }
	ns.RegisterCommand("probe", "/mcp probe [files, pad, audio] - research into other channels (needs the server's probe helper)",
		function(rest)
			local probe = PROBES[strtrim(tostring(rest or "")):lower()]
			if not probe then
				ns.Print("/mcp probe files, /mcp probe pad or /mcp probe audio (each needs its server half running)")
				return
			end
			Safely(probe)()
		end)
end
