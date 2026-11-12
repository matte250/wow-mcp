-- Sound files: the bell and the stream (Core.lua, Stream.lua), as the Forever client treats them (measured live,
-- server/probes/channel_probes.py):
--   * It only plays files it found when it started; at startup it reads every file in the AddOns folder
--     (client.Launch, which a test calls again to restart the client).
--   * A long sound (STREAMED seconds or more) is streamed: read from disk on every play. A short valid one is read
--     once and kept for the rest of the launch, whatever happens to the file. An empty or broken one gives a
--     handle but never plays, and is read again next time. Paths are case- and slash-blind.
--   * A sound plays for its length: IsPlaying says so until then, or until StopSound.
--   * A file played twice in one frame is only read from disk once.
-- Every read from disk is in client.reads ({ at, path from the AddOns folder, scan = at startup }): what the
-- server sees of the game reading (inotify).

C_CVar = { GetCVarBool = function(name) return name == "Sound_EnableAllSound" and client.sound end }

local STREAMED = 2.5 -- seconds: the probes had 3 s streamed and 2 s kept

client.reads, client.kept, client.playing = {}, {}, {}
local known = {}     -- the files found at startup, by path as the client compares them
local nextHandle = 0

local function Key(path)
	return (path:lower():gsub("/", "\\"))
end

-- A sound's length in seconds, read from its file; nil if it doesn't play (empty, not a wav).
local function Read(path)
	local rel = path:gsub("^Interface[\\/]AddOns[\\/]", ""):gsub("\\", "/")
	table.insert(client.reads, { at = client.now, path = rel })
	local f = io.open(client.addonsDir .. "/" .. rel, "rb")
	if not f then return nil end
	local header = f:read(44)
	f:close()
	if not header or #header < 44 or header:sub(1, 4) ~= "RIFF" or header:sub(9, 12) ~= "WAVE" then return nil end
	local rate, size = string.unpack("<I4", header, 29), string.unpack("<I4", header, 41)
	return rate > 0 and size / rate or nil
end

-- The client starts: what it has kept is gone, and it finds (and reads) every file there is.
function client.Launch()
	known, client.kept = {}, {}
	local files = io.popen('cd "' .. client.addonsDir .. '" && find . -type f')
	for line in files:lines() do
		local rel = line:gsub("^%./", "")
		known[Key("Interface\\AddOns\\" .. rel)] = true
		table.insert(client.reads, { at = client.now, path = rel, scan = true })
	end
	files:close()
end

function PlaySoundFile(path)
	local key = Key(path)
	if not known[key] then return false end
	local length = client.kept[key] or client.openedThisFrame[key]
	if length == nil then
		length = Read(path) or false
		client.openedThisFrame[key] = length
		if length and length < STREAMED then client.kept[key] = length end
	end
	nextHandle = nextHandle + 1
	client.playing[nextHandle] = length and client.now + length or false
	return true, nextHandle
end
function StopSound(handle) client.playing[handle] = false end
C_Sound = {
	IsPlaying = function(handle)
		local ends = client.playing[handle]
		return ends ~= nil and ends ~= false and client.now < ends
	end,
}

