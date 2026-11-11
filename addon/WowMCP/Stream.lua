-- WowMCP stream: sends data to the wow-mcp server without drawing anything.
--
-- Every PlaySoundFile() call on an empty file makes the client open that file on disk,
-- and the server sees each open, in order, through inotify (wow_mcp/stream.py). So we
-- "play" out/000.wav-out/fff.wav in the order that spells the message, 12 bits per file.
-- The files are empty, so nothing is ever heard.
--
-- Messages, with body = zlib(JSON):
--   reply: "WM\1", request id (4 bytes), body length (4 bytes), body
--   post:  "WM\2", session token (4 bytes), post id (4 bytes), body length (4 bytes), body
-- A post is sent unasked (e.g. a /ai question). Post ids count up from 1 in each UI session,
-- and the token tells them apart from the posts before a /reload.
-- A message is read as a big-endian bit stream cut into 12-bit symbols. Symbol k is sent as
-- file (symbol + Whitening(k)) % 4096, so runs of equal symbols (e.g. the zero bytes in the
-- header) become different files. Messages go out one after another, never interleaved.
--
-- The client opens a given file at most once per frame (a repeat in the same frame never
-- reaches the disk), so a file isn't reused in the current or previous frame; when the
-- next one would be, sending pauses until a later frame. Each open costs roughly 0.1 ms,
-- so sending is also spread over frames with a small time budget per frame.

local ADDON, ns = ...

local Stream = {}
ns.Stream = Stream

local ROOT = "Interface\\AddOns\\" .. ADDON .. "\\out\\"
local BUDGET_MS = 1.5
local MAX_PER_FRAME = 100
local MAGIC, POST_MAGIC = "WM\1", "WM\2"

local paths = setmetatable({}, {
	__index = function(t, symbol)
		local path = string.format("%s%03x.wav", ROOT, symbol)
		t[symbol] = path
		return path
	end,
})

local queue = {}  -- messages (file lists) waiting their turn
local current     -- { files, position } being sent
local sender = CreateFrame("Frame")

-- Same sequence as stream.py: an LCG mod 4096 with full period.
local function Whitening(count)
	local values, r = {}, 1
	for k = 1, count do
		values[k] = r
		r = (1597 * r + 51) % 4096
	end
	return values
end

local function U32(n)
	return string.char(math.floor(n / 16777216) % 256, math.floor(n / 65536) % 256, math.floor(n / 256) % 256, n % 256)
end

-- 3 bytes -> 2 symbols (a short tail is padded with zero bits), then whitened.
local function Files(bytes)
	local symbols, n = {}, #bytes
	for i = 1, n, 3 do
		local b0, b1, b2 = bytes:byte(i, i + 2)
		b1, b2 = b1 or 0, b2 or 0
		symbols[#symbols + 1] = b0 * 16 + math.floor(b1 / 16)
		if i + 1 <= n then symbols[#symbols + 1] = (b1 % 16) * 256 + b2 end
	end
	local whitening = Whitening(#symbols)
	for k = 1, #symbols do symbols[k] = (symbols[k] + whitening[k]) % 4096 end
	return symbols
end

local frame = 0
local lastFrame = {} -- file -> frame it was last opened in

local function SendSome(self)
	frame = frame + 1
	local started, count = debugprofilestop(), 0
	while current and count < MAX_PER_FRAME and debugprofilestop() - started < BUDGET_MS do
		local file = current.files[current.position]
		if (lastFrame[file] or -1) >= frame - 1 then break end
		PlaySoundFile(paths[file], "Master")
		lastFrame[file] = frame
		current.position = current.position + 1
		count = count + 1
		if current.position > #current.files then
			current = table.remove(queue, 1)
			if current then current = { files = current, position = 1 } end
		end
	end
	if not current then self:SetScript("OnUpdate", nil) end
end

local function Body(value)
	local json = C_EncodingUtil.SerializeJSON(value, { ignoreSerializationErrors = true })
	return C_EncodingUtil.CompressString(json, Enum.CompressionMethod.Zlib, Enum.CompressionLevel.OptimizeForSize)
end

local function Enqueue(message)
	local files = Files(message)
	if current then
		table.insert(queue, files)
	else
		current = { files = files, position = 1 }
		sender:SetScript("OnUpdate", SendSome)
	end
end

-- Send a Lua value (tables, strings, numbers, booleans) to the server as reply `id`.
function Stream.Send(id, value)
	local body = Body(value)
	Enqueue(MAGIC .. U32(id) .. U32(#body) .. body)
end

local session, posts -- set by the first post

-- Send a Lua value to the server unasked (keep it small); returns its post id and the session token,
-- which together name it (the server's replies say which post they're about by them).
function Stream.Post(value)
	if not session then
		session, posts = (math.floor(time()) % 65536) * 65536 + math.random(0, 65535), 0
	end
	posts = posts + 1
	local body = Body(value)
	Enqueue(POST_MAGIC .. U32(session) .. U32(posts) .. U32(#body) .. body)
	return posts, session
end
