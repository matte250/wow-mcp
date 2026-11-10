-- Load-on-demand slot addons (Core.lua).

C_AddOns = {
	IsAddOnLoaded = function(name) return client.loaded[name] == true end,
	LoadAddOn = function(name)
		if client.loaded[name] then return false, "LOADED" end
		local chunk = loadfile(client.addonsDir .. "/" .. name .. "/Inbox.lua")
		if not chunk then return false, "MISSING" end
		client.loaded[name] = true
		chunk(name, {})
		return true
	end,
}

