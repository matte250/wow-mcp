-- C_EncodingUtil: JSON and zlib (stored blocks: valid zlib, just not smaller).

local function JSON(value)
	local kind = type(value)
	if kind == "table" then
		if next(value) == nil or value[1] ~= nil then
			local items = {}
			for _, v in ipairs(value) do items[#items + 1] = JSON(v) end
			return "[" .. table.concat(items, ",") .. "]"
		end
		local items = {}
		for k, v in pairs(value) do items[#items + 1] = JSON(tostring(k)) .. ":" .. JSON(v) end
		return "{" .. table.concat(items, ",") .. "}"
	elseif kind == "string" then
		return '"' .. value:gsub('[%c"\\]', function(c) return string.format("\\u%04x", c:byte()) end) .. '"'
	elseif kind == "number" then
		return value == math.floor(value) and string.format("%d", value) or string.format("%.17g", value)
	end
	return tostring(value)
end

local function Zlib(data)
	local a, b = 1, 0
	for i = 1, #data do
		a = (a + data:byte(i)) % 65521
		b = (b + a) % 65521
	end
	local out = { "\120\1" }
	local pos = 1
	repeat
		local chunk = data:sub(pos, pos + 65534)
		pos = pos + #chunk
		local n = #chunk
		out[#out + 1] = string.char(pos > #data and 1 or 0, n % 256, math.floor(n / 256), (65535 - n) % 256, math.floor((65535 - n) / 256)) .. chunk
	until pos > #data
	out[#out + 1] = string.char(math.floor(b / 256), b % 256, math.floor(a / 256), a % 256)
	return table.concat(out)
end

Enum.CompressionMethod = { Deflate = 0, Zlib = 1, Gzip = 2 }
Enum.CompressionLevel = { Default = 0, OptimizeForSpeed = 1, OptimizeForSize = 2 }
C_EncodingUtil = {
	SerializeJSON = function(value) return JSON(value) end,
	CompressString = function(data, method) assert(method == Enum.CompressionMethod.Zlib) return Zlib(data) end,
}

