-- Typing in the chat box and the clock (Chat.lua, Stream.lua posts).

-- Runs a line typed into the chat box: like the client, finds the slash command by the
-- SLASH_<key><n> strings of each SlashCmdList entry.
function client.Type(line)
	local command, rest = line:match("^(/%S+)%s*(.-)$")
	for key, handler in pairs(SlashCmdList) do
		local i = 1
		while _G["SLASH_" .. key .. i] do
			if _G["SLASH_" .. key .. i]:upper() == command:upper() then return handler(rest) end
			i = i + 1
		end
	end
	error("unknown command: " .. line)
end
