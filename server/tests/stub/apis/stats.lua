-- Money, XP, hearthstone, /played and the chat frames that print it (Stats.lua).

client.stats = { money = 123456, xp = 4200, xpMax = 8800, rested = 3000, resting = false, xpDisabled = false, hearth = "Thelsamar" }
-- What the server says /played is at time 0; it keeps counting from there.
client.played = { total = 200000, level = 3000, requests = 0 }

function GetTime() return client.now end
function GetMoney() return client.stats.money end
function UnitXP() return client.stats.xp end
function UnitXPMax() return client.stats.xpMax end
function GetXPExhaustion() return client.stats.rested end  -- nil when not rested
function IsResting() return client.stats.resting end
function IsXPUserDisabled() return client.stats.xpDisabled end
function GetBindLocation() return client.stats.hearth end
function GetMaxPlayerLevel() return 60 end
function GetMaxLevelForPlayerExpansion() return 60 end

-- The answer comes back from the server a moment later, to every frame registered for it.
function RequestTimePlayed()
	client.played.requests = client.played.requests + 1
	C_Timer.After(0.5, function()
		local now = math.floor(client.now)
		client.Fire("TIME_PLAYED_MSG", client.played.total + now, client.played.level + now)
	end)
end

-- The chat frames print /played when they get TIME_PLAYED_MSG (ChatFrameUtil.DisplayTimePlayed).
local function ChatFrame()
	local frame = CreateFrame("Frame")
	function frame:UnregisterEvent(e) self.registered[e] = nil end
	function frame:IsEventRegistered(e) return self.registered[e] == true end
	frame:RegisterEvent("TIME_PLAYED_MSG")
	frame:SetScript("OnEvent", function(_, _, total, level)
		table.insert(client.chat, string.format("Total time played: %d", total))
		table.insert(client.chat, string.format("Time played this level: %d", level))
	end)
	return frame
end

ChatFrame1, ChatFrame2 = ChatFrame(), ChatFrame()
ChatFrame2:UnregisterEvent("TIME_PLAYED_MSG") -- e.g. a combat log tab
CHAT_FRAMES = { "ChatFrame1", "ChatFrame2" }
ChatFrameUtil = ChatFrameUtil or {}
function ChatFrameUtil.ForEachChatFrame(fn)
	for _, name in pairs(CHAT_FRAMES) do fn(_G[name]) end
end
