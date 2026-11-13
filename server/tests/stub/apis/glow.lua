-- What the glow (Glow.lua) uses beyond client.lua's widgets: frame levels, visibility through parents, blend
-- modes, and stand-ins for the game's windows whose buttons it finds. client.GlowUI() builds those windows (as
-- the UI does after each /reload): the combined bags (the backpack's slots), a vendor's first page, the old
-- loot window, the action bars, the character's gear slots, and optionally BetterBags' item buttons.

local Frame = getmetatable(UIParent)
local Region = getmetatable(UIParent:CreateTexture())

function Region:SetBlendMode(mode) self.blend = mode end
function Region:SetDesaturated(on) self.desaturated = on end
function Frame:SetFrameLevel(level) self.level = level end
function Frame:GetFrameLevel() return self.level or 1 end
function Frame:GetFrameStrata() return self.strata or "MEDIUM" end
function Frame:GetEffectiveScale() return self.scale or 1 end
function Frame:GetSize() return self.width or 36, self.height or 36 end
-- The mouse is over a frame a test says it is over.
function Frame:IsMouseOver() return self.mouseOver == true end
function Frame:GetObjectType() return "Frame" end
function Frame:SetID(id) self.id = id end
function Frame:GetID() return self.id or 0 end
-- Shown, and so is every parent.
Frame.IsVisible = Frame.IsVisible or function(self)
	local frame = self
	while frame do
		if not frame:IsShown() then return false end
		frame = frame.parent
	end
	return true
end

client.actions = {} -- action slot -> { kind, id }
function GetActionInfo(action)
	local a = client.actions[action]
	if a then return a[1], a[2] end
end

-- A trainer: service index -> { name, spell or item ID, item = true for a recipe's item }.
client.trainer = {}
function GetTrainerServiceInfo(index)
	local s = client.trainer[index]
	if s then return s.name, "available" end
end

client.loot = {} -- loot slot -> item ID
function GetLootSlotLink(slot)
	local id = client.loot[slot]
	return id and string.format("|cffffffff|Hitem:%d::::::::12:::::|h[x]|h|r", id) or nil
end

local function Button(name, parent, id)
	local button = CreateFrame("Button", name, parent)
	button:SetID(id)
	return button
end

function client.GlowUI(opts)
	opts = opts or {}
	-- Bags: closed until a test shows them.
	local bags = CreateFrame("Frame", "ContainerFrameCombinedBags", UIParent)
	bags:Hide()
	bags.buttons = {}
	for slot = 1, 16 do
		local button = Button(nil, bags, slot)
		function button:GetBagID() return 0 end
		bags.buttons[slot] = button
	end
	function bags:EnumerateValidItems() return ipairs(self.buttons) end

	MERCHANT_ITEMS_PER_PAGE = 10
	local merchant = CreateFrame("Frame", "MerchantFrame", UIParent)
	merchant:Hide()
	merchant.selectedTab = 1
	for i = 1, 10 do Button("MerchantItem" .. i .. "ItemButton", merchant, i) end

	local loot = CreateFrame("Frame", "LootFrame", UIParent)
	loot:Hide()
	for i = 1, 4 do Button("LootButton" .. i, loot, i).slot = i end

	for i = 1, 12 do Button("ActionButton" .. i, UIParent, i).action = i end

	local character = CreateFrame("Frame", "CharacterFrame", UIParent)
	character:Hide()
	Button("CharacterHeadSlot", character, 1)
	Button("CharacterChestSlot", character, 5)

	Button("MerchantRepairAllButton", merchant, 0)

	local trainer = CreateFrame("Frame", "ClassTrainerFrame", UIParent)
	trainer:Hide()
	for i = 1, 11 do Button("ClassTrainerSkill" .. i, trainer, i) end
	C_TooltipInfo.GetTrainerService = function(index)
		local s = client.trainer[index]
		if s then return { type = s.item and Enum.TooltipDataType.Item or Enum.TooltipDataType.Spell, id = s.id, lines = {} } end
	end

	if opts.betterBags then
		client.loaded.BetterBags = true
		local bb = CreateFrame("Frame", nil, UIParent)
		bb:Hide()
		client.betterBags = bb
		local buttonsBySlotkey = {}
		for slot = 1, 16 do buttonsBySlotkey["0_" .. slot] = { frame = CreateFrame("Frame", nil, bb), button = {} } end
		buttonsBySlotkey.Container = { frame = CreateFrame("Frame", nil, bb) } -- a virtual one: no bag and slot
		local itemFrame = { buttonsBySlotkey = buttonsBySlotkey }
		local addon = { GetModule = function(_, name) return name == "ItemFrame" and itemFrame or nil end }
		LibStub = function(lib)
			assert(lib == "AceAddon-3.0")
			return { GetAddon = function(_, name) return name == "BetterBags" and addon or nil end }
		end
	end
end

-- Which frames glow right now, by colour, as the addon reports them.
function client.Glowing()
	return client.ns.Glow.Shown()
end
