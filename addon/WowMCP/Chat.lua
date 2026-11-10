-- WowMCP chat: /ai <question> (or /ask) asks the AI without leaving the game. The question
-- goes to the wow-mcp-bridge service as a stream post (Stream.lua); the bridge asks an agent
-- that has the wow tools, and the answer comes back as a message (Messages.lua). The bridge
-- remembers the recent questions and answers, so follow-ups work; /ai new starts afresh.

local _, ns = ...

local MAX_QUESTION = 500 -- bytes; the chat box allows 255 anyway
local HINT_AFTER = 90    -- seconds without any update before saying the bridge may be down

-- Links shift-clicked into the question become text the agent can read:
-- "[Linen Cloth] (item 2589)".
local function PlainText(text)
	text = text:gsub("|H(%a+):(%d+)[^|]*|h(.-)|h", "%3 (%1 %2)"):gsub("|H.-|h(.-)|h", "%1")
	text = text:gsub("|c%x%x%x%x%x%x%x%x", ""):gsub("|cn[^:]*:", ""):gsub("|r", ""):gsub("|T.-|t", "")
	return (text:gsub("||", "|"))
end

local function Ask(input)
	local typed = strtrim(input or "")
	if typed == "" then
		ns.Print("/ai <question> - ask the AI, e.g. /ai where can I learn leatherworking?")
		ns.Print("/ai new - start a fresh conversation (it remembers your recent questions for follow-ups)")
		return
	end
	if typed:lower() == "new" then
		-- Nothing comes back for this, so it costs no live-update slot.
		ns.Stream.Post({ reset = true, player = UnitName("player") })
		ns.Print("Starting a fresh conversation.")
		return
	end
	local question = PlainText(typed)
	if #question > MAX_QUESTION then -- drop the (UTF-8) character that crosses the limit
		question = question:sub(1, MAX_QUESTION + 1):gsub("[\1-\127\192-\255][\128-\191]*$", "")
	end
	ns.Stream.Post({ question = question, player = UnitName("player") })
	ns.Print("Asking… " .. typed)
	if ns.run.liveProblem then
		ns.Print("Live updates are off (" .. ns.run.liveProblem .. "), so the answer will only show after /reload.")
		return
	end
	local seq = ns.run.seq
	C_Timer.After(HINT_AFTER, function()
		if ns.run.seq == seq then
			ns.Print("Still no answer. If none comes, check that the wow-mcp-bridge service is running.")
		end
	end)
end

-- Neither is a command of the default UI.
SLASH_WOWMCPAI1 = "/ai"
SLASH_WOWMCPAI2 = "/ask"
SlashCmdList.WOWMCPAI = Ask
