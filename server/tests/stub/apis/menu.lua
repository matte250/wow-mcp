-- Blizzard_Menu's dropdown buttons (DropdownButton.lua, MenuTemplates.lua, Menu.lua; Forever 1.60.1),
-- as addons use them: CreateFrame("DropdownButton", name, parent, "WowStyle1DropdownTemplate"), then
-- SetupMenu(generator). The generator fills a root description (CreateRadio(text, isSelected,
-- setSelected, data)) and runs anew on GenerateMenu(), when the button shows and when its menu opens.
-- The button's text is its selected radios' (each through the selection translator, by default their
-- own text), or the default text. Picking a radio calls setSelected(data, menuInputData, menu); it
-- returns nothing, which closes the menu, and the button updates its text from the selections.
--
-- client.noDropdowns: CreateFrame refuses the frame type, like a client without Blizzard_Menu.
-- client.OpenMenu(dropdown) opens its menu; client.PickMenu(dropdown, text) picks the radio whose
-- text starts with text. client.dropdowns lists the dropdowns made.

client.dropdowns = {}

local Frame = getmetatable(UIParent)
function Frame:IsMouseOver() return self.mouseOver == true end
function Frame:RegisterForClicks(...) self.clicks = { ... } end
function Frame:SetNormalFontObject(font) self.font = font end

local Description = {}
Description.__index = Description

function Description:CreateRadio(text, isSelected, setSelected, data)
	local radio = setmetatable({ text = text, isSelected = isSelected, responder = setSelected, data = data }, Description)
	table.insert(self.elements, radio)
	return radio
end

function Description:IsSelected() return self.isSelected ~= nil and self.isSelected(self.data) or false end

local Dropdown = {}

function Dropdown:SetupMenu(generator)
	assert(type(generator) == "function", "DropdownButtonMixin:SetupMenu(generator): argument is not a function.")
	self.generator = generator
	if self:IsShown() then self:GenerateMenu() end
end

function Dropdown:GenerateMenu()
	if not self.generator then return end
	self.description = setmetatable({ elements = {} }, Description)
	self.generator(self, self.description)
	self:UpdateText()
end

function Dropdown:UpdateText()
	local texts = {}
	for _, element in ipairs(self.description and self.description.elements or {}) do
		if element:IsSelected() then table.insert(texts, self.translator and self.translator(element) or element.text) end
	end
	self.Text:SetText(#texts > 0 and table.concat(texts, ", ") or self.defaultText)
end

function Dropdown:SetSelectionTranslator(translator) self.translator = translator end
function Dropdown:SetDefaultText(text)
	self.defaultText = text
	self:UpdateText()
end
function Dropdown:SetTooltip(tooltipFunc) self.tooltipFunc = tooltipFunc end
function Dropdown:IsMenuOpen() return self.menuOpen == true end
function Dropdown:OpenMenu()
	if self:IsMenuOpen() or not self:IsShown() then return end
	self:GenerateMenu()
	self.menuOpen = true
end
function Dropdown:CloseMenu() self.menuOpen = false end

-- WowStyle1DropdownMixin:OnShow makes the menu; hiding the button closes it.
function Dropdown:Show()
	if self:IsShown() then return end
	self.shown = true
	self:GenerateMenu()
end
function Dropdown:Hide()
	self.shown = false
	self:CloseMenu()
end
function Dropdown:SetShown(shown)
	if shown then self:Show() else self:Hide() end
end

local createFrame = CreateFrame
function CreateFrame(frameType, name, parent, template)
	if frameType ~= "DropdownButton" then return createFrame(frameType, name, parent, template) end
	if client.noDropdowns then error("CreateFrame: Unknown frame type 'DropdownButton'") end
	local dropdown = createFrame(frameType, name, parent, template)
	for key, fn in pairs(Dropdown) do dropdown[key] = fn end
	dropdown.template, dropdown.width, dropdown.height = template, 120, 25 -- WowStyle1DropdownTemplate's size
	dropdown.Text = client.NewRegion("DropdownText", dropdown) -- not one of the addon's font strings
	table.insert(client.dropdowns, dropdown)
	return dropdown
end

function client.OpenMenu(dropdown)
	dropdown:OpenMenu()
	assert(dropdown:IsMenuOpen(), "the menu didn't open")
	return dropdown.description.elements
end

function client.PickMenu(dropdown, text)
	for _, element in ipairs(client.OpenMenu(dropdown)) do
		if element.text:sub(1, #text) == text then
			local response = element.responder(element.data, { context = 2, buttonName = "LeftButton" }, nil)
			assert(response == nil, "the stub only closes the menu")
			dropdown:CloseMenu()
			dropdown:UpdateText()
			return
		end
	end
	error("no menu element starts with " .. text)
end
