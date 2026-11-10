-- Tooltip data post-calls (Tooltips.lua), as in Blizzard_SharedXMLGame/Tooltip/TooltipDataHandler.lua:
-- a tooltip built from data is cleared, gets the data's lines, then runs the post-calls
-- registered for "ALL" and for the data's type, then shows.

Enum.TooltipDataType = { Item = 0, Spell = 1, Unit = 2 }

local postCalls = {} -- type -> { fn }; a test simulating /reload wipes it
client.tooltipPostCalls = postCalls

TooltipDataProcessor = {
	AllTypes = "ALL",
	AddTooltipPostCall = function(tooltipType, fn)
		if not tooltipType then return end
		postCalls[tooltipType] = postCalls[tooltipType] or {}
		table.insert(postCalls[tooltipType], fn)
	end,
}

-- Show a tooltip for data like { type, id, hyperlink, lines = { { leftText } } }; returns its
-- lines ({ text, r, g, b, wrap }).
function client.ShowTooltip(data)
	local tooltip = { lines = {} }
	function tooltip:AddLine(text, r, g, b, wrap)
		table.insert(self.lines, { text = text, r = r, g = g, b = b, wrap = wrap })
	end
	function tooltip:NumLines() return #self.lines end
	for _, line in ipairs(data.lines or {}) do tooltip:AddLine(line.leftText) end
	for _, key in ipairs({ TooltipDataProcessor.AllTypes, data.type }) do
		for _, fn in ipairs(postCalls[key] or {}) do fn(tooltip, data) end
	end
	return tooltip.lines
end
