"""Map markers and the game's waypoint, delivered live through the slots (Map.lua, tools/map.py)."""

from wow_mcp.transport import Transport, SILENT_WAV, bell_path


def test_live_update_and_clear(run_client):
    run_client(r"""
        local ns = client.LoadAddon()
        client.Wait()
        assert(ns.run.live, "signal self-test should pass")
        assert(#WorldMapFrame.pins == 0 and not client.loaded.WowMCP_S001)

        client.Publish('{"title":"Leatherworking trainers","markers":['
            .. '{"zone":"Orgrimmar","x":63.2,"y":45.1,"label":"Karolek \\"Tanner\\" | \\\\ é","note":"In the Drag"},'
            .. '{"zone":"stormwind","x":50,"y":50,"label":"Simon Tanner"},'
            .. '{"zone":"Atlantis","x":1,"y":1,"label":"Nope"}]}')
        client.Wait()
        assert(client.loaded.WowMCP_S001, "signal should trigger a slot load")
        assert(ns.run.seq == 1)

        -- Waypoint and map on the first marker; label survives escaping.
        assert(client.waypoint.uiMapID == 1454 and client.superTracked)
        local wx, wy = client.waypoint.position:GetXY()
        assert(math.abs(wx - 0.632) < 1e-9 and math.abs(wy - 0.451) < 1e-9)
        assert(client.opened[1] == 1454)
        assert(#WorldMapFrame.pins == 1)
        assert(WorldMapFrame.pins[1].Label.text == 'Karolek "Tanner" || \\ é', WorldMapFrame.pins[1].Label.text)

        -- Projected onto the surrounding zone and continent, unlabeled; not onto the other continent.
        WorldMapFrame:SetMapID(1411)
        assert(#WorldMapFrame.pins == 1 and WorldMapFrame.pins[1].Label.text == "")
        local px, py = WorldMapFrame.pins[1].x, WorldMapFrame.pins[1].y
        assert(math.abs(px - (520 + 0.632 * 80 - 500) / 200) < 1e-9 and math.abs(py - (300 + 0.451 * 80 - 300) / 300) < 1e-9)
        WorldMapFrame:SetMapID(1414)
        assert(#WorldMapFrame.pins == 1)
        WorldMapFrame:SetMapID(1453)
        assert(#WorldMapFrame.pins == 1 and WorldMapFrame.pins[1].Label.text == "Simon Tanner", "prefix match")

        local chat = table.concat(client.chat, "\n")
        assert(chat:find("Leatherworking trainers", 1, true))
        assert(chat:find("Atlantis", 1, true), "unknown zone reported")
        assert(chat:find("|Haddon:WowMCP:nav:2|h", 1, true))

        -- No new signal, no slot spent.
        client.Wait()
        assert(not client.loaded.WowMCP_S002)

        -- Chat link navigates to marker 2.
        client.itemRef("addon:WowMCP:nav:2")
        assert(client.waypoint.uiMapID == 1453)

        client.Publish('{"clear":true}')
        client.Wait()
        assert(client.loaded.WowMCP_S002)
        assert(#WorldMapFrame.pins == 0)
        assert(client.waypoint == nil, "clearing removes the waypoint we set")
    """)


def test_reload_restores_quietly(run_client):
    run_client(r"""
        client.LoadAddon()
        client.Wait()
        client.Publish('{"markers":[{"zone":"Durotar","x":50,"y":50,"label":"Razor Hill"}]}')
        client.Wait()
        assert(#client.opened == 1)

        -- /reload: Lua state and loaded slots reset, saved variables and the sound cache don't.
        wipe(client.loaded); wipe(client.tickers); wipe(client.timers); wipe(client.frames); wipe(client.chat); wipe(client.opened)
        WorldMapFrame.providers = {}; wipe(WorldMapFrame.pins); WorldMapFrame.shown = false
        local ns = client.LoadAddon()
        assert(ns.run.seq == 1, "Inbox.lua carries the latest state")
        assert(#WorldMapFrame.pins == 1, "pins restored")
        assert(#client.chat == 0 and #client.opened == 0, "no re-announcement")

        -- Live updates keep working after the reload, from the first slot again.
        client.Wait()
        client.Publish('{"markers":[{"zone":"Orgrimmar","x":10,"y":10,"label":"Bank"}]}')
        client.Wait()
        assert(client.loaded.WowMCP_S001 and ns.run.seq == 2)
    """)


def test_combat_defers_loading(run_client):
    run_client(r"""
        client.LoadAddon()
        client.Wait()
        client.combat = true
        client.Publish('{"markers":[{"zone":"Durotar","x":50,"y":50,"label":"Razor Hill"}]}')
        client.Wait()
        assert(not client.loaded.WowMCP_S001 and #WorldMapFrame.pins == 0)
        client.combat = false
        client.Fire("PLAYER_REGEN_ENABLED")
        assert(client.loaded.WowMCP_S001 and #WorldMapFrame.pins == 1)
        assert(not client.loaded.WowMCP_S002, "one slot per update")
    """)


def test_slash_commands(run_client):
    run_client(r"""
        client.LoadAddon()
        client.Wait()
        local mcp = SlashCmdList.WOWMCP
        mcp("")
        assert(client.chat[#client.chat]:find("No markers", 1, true))
        client.Publish('{"markers":[{"zone":"Durotar","x":50,"y":50,"label":"Razor Hill"},{"zone":"Orgrimmar","x":40,"y":60,"label":"Bank"}]}')
        client.Wait()
        for _, input in ipairs({ "", "list", "show", "2", "9", "status", "help", "CLEAR" }) do mcp(input) end
        assert(#WorldMapFrame.pins == 0, "clear hides the pins")
        local chat = table.concat(client.chat, "\n")
        assert(chat:find("No marker 9", 1, true))
        assert(chat:find("live updates: on; slots left this session: 299/300; last update #1", 1, true))
    """)


def test_live_updates_off_when_sound_disabled(run_client):
    run_client(r"""
        client.sound = false
        local ns = client.LoadAddon()
        client.Wait()
        assert(not ns.run.live and ns.run.liveProblem == "game sound is turned off")
        assert(table.concat(client.chat, "\n"):find("Live updates are off", 1, true))
        client.Publish('{"markers":[{"zone":"Durotar","x":50,"y":50,"label":"Razor Hill"}]}')
        client.Wait()
        assert(not client.loaded.WowMCP_S001)
    """)


def test_signal_with_stale_slot_moves_on(addons, run_client):
    # A raised signal whose slot doesn't carry newer data (only possible if the client
    # cached that signal file earlier in the same process) must not stall the channel.
    transport = Transport(addons)
    bell_path(transport.addon_dir, "sig", 1).write_bytes(SILENT_WAV)
    run_client(r"""
        local ns = client.LoadAddon()
        client.Wait()
        assert(client.loaded.WowMCP_S001 and ns.run.seq == 0, "stale signal spends one slot")
        client.Wait()
        assert(not client.loaded.WowMCP_S002, "and only one")
        -- The update on the spent signal index waits for the next one, which carries the full state.
        client.Publish('{"markers":[{"zone":"Durotar","x":50,"y":50,"label":"Razor Hill"}]}')
        client.Wait()
        client.Publish('{"markers":[{"zone":"Durotar","x":50,"y":50,"label":"Razor Hill"},{"zone":"Orgrimmar","x":1,"y":1,"label":"Gate"}]}')
        client.Wait()
        assert(ns.run.seq == 2 and #WorldMapFrame.pins == 2)
    """)


def test_nearest_first(run_client):
    # Player in Durotar at 50, 50 (world 600, 450 in the stub's geometry).
    run_client(r"""
        client.LoadAddon()
        client.Wait()
        client.Publish('{"nearest_first":true,"markers":['
            .. '{"zone":"Stormwind City","x":50,"y":50,"label":"Far away"},'
            .. '{"zone":"Orgrimmar","x":50,"y":50,"label":"Medium"},'
            .. '{"zone":"Durotar","x":60,"y":50,"label":"Close"}]}')
        client.Wait()
        assert(client.waypoint.uiMapID == 1411, "waypoint on the nearest")
        local chat = table.concat(client.chat, "\n")
        local close, medium, far = chat:find("Close", 1, true), chat:find("Medium", 1, true), chat:find("Far away", 1, true)
        assert(close < medium and medium < far, chat)
        assert(chat:find("[Close]|r|h Durotar 60.0, 50.0 |cffaaaaaa(20 yd)|r", 1, true), chat)
        assert(not chat:find("Far away.-yd"), "no distance across continents")

        -- Without nearest_first the order is kept.
        wipe(client.chat)
        client.Publish('{"markers":[{"zone":"Orgrimmar","x":50,"y":50,"label":"Medium"},{"zone":"Durotar","x":60,"y":50,"label":"Close"}]}')
        client.Wait()
        assert(client.waypoint.uiMapID == 1454)
    """)


def test_clear_all_waypoints(run_client):
    run_client(r"""
        client.LoadAddon()
        client.Wait()
        client.Publish('{"markers":[{"zone":"Durotar","x":50,"y":50,"label":"Razor Hill"}]}')
        client.Wait()
        -- The player then places their own pin on the map.
        client.waypoint = UiMapPoint.CreateFromCoordinates(1454, 0.3, 0.3)

        client.Publish('{"clear":true}')
        client.Wait()
        assert(#WorldMapFrame.pins == 0)
        assert(client.waypoint and client.waypoint.uiMapID == 1454, "a plain clear leaves the player's own waypoint")

        client.Publish('{"clear":true,"all_waypoints":true}')
        client.Wait()
        assert(client.waypoint == nil, "all_waypoints removes it")
        assert(client.chat[#client.chat]:find("Markers and waypoints cleared.", 1, true))

        -- Reloading must not clear a waypoint set after that.
        client.waypoint = UiMapPoint.CreateFromCoordinates(1411, 0.5, 0.5)
        wipe(client.loaded); wipe(client.tickers); wipe(client.timers); wipe(client.frames)
        client.LoadAddon()
        client.Wait()
        assert(client.waypoint and client.waypoint.uiMapID == 1411)
    """)


def test_map_helpers_for_other_features(run_client):
    run_client(r"""
        local ns = client.LoadAddon()
        local loc = ns.Map.Locate("Orgrimmar", 50, 50)
        assert(loc.mapID == 1454 and loc.x == 0.5 and loc.continent == 1)
        assert(ns.Map.Locate("Atlantis", 1, 1) == nil)
        local x, y = ns.Map.Project(loc, 1411)  -- onto Durotar
        assert(math.abs(x - 0.3) < 1e-9 and math.abs(y - 0.1333333) < 1e-6, x .. "," .. y)
        assert(math.floor(ns.Map.Distance(loc) + 0.5) == 117)  -- player at Durotar 50, 50
        assert(ns.Map.SetWaypoint(loc) and client.waypoint.uiMapID == 1454)
        assert(ns.SLOT_COUNT == 300)
    """)
