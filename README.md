# wow-mcp

An MCP server plus a WoW addon that let AI agents show you things inside World of Warcraft
while you play. Built for **WoW: Forever** (Interface 16001, retail client).

Ask your agent "where's a leatherworking trainer?" and within about a second the game:

- marks each place on the world map (numbered raid-icon pins with labels and tooltips, also projected onto the zone and continent maps around it),
- sets the game's own navigation waypoint on the first one (map pin, minimap edge arrow, on-screen marker with distance),
- opens the world map there (after combat, if you're fighting),
- prints the list in chat. Click an entry to navigate to it.

No `/reload` and no alt-tab.

Agents can also ask where you are (`get_player_location`: zone, subzone, coordinates,
faction, race, class, level). So "where's the closest leatherworking trainer?" works:
the agent sends every candidate for your faction with `nearest_first`, the game measures real
distances, puts the waypoint on the nearest one and lists them with distances in yards.

## How data gets into a running game

Addons can't use the network or read files while the game runs. Two client behaviours still
reach the disk on demand (technique from [wow-ai](https://github.com/chelinho139/wow-ai), measured on a live Forever client):

1. `C_AddOns.LoadAddOn()` reads a load-on-demand addon's Lua from disk when it's called.
   The installer creates 100 slot addons (`WowMCP_S001`-`S100`, grouped under WowMCP in the
   addon list). On each update the server writes the full current state into every slot.
2. `PlaySoundFile()` reads a sound file again on every call until it has once been valid.
   The addon plays the next `sig/NNNN.wav` twice a second and checks `C_Sound.IsPlaying()` on
   the following poll. On Forever, an empty file still returns a handle but doesn't play.
   After writing the slots, the server turns that file into a 2-second silent wav, and the
   addon then loads one slot, which runs the update.

The same state goes into `WowMCP/Inbox.lua`, which the game reads on login and `/reload`, so
markers survive reloads and restarts.

The other direction uses file opens. A request for the player's state rings a second set of
doorbells (`req/`, which don't use slots). The addon collects the answer as a Lua table, serializes
it to JSON and compresses it with zlib (both built into the client). Then it "plays" a sequence of
empty files `out/000.wav`–`out/fff.wav`, where each file name is 12 bits of the message. The client
opens each file on disk. The server watches that folder with `inotify` and reads the sequence back.
Nothing is drawn or heard, and the game doesn't have to be on screen. Sending is paced at about
1.5 ms of frame time per frame: each open costs about 0.1 ms, and the client drops sound starts
beyond about 256 per frame.

Limits: each live update uses one slot, so you get 100 per UI session (`/reload` resets them).
Updates that arrive during combat are applied when combat ends. If the game's sound is fully
disabled, the doorbell can't work, so live updates turn off and new markers show after `/reload`.
The addon checks this at login.

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```sh
cd server
uv run wow-mcp-install "/path/to/World of Warcraft/_forever_/Interface/AddOns"
claude mcp add --scope user wow \
  -e WOW_MCP_ADDONS_DIR="/path/to/World of Warcraft/_forever_/Interface/AddOns" \
  -- uv run --directory "$PWD" wow-mcp
```

Then **fully quit and restart WoW once**. The game only discovers new addon files at startup.
Re-run the installer after changing the addon code. That only needs a `/reload`, unless it reports new files.

## Tools

| Tool | Does |
|---|---|
| `show_on_map(markers, title?, open_map?, set_waypoint?)` | Marks places. Each marker has `zone` (name as shown in game), `x`/`y` (0-100, as on Wowhead), `label`, optional `note` and `map_id`. Replaces earlier markers. |
| `clear_map()` | Removes the markers and the waypoint they set. |
| `get_player_location()` | Zone, subzone, continent, coordinates, uiMapID, faction, race, class, level, whether in an instance. |
| `get_professions()` | Professions and secondary skills with skill level and current cap. |

`show_on_map(..., nearest_first=true)` lets the game order the markers by distance from you,
so the waypoint goes to the closest one.

Zone names are resolved in game against the client's own map list, so the agent doesn't need
uiMapIDs. Unknown zones are reported in your chat.

The agent is told which game version you're on, so it looks up the right data. That's e.g.
"World of Warcraft: Forever 1.60.1 (build 70862, EU region, enUS client)", worked out at startup
from the install's `.build.info` and the running WoW processes, and also returned by
`get_player_location`. If you're running a different WoW client than the one the addon is
installed for (e.g. Anniversary), the tools say so instead of failing silently.

## In game

| Command | Does |
|---|---|
| `/mcp` | List the current markers |
| `/mcp show` | Open the map on them |
| `/mcp <n>` | Navigate to marker n |
| `/mcp clear` | Hide them |
| `/mcp status` | Live-update status and slots left |

## Development

```sh
cd server && uv run pytest
```

Layout: one file per feature on each side, plugged into small registries.

- `addon/WowMCP/`: `Core.lua` (live channel, registries, `/mcp`), `Requests.lua` (what the server
  can ask for), `Stream.lua` (sending to the server), then one file per feature (`Map.lua`,
  `Player.lua`, `Quests.lua`, ...).
- `server/wow_mcp/`: `app.py` (shared helpers), `tools/` (one module per feature, picked up
  automatically), `transport.py` / `stream.py` (the two directions), `game.py`, `install.py`.
- `server/tests/`: `stub/client.lua` runs the addon in Lua, with game APIs in `stub/apis/` (one file
  per area). There's one `test_<feature>.py` per feature, and `conftest.py` has the fixtures
  (`run_client` for scripted sessions, `live_game` for end-to-end runs against the real tools).

`docs/PLAN.md` describes the contracts and the planned features.
