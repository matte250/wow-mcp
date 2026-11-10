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
   The installer creates 300 slot addons (`WowMCP_S001`-`S300`, grouped under WowMCP in the
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

Limits: each live update uses one slot, so you get 300 per UI session (`/reload` resets them).
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

## Asking from inside the game: `/ai`

Type `/ai <question>` (or `/ask`) in WoW chat. The question goes to `wow-mcp-bridge`, a small
background service. It runs Claude Code headless with these tools and shows the answer in your
chat, with map markers when useful. Shift-clicked item links work.

```sh
cd server && uv run wow-mcp-bridge --addons-dir "/path/to/World of Warcraft/_forever_/Interface/AddOns"
```

To keep it running, copy `server/systemd/wow-mcp-bridge.service` to `~/.config/systemd/user/`, fix the
two paths in it, and run `systemctl --user enable --now wow-mcp-bridge`. Its log is at
`journalctl --user -u wow-mcp-bridge -f`. `--model`, `--timeout` and `--agent` change how it answers.

It remembers your recent questions (by default the last 8 from the past hour), so follow-ups
like "and the next one?" work. `/ai new` starts a fresh conversation. `--history` and
`--history-minutes` change how much it remembers (`--history 0` turns it off). Only one bridge
can answer for a game at a time; a second one refuses to start.

## Tools

Every tool that reads the game also returns the game version, so agents look things up for the right one.

| Area | Tools |
|---|---|
| Map | `show_on_map` (markers + waypoint; `nearest_first` sorts by real distance), `clear_map` (`all_waypoints` also removes yours) |
| Routes | `show_route` (numbered stops, lines, navigator arrow), `clear_route` |
| Areas | `highlight_areas` (shaded circles, e.g. where a mob or herb is), `clear_areas` |
| Where and who | `get_player_location` (zone, coordinates, faction, race, class, level) |
| Character | `get_character_stats` (money, XP, rested, hearthstone, /played), `get_reputations` |
| Professions | `get_professions` (skill levels), `get_known_recipes` (cached when you open a profession), `get_vendor_items` (vendors you've visited) |
| Items | `get_inventory` (bags, equipment with durability, `include_bank` for the bank as last seen), `set_item_notes` / `clear_item_notes` (a line on item tooltips) |
| Quests | `get_quest_status` (done / in log / ready, for given quest IDs), `get_quest_log` (objective progress) |
| Travel | `get_flight_paths` (known flight masters) |
| Telling you things | `show_message` (chat or center screen), `set_reminder` / `list_reminders` / `cancel_reminder` (by time or place) |
| Diagnostics | `get_status` (is the addon up to date, live updates, slots left; says when to /reload or reinstall) |

Zone names are resolved in game against the client's own map list, so the agent doesn't need
uiMapIDs. Unknown zones are reported in your chat.

The agent is told which game version you're on, so it looks up the right data. That's e.g.
"World of Warcraft: Forever 1.60.1 (build 70862, EU region, enUS client)", worked out at startup
from the install's `.build.info` and the running WoW processes. It's also told which sites cover
that version. If you're running a different WoW client than the one the addon is installed for,
the tools say so instead of failing silently.

## In game

| Command | Does |
|---|---|
| `/ai <question>`, `/ask <question>` | Ask your AI (needs `wow-mcp-bridge` running) |
| `/ai new` | Start a fresh conversation with your AI |
| `/mcp` | List the current markers |
| `/mcp show`, `/mcp <n>`, `/mcp clear` | Open the map on them, navigate to marker n, hide them |
| `/mcp route [show, next, prev, <n>, stop]` | The route and its navigator (right-click the navigator for the next stop) |
| `/mcp areas [show, clear]` | The highlighted areas |
| `/mcp messages` | Repeat the last messages from your AI |
| `/mcp reminders` | Reminders waiting to fire |
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
