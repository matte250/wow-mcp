-- What the Visuals window (Messages.lua) uses beyond client.lua's widgets and the other API files: buttons
-- that can be disabled, scroll frames, texture coordinates, item tooltips, money as the game shows it and
-- modified clicks on items.

local Frame = getmetatable(UIParent)
local Region = getmetatable(UIParent:CreateTexture())

function Frame:SetPushedTexture(texture) self.pushedTexture = texture end
function Frame:SetDisabledTexture(texture) self.disabledTexture = texture end
function Frame:SetEnabled(enabled) self.enabled = not not enabled end
function Frame:IsEnabled() return self.enabled ~= false end
function Region:SetTexCoord(...) self.texCoord = { ... } end

-- Scroll frames: the offset they were told; the scroll child is the frame they show.
function Frame:SetScrollChild(child) self.scrollChild = child end
function Frame:GetScrollChild() return self.scrollChild end
function Frame:SetVerticalScroll(offset) self.verticalScroll = offset end
function Frame:GetVerticalScroll() return self.verticalScroll or 0 end
function Frame:UpdateScrollChildRect() end

-- An item's own tooltip: client.tooltip.item is the item it shows.
function GameTooltip:SetItemByID(itemID)
	client.tooltip.item = itemID
	client.tooltip.lines = { "item " .. itemID }
end

-- As FormattingUtil.lua's, outside colourblind mode: each coin with its icon, the gold in thousands.
function GetMoneyString(money, separateThousands)
	local gold = math.floor(money / 10000)
	local silver, copper = math.floor(money / 100) % 100, money % 100
	local goldText = tostring(gold)
	if separateThousands then
		local n = 1
		while n > 0 do goldText, n = goldText:gsub("^(%d+)(%d%d%d)", "%1,%2") end
	end
	local parts = {}
	if gold > 0 then parts[#parts + 1] = goldText .. "|TInterface\\MoneyFrame\\UI-GoldIcon:0:0:2:0|t" end
	if silver > 0 then parts[#parts + 1] = silver .. "|TInterface\\MoneyFrame\\UI-SilverIcon:0:0:2:0|t" end
	if copper > 0 or #parts == 0 then parts[#parts + 1] = copper .. "|TInterface\\MoneyFrame\\UI-CopperIcon:0:0:2:0|t" end
	return table.concat(parts, " ")
end

-- A click on an item with a modifier: the links it was given, in order.
client.linked = {}
function HandleModifiedItemClick(link)
	table.insert(client.linked, link)
	return true
end
