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
		verified = run.verified,        -- seen the bell's files change on disk (Core.lua's handshake)
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
--   audio  short quiet tones the server hears in WoW's own audio stream (no file opens)
--   stream long sounds read afresh on every play? (one file, no per-launch limit)
--   font   a real font whose letter widths the server rewrites (bigger data)
--   speed  what a streamed play costs, and how precisely its length can be timed
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

	-- stream ----------------------------------------------------------------------------------------
	-- Long or compressed sounds may be streamed from disk on every play rather than read once and
	-- remembered. Each is played as prepared (60 s of silence), after the server empties it, and after
	-- it puts it back 3 s long: silent the second time and about 3 s the third means the game read the
	-- file again each time, so one file could signal without limit.

	local STREAMS = { "big.wav", "big.ogg", "big.mp3", "small.ogg" }
	local timer

	local function Stream()
		local results = Keep("stream", { files = {} })
		for _, f in ipairs(STREAMS) do results.files[f] = {} end
		ns.Print("Probe stream: playing four long silent sounds three times while the server changes them (about 15 s)…")
		local handles = {}
		local function PlayAll()
			for i, f in ipairs(STREAMS) do handles[i] = Play(DIR .. f) end
		end
		local function CheckAll(phase)
			for i, f in ipairs(STREAMS) do results.files[f][phase] = Playing(handles[i]) end
		end
		PlayAll()
		C_Timer.After(1, Safely(function()
			CheckAll("prepared")
			Play(DIR .. "go2.wav") -- the server empties them
			C_Timer.After(1.5, Safely(function()
				PlayAll()
				C_Timer.After(1, Safely(function()
					CheckAll("emptied")
					Play(DIR .. "go3.wav") -- the server puts them back, 3 s long
					C_Timer.After(1.5, Safely(function()
						PlayAll()
						local started, lasted = GetTime(), {}
						timer = timer or CreateFrame("Frame")
						timer:SetScript("OnUpdate", Safely(function()
							local now = GetTime() - started
							for i, f in ipairs(STREAMS) do
								if not lasted[f] and not (handles[i] and C_Sound.IsPlaying(handles[i])) then
									lasted[f] = handles[i] and string.format("%.1f s", now) or "no handle"
								end
							end
							if now < 8 then return end
							timer:SetScript("OnUpdate", nil)
							for i, f in ipairs(STREAMS) do
								if handles[i] then StopSound(handles[i]) end
								results.files[f].restored = lasted[f] or "over 8 s"
								local r = results.files[f]
								ns.Print(string.format("%s: prepared %s, emptied %s, back as 3 s: played %s", f, r.prepared,
									r.emptied, r.restored))
							end
							ns.Print("Probe done. /reload writes the results to disk.")
						end))
					end))
				end))
			end))
		end))
	end

	-- font ------------------------------------------------------------------------------------------
	-- A real font's letters, before and after the server gives them other widths: does SetFont take
	-- it, and does setting it again (or on a new string) read the new file?

	local function Font()
		Measuring()
		local results = Keep("font", {})
		results.control = Widths("Fonts\\FRIZQT__.TTF")
		results.first = Widths(DIR .. "real.ttf")
		ns.Print("Probe font: " .. results.first)
		Play(DIR .. "go4.wav") -- the server rewrites real.ttf with other widths
		C_Timer.After(1.5, Safely(function()
			Widths("Fonts\\FRIZQT__.TTF") -- away and back, so the font is set anew
			results.again = Widths(DIR .. "real.ttf")
			local saved = text
			text = UIParent:CreateFontString(nil, "ARTWORK") -- a new string, in case each keeps its own copy
			text:SetAlpha(0)
			results.newString = Widths(DIR .. "real.ttf")
			text = saved
			ns.Print("Control: " .. results.control)
			ns.Print("After the change, set again: " .. results.again)
			ns.Print("After the change, a new string: " .. results.newString)
			ns.Print("Probe done. /reload writes the results to disk.")
		end))
	end

	-- speed -----------------------------------------------------------------------------------------
	-- How far streamed sounds stretch as a channel: what one play costs (against a cached sound), and
	-- how precisely the game times a streamed sound the server rewrote to a given length.

	local SPEED_FILES = { "big.wav", "big.ogg", "big.mp3" }
	local SPEED_STEPS = 14 -- channel_probes.py's SPEED_LENGTHS
	local clock = GetTimePreciseSec or GetTime

	local function Cost(path, times)
		local total = 0
		for _ = 1, times do
			local start = debugprofilestop()
			local handle = Play(path)
			total = total + (debugprofilestop() - start)
			if handle then StopSound(handle) end
		end
		return total / times -- milliseconds per play
	end

	local function Speed()
		local results = Keep("speed", { cost = {}, steps = {} })
		for _, f in ipairs(SPEED_FILES) do results.cost[f] = Cost(DIR .. f, 10) end
		results.cost["s2.wav (cached)"] = Cost(DIR .. "s2.wav", 10)
		local parts = {}
		for f, ms in pairs(results.cost) do parts[#parts + 1] = string.format("%s %.3f ms", f, ms) end
		table.sort(parts)
		ns.Print("Probe speed: one play costs " .. table.concat(parts, ", "))
		ns.Print("Timing " .. SPEED_STEPS .. " lengths (about 25 s)…")
		local step = 0
		local Next
		Next = Safely(function()
			step = step + 1
			if step > SPEED_STEPS then
				for i, row in ipairs(results.steps) do ns.Print(string.format("step %d: %s", i, table.concat(row, ", "))) end
				ns.Print("Probe done. /reload writes the results to disk.")
				return
			end
			Play(DIR .. "go2.wav") -- the server rewrites the files to the next length
			C_Timer.After(0.3, Safely(function()
				local handles, started, lasted = {}, {}, {}
				for i, f in ipairs(SPEED_FILES) do
					started[i] = clock()
					handles[i] = Play(DIR .. f)
				end
				timer = timer or CreateFrame("Frame")
				timer:SetScript("OnUpdate", Safely(function()
					local done = true
					for i in ipairs(SPEED_FILES) do
						if not lasted[i] then
							if not handles[i] then
								lasted[i] = "no handle"
							elseif not C_Sound.IsPlaying(handles[i]) then
								lasted[i] = string.format("%.0f ms", (clock() - started[i]) * 1000)
							else
								done = false
							end
						end
					end
					if not done and clock() - started[1] < 3 then return end
					timer:SetScript("OnUpdate", nil)
					local row = {}
					for i, f in ipairs(SPEED_FILES) do
						if not lasted[i] then
							StopSound(handles[i])
							lasted[i] = "over 3 s"
						end
						row[i] = f .. " " .. lasted[i]
					end
					results.steps[step] = row
					C_Timer.After(0.2, Next)
				end))
			end))
		end)
		Next()
	end

	local PROBES = { files = Files, audio = Audio, stream = Stream, font = Font, speed = Speed }
	ns.RegisterCommand("probe", "/mcp probe [files, audio, stream, font, speed] - research into other channels (needs the server's probe helper)",
		function(rest)
			local probe = PROBES[strtrim(tostring(rest or "")):lower()]
			if not probe then
				ns.Print("/mcp probe files, audio, stream, font or speed (each needs its server half running)")
				return
			end
			Safely(probe)()
		end)
end
