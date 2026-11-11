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

Type `/ai <question>` (or `/ask`) in WoW chat, or just type in the **AI** chat tab. The question goes
to `wow-mcp-bridge`, a small background service. It runs Claude Code headless with these tools and
shows the answer in the AI tab, with map markers when useful. Shift-clicked item links work.

The conversation has its own chat tab, so answers don't drown in a city's chat: your question,
"Thinking…", then the answer. The tab is made the first time you ask and flashes when a new answer
arrives while you're in another one. In the AI tab, plain lines go to the AI; lines starting with `/`,
and replies with R, work as usual. `/mcp aitab both` also shows the conversation in your main chat
window, `/mcp aitab off` only there. If you close or rename the tab, the AI talks in the main window,
and `/mcp aitab` brings the tab back.

**Which model answers:** the AI tab has a picker at its top right: Default (what the bridge was started
with), Fable (the most capable, slowest), Opus, Sonnet (fast and strong) or Haiku (fastest, for simple
lookups). `/ai model` says which one answers; `/ai model sonnet` (or fable, opus, haiku, default) picks
one. The pick is kept per character and goes with each question. Answers are signed with the model,
e.g. [Claude Sonnet], and the conversation carries over when you switch.

```sh
cd server && uv run wow-mcp-bridge --addons-dir "/path/to/World of Warcraft/_forever_/Interface/AddOns"
```

To keep it running, copy `server/systemd/wow-mcp-bridge.service` to `~/.config/systemd/user/`, fix the
two paths in it, and run `systemctl --user enable --now wow-mcp-bridge`. Its log is at
`journalctl --user -u wow-mcp-bridge -f`. `--model` is the model when you haven't picked one in game (only fable, opus, sonnet and haiku can be picked); `--timeout` and `--agent` change how it answers (with `--agent`, picks are ignored).

It remembers your recent questions (by default the last 8 from the past hour), so follow-ups
like "and the next one?" work. `/ai new` starts a fresh conversation. `--history` and
`--history-minutes` change how much it remembers (`--history 0` turns it off). Only one bridge
can answer for a game at a time; a second one refuses to start.

## Tools

Every tool that reads the game also returns the game version, so agents look things up for the right one.

| Area | Tools |
|---|---|
| Map | `show_on_map` (markers + waypoint; `nearest_first` sorts by real distance), `clear_map` (`all_waypoints` also removes yours) |
| Plans | `show_plan` (a checklist window that guides you step by step and ticks steps off itself: items in bags or bank, quests, places, level, skill, recipes; item IDs are checked in the game before a plan is shown), `get_plan_progress`, `clear_plan` |
| Goals | `set_goals` (background goals next to the plan that track themselves and point out chances: zones and areas, a tooltip line on mobs, vendors and the auction house selling the item; a plan step can wait for one), `get_goals`, `clear_goals` |
| Gear | `find_upgrades` (the best upgrades per slot this character can use, from the auction house's last full scan, quest rewards in the log and vendors seen: stats, required level, where to get it and what it costs, and the score change; random-suffix items priced per suffix; `item_ids` for "is X an upgrade"; auction prices brought up to date from the online snapshot when it's newer) |
| Auction house | `get_auction_prices` (online from Blizzard's API when set up: a snapshot of your auction house at most about an hour old, plus the game's own price when that's newer; otherwise the game's full scan or a search, with its age; `history` adds the usual price (7-day median) and price history; an item that's gone says when it was last listed and at what), `watch_auction_prices` (puts items on the priority list checked at every visit) |
| Routes | `show_route` (numbered stops, lines, navigator arrow; a plan's current step uses the same navigator), `clear_route` |
| Areas | `highlight_areas` (shaded circles, e.g. where a mob or herb is), `clear_areas` |
| Where and who | `get_player_location` (zone, coordinates, faction, race, class, level) |
| Character | `get_character_stats` (money, XP, rested, hearthstone, /played), `get_reputations` |
| Professions | `get_professions` (skill levels), `get_known_recipes` (cached when you open a profession, plus recipes you learn as you learn them; each recipe's `item_id` is what it makes), `get_vendor_items` (vendors you've visited) |
| Items | `get_inventory` (bags, equipment with durability and stats, `include_bank` for the bank as last seen, `include_mail` for the mailbox as last seen: attachments, money, COD, expiry, auction house sales and the game's new-mail notice), `get_item_details` (stats and tooltip text of any item, read from the game), `set_item_notes` / `clear_item_notes` (a line on item tooltips) |
| Quests | `get_quest_status` (done / in log / ready, for given quest IDs), `get_quest_log` (objective progress, and rewards: the XP this character gets, money and items, read from the game) |
| Travel | `get_flight_paths` (known flight masters) |
| Telling you things | `show_message` (chat or center screen), `set_reminder` / `list_reminders` / `cancel_reminder` (by time or place) |
| Diagnostics | `get_status` (is the addon up to date, live updates, slots left; says when to /reload or reinstall) |

At the auction house the addon takes a full scan whenever the game allows one (every 15 minutes),
reading it a little per frame: a price for every item listed, kept per realm, also once it's gone.
A priority list of up to 100 items (what your goals, plan and AI track, then what you could sell from
your bags and bank) also gets the cheapest price points. When the last full scan is over 10 minutes
old, those items are searched one at a time, at most 80 auction house messages a minute counting
yours, and only on the Browse view, so it never changes what you see.

The full scan also keeps, for gear with random suffixes ("of the Bear"), the cheapest price of each
suffix. Upgrades are searched in game, a little per frame: the slot and armor or weapon type of every
item from the scans, quest rewards and vendors seen, then level, stats and price, then the tooltip for
what this character can use. Stat weights are leveling defaults for the class and talent
specialization unless the agent passes its own.

The mailbox can only be read at a mailbox, so the addon saves it per character whenever one is open,
like the bank. Agents count mail items as yours (to collect), point out mail that's about to expire,
and away from a mailbox still see "new mail from the Auction House".

Zone names are resolved in game against the client's own map list, so the agent doesn't need
uiMapIDs. Unknown zones are reported in your chat.

Items in anything an agent shows in game (plans, messages, routes, areas, reminders, map labels,
item notes, `/ai` answers) appear with their icon and the game's own name in quality colour.
Agents write `{item:2318}` or `{item:2318:Light Leather}`; in chat it's a real item link you can
click or shift-click.

The agent is told which game version you're on, so it looks up the right data. That's e.g.
"World of Warcraft: Forever 1.60.1 (build 70862, EU region, enUS client)", worked out at startup
from the install's `.build.info` and the running WoW processes. It's also told which sites cover
that version. If you're running a different WoW client than the one the addon is installed for,
the tools say so instead of failing silently.

## Online auction prices (optional)

Blizzard's API has had Forever's auction houses since 2026-11-06. Create a client at
https://develop.battle.net/access/clients and put its ID and secret in
`~/.config/wow-mcp/battlenet.env` (chmod 600):

```
BATTLENET_CLIENT_ID=...
BATTLENET_CLIENT_SECRET=...
```

`get_auction_prices` then answers from a snapshot of your auction house that's at most about an hour
old, even if you haven't been there lately, and keeps price history. The API has no realms, only 15
houses per region (five each called Alliance, Horde and Blackwater Auction House). wow-mcp works out
which one is yours by comparing them with the prices your game saw; `BATTLENET_AUCTION_HOUSE=<id>` in
the same file sets it instead. Every snapshot is kept in `~/.cache/wow-mcp/auctions.sqlite3` (about
0.7 MB an hour). `wow-mcp-bridge` fetches each new one in the background; `--no-auction-refresh` turns
that off. `cd server && uv run wow-mcp-auctions status` shows the house and how sure the match is, the
latest snapshot and how much history there is; `houses`, `refresh` and `match` do what they say.

## In game

| Command | Does |
|---|---|
| `/ai <question>`, `/ask <question>` | Ask your AI (needs `wow-mcp-bridge` running); or just type in the AI chat tab |
| `/ai model [default, fable, opus, sonnet, haiku]` | Which Claude model answers your `/ai` questions (kept per character); the AI tab has a picker for it too |
| `/mcp aitab [on, both, off]` | Where the AI conversation shows: its own chat tab (default), the tab and the main window, or only the main window; brings a closed tab back |
| `/ai new` | Start a fresh conversation with your AI |
| `/mcp` | List the current markers |
| `/mcp show`, `/mcp <n>`, `/mcp clear` | Open the map on them, navigate to marker n, hide them |
| `/mcp plan [show, hide, next, prev, <n>, reset, clear]` | The plan window. Click a step to make it current; right-click it or click its box to tick or un-tick it; the wheel scrolls; − collapses |
| `/mcp goals [show, hide, mute <n>, unmute <n>, clear [<n>, all]]` | The goals list (docks under the plan window): right-click a goal to mute it; − collapses; `clear` removes the done ones |
| `/mcp prices` | Auction prices your AI has: the last full scan, the priority list, searches left; at the auction house, look the list up again |
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

- `addon/WowMCP/`: `Core.lua` (live channel, registries, `/mcp`, item tokens in AI text), `Requests.lua` (what the server
  can ask for), `Stream.lua` (sending to the server), then one file per feature (`Map.lua`,
  `Player.lua`, `Quests.lua`, ...).
- `server/wow_mcp/`: `app.py` (shared helpers), `item_text.py` (the item markup, shared by the tools and the bridge), `tools/` (one module per feature, picked up
  automatically), `transport.py` / `stream.py` (the two directions), `battlenet.py` (Blizzard's API), `online_auctions.py` (online prices: snapshots, history, house matching, `wow-mcp-auctions`), `game.py`, `install.py`.
- `server/tests/`: `stub/client.lua` runs the addon in Lua, with game APIs in `stub/apis/` (one file
  per area). There's one `test_<feature>.py` per feature, and `conftest.py` has the fixtures
  (`run_client` for scripted sessions, `live_game` for end-to-end runs against the real tools).

`docs/PLAN.md` describes the contracts and the planned features.
