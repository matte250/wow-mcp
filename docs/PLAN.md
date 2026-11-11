# Feature plan: parallel workstreams

Fourteen features, built in parallel, one workstream each, run as eight agents (see the end). The project is structured so that each
workstream only touches its **own files**. Everything shared is already in place and is changed
only by the integrator, who merges the workstreams one by one.

| ID | Feature | Addon file | Server file | Request kind / payload key |
|---|---|---|---|---|
| W1 | Money, XP, rested XP, hearthstone, /played | `Stats.lua` | `tools/stats.py` | kind `STATE`, section `stats` |
| W2 | Bags, equipment (with durability), bank as last seen | `Inventory.lua` | `tools/inventory.py` | kind `ITEMS` |
| W3 | Quest objectives progress | `Quests.lua` | `tools/quests.py` | kind `QUESTS` |
| W4 | Reputations | `Reputation.lua` | `tools/reputation.py` | kind `REPUTATION` |
| W5 | Known flight paths | `Travel.lua` | `tools/travel.py` | kind `TRAVEL` |
| W6 | Recipes known, vendors seen | `Knowledge.lua` | `tools/knowledge.py` | kind `KNOWLEDGE` |
| W7 | Show AI text in game | `Messages.lua` | `tools/messages.py` | payload key `messages` |
| W8 | `/ai <question>` from in game | `Chat.lua`, `Stream.lua` | `bridge.py`, `stream.py` | stream posts (`WM\2`) |
| W9 | Routes + navigator arrow | `Routes.lua`, `Routes.xml` | `tools/routes.py` | payload key `routes` |
| W10 | Area highlights | `Areas.lua`, `Areas.xml` | `tools/areas.py` | payload key `areas` |
| W11 | Reminders (time or place) | `Reminders.lua` | `tools/reminders.py` | payload key `reminders` |
| W12 | Item tooltip notes | `Tooltips.lua` | `tools/item_notes.py` | payload key `itemNotes` |
| W13 | Version source hints | – | `tools/sources.py` | guidance only |
| W14 | Status / version tool | `Status.lua` | `tools/status.py` | kind `STATUS` |
| W15 | Item stats from the game | `Inventory.lua` | `tools/items.py`, `tools/inventory.py` | payload key `itemLookup`, kind `ITEM_DETAILS` |
| W16 | Quest rewards (XP, money, items) | `Quests.lua` | `tools/quests.py` | kind `QUESTS` |
| W17 | Plans: step checklists that track themselves | `Plans.lua` | `tools/plans.py` | payload key `plan`, STATE section `plan` |
| W18 | Goals: background targets that point out chances | `Goals.lua` | `tools/goals.py` | payload key `goals`, STATE section `goals` |
| W20 | Mail as last seen, new mail | `Inventory.lua` | `tools/inventory.py` | kind `ITEMS`, section `mail` |
| W21 | Gear upgrades | `Auction.lua` (finder, per-suffix scan prices), `Player.lua` (spec), `Inventory.lua` (exports) | `tools/upgrades.py`, `tools/items.py` (`ask`) | payload key `upgradeQuery`, ITEM_DETAILS section `upgrades` |
| W22 | Online auction prices (Blizzard's API) | – | `battlenet.py`, `online_auctions.py`, `tools/auction.py`, `tools/upgrades.py`, `bridge.py` | config `~/.config/wow-mcp/battlenet.env`, cache `~/.cache/wow-mcp/auctions.sqlite3` |
| W23 | Long-term memory: conversation archive, search, notes | – | `memory.py`, `tools/memory.py`, `bridge.py` | data `~/.local/share/wow-mcp/` (`conversations.jsonl`, `notes.json`) |
| W24 | The player's own auctions | `Auction.lua` (after the lookups, own do-block) | `tools/my_auctions.py` | KNOWLEDGE section `myAuctions` |
| W19 | Auction house prices (full scans, priority list) | `Auction.lua` | `tools/auction.py` | payload key `auctionWatch` (items `{item, at}`, `cleared`, `lookup {id, at, items}`), KNOWLEDGE section `auction` |

Each workstream also owns `server/tests/test_<feature>.py` and `server/tests/stub/apis/<feature>.lua`
(W3 already has `test_quests.py` and `stub/apis/quests.lua`; W8 owns `test_stream.py` changes).

The placeholder files already exist and are listed in the TOC. WoW only discovers files at
startup, so after the one restart for this restructure, every feature only needs `/reload`.

## Shared contracts (already implemented, don't change them in a workstream)

### Addon (`addon/WowMCP/`)
- **Request sections** (`Requests.lua`): `ns.RegisterSection(ns.Kinds.X, "name", collect)`.
  When the server asks for kind X, every section registered for X runs and the reply is
  `{ name = collect(), ... }`. A section that errors shows up as `errors[name]` instead.
  Values must be JSON-serializable (`C_EncodingUtil.SerializeJSON`): tables, strings,
  numbers, booleans. Sequences become JSON arrays, but an **empty** table becomes `{}`.
- **Payload receivers** (`Core.lua`): `ns.RegisterReceiver("key", function(value) end)` runs for
  every new payload that carries `key`. A receiver decides for itself what's new (e.g. by an `id`).
- **Slash commands** (`Core.lua`): `ns.RegisterCommand("name", "/mcp name - what it does", run)`.
  `run(rest)` gets the rest of the line. `/ai` (W8) is its own slash command, not under `/mcp`.
- **Login hooks**: `ns.OnLogin(fn)` runs at `PLAYER_LOGIN`, before the stored payload is applied.
- **Saved data**: `WowMCPDB` is the SavedVariables table. Use your own subtable
  (`WowMCPDB.bank`, `WowMCPDB.knowledge`, ...). It's written to disk on logout and `/reload`.
- **Output**: `ns.Print(msg)` prints to chat; `ns.Escape(text)` before showing any text from the AI.
  Once W7 lands, `ns.ShowMessage(text, style, sender)` shows a message the way W7 does. Other
  features call it when present and fall back to `ns.Print`.
- **AI conversation**: `ns.Chat(line, frame)` and `ns.ShowMessage(text, style, sender, sound, frames)`
  take target chat windows (default: the main one); `ns.AIChat.Answer(text, style, sender)` shows an
  answer in the conversation. Messages with `reply = true` (`publish_message(..., reply=True)`, set by
  the bridge) go to the AI tab, made with `FCF_OpenNewWindow(name, true)` and found by name;
  `WowMCPDB.aiTab = { mode, made = { ["Name-Realm"] = true } }`. Typing in the tab goes through
  EventRegistry's chat box events plus `hooksecurefunc` only: no writes to Blizzard's chat box fields.
  `/ai model [name]`, `/ai thinking [level]` (alias `/ai effort`) and the AI tab's picker (one Blizzard_Menu
  dropdown with a Model and a Thinking group, or a cycling button: click for the model, shift-click for
  the level) pick both per character (`WowMCPDB.aiTab.models`, `.efforts`). Question posts are
  `{ question, player, model?, effort? }`; `model` is one of Chat.lua's `MODELS` ids (must match
  `bridge.MODELS`), `effort` one of its `EFFORTS` (must match `bridge.EFFORTS`, Claude Code's `--effort`
  levels). The bridge passes `--model`/`--effort` only for those, signs answers "Claude <Model>[ · <level>]",
  and replies carry `defaultModel` and `defaultEffort`. The agent
  contract is `agent(prompt, model, player, effort, progress)` (`progress.saw(step, tool)` on each sign of life, `progress.read(event)` for each of Claude Code's stream-json events (steps and tokens, `bridge.Tokens`), `progress.tick()` about every second, `bridge.Progress`); `player` reaches the wow tools as `WOW_MCP_PLAYER`.
- **/ai progress**: the bridge acks each question and sends progress with `publish_status(status, text,
  question, quiet)`: `messages` entries `{ id, status = "ack" | "progress", text, session, post, quiet, at }`,
  routed to `ns.AIChat.Status(m)`, never shown as messages; replies carry `session`/`post`, and a question's
  notes are dropped when its newer note or answer is added. `ns.Stream.Post(value)` returns `post, session`.
  Stub chat windows have `RemoveMessagesByPredicate` and `TransformMessages`. Status entries also carry
  `tokens` and `rate` when the agent streams Claude Code's events (`Bridge(tokens=agent.streams)`; the ack's
  `tokens` is 0); between updates the game runs the count on at `rate` for up to 40 s (Chat.lua's
  `GUESS_FOR` = `bridge.PROGRESS_EVERY`). Answers carry `took` (seconds) and `tokens` (the result's exact
  count); the AI tab adds a grey "took M:SS · ↓ N tokens" line.
- **Items in AI text**: `ns.ItemText(text)` escapes AI text for display outside chat and shows
  `{item:<id>}` / `{item:<id>:<name>}` as icon plus game name in quality colour; `ns.RenderItems(text)`
  for text that's already escaped, `ns.ItemIcon(id)` for just the icon. `ns.Print` and
  `ns.ShowMessage` render tokens in chat as item links through `ns.Chat(line)`, which holds a line up
  to 1 s while its items load, keeping chat order. `ns.OnItemsLoaded(fn)` runs once items shown
  before they loaded arrive, so features can redraw.
- **Map helpers** (`Map.lua`, for routes, areas, reminders): `ns.Map.Locate(zone, x, y, mapID)` turns a zone
  name and 0-100 coordinates into a location, or nil if the zone is unknown. Then:
  - `ns.Map.Project(loc, mapID)` gives its x, y on another map.
  - `ns.Map.Distance(loc)` gives yards from the player.
  - `ns.Map.SetWaypoint(loc)` sets the game's waypoint.
  - `ns.Map.Resolve(zone, mapID)` returns the map info.
- **Guiding** (for plans or anything else that drives the navigator): `ns.Routes.Guide(places,
  {key, title, loop, onArrive, front})` shows places with the navigator, waypoint and map lines,
  and `ns.Routes.Unguide()` hands back to the plain route. `ns.Areas.Guide(list, key, title)` and
  `ns.Areas.Unguide()` do the same for areas. A newer `show_route` route shows over the guide until
  it's finished or hidden.
- **Plans, goals, prices**: `ns.Plans.ParseCondition` / `CheckCondition(c)` / `Items()` evaluate plan
  conditions anywhere (goals use them for targets; `{goal: id}` is a condition too).
  `ns.Goals.Status` / `Items` / `AuctionPrices` / `Money` and `ns.Auction.Price` / `Cost` / `IsOpen`.
  Guidance orders 12-14 are plans, goals and buy-vs-farm.
- **AH status panel** `WowMCPAuctionPanel` (our frame, parented to UIParent, only anchored under
  `AuctionHouseFrame`'s bottom right): Auction.lua's internal `Notify()` schedules a redraw at most every
  0.25 s from a timer, never per frame. `WowMCPDB.auctionPanel = { hidden }` (account-wide).
- **Blocked actions**: Status.lua keeps the last ten ADDON_/MACRO_ACTION_BLOCKED/FORBIDDEN events (any
  addon) with the stack in `WowMCPDB.blocked`; `get_status` reports them as `blocked_actions`.
- **Upgrades**: auction data also has `variants = { [id] = "scan;suffix,price,listed;..." }` for gear with
  random suffixes; `ns.Inventory = { Equipment, GearStats, Lines }`; STATE `character` also has
  `classFile`, `spec`, `role`. Server: `items.ask(...)` is the publish-then-ask loop for ITEM_DETAILS
  sections. Guidance order 47 is upgrades.
- **Online prices** (`online_auctions.py`): `lookup(items, realm, faction, seen, with_history)` returns
  `Prices(enabled, house, how, at, since, items={id: ItemPrice(at, listing, commodity, last, usual,
  history)}, notes)`. API problems become notes and never raise. In-game prices passed as `seen` are the
  evidence for which house is the player's. Tests: conftest's autouse `no_online_prices` keeps every
  test away from the real config, cache and API; tests that need the API patch
  `online_auctions.make_client` with an `httpx2.MockTransport`.
- **Memory** (`memory.py`): `Archive().add/extend/entries/search` over the append-only `conversations.jsonl`
  (records `{at, player, model, question, answer, approx?}`; an entry's id is its line). `Notes().about/add/
  forget` per player in `notes.json` (30 notes / 3,000 characters, oldest cut). `memory.read_log` reads the
  bridge's "asks"/"answer:" log lines back, so keep their wording. Guidance orders 17-18 are memory.
  Tests: conftest's autouse `no_real_memory` sets `XDG_DATA_HOME` to tmp.
- **Binding**: `itemDetails` items carry `bind` (the `Enum.ItemBind` value) and `held` / `heldBound`;
  `items.binding()` is the one bind mapping, shared by `get_item_details`, `get_vendor_items` and
  `get_auction_prices`. Vendor items carry `bind` when loaded at the merchant read. Auction prices carry
  `scanned = true` when some full scan saw the item; never-listed items asked about are looked up with
  `items.look_up`. The trading rule is `TRADING_GUIDANCE` (order 14).
- **Own auctions**: `WowMCPDB.myAuctions["Name-Realm"] = { seen, auctions, notices }`; each auction
  `{ id, item, name, count, price (per item), bid, status (active, sold-pending, sold, expired, cancelled,
  gone), expires, soonest, left ("seconds" | "band"), how (list, notice, chat, mail, cancel), ... }`. KNOWLEDGE
  section `myAuctions` = `{ seen, open, reading, realm, faction, auctions, more, notices, prices, places,
  mail = { at, sales }, problem }`. Guidance order 14, after the auction guidance. Auction.lua is at 184 of
  Lua's 200 top-level locals: new code goes in a do-block.
- **Mail data**: `WowMCPDB.mail["Name-Realm"] = { time, total, mails = { { from, subject, money, cod,
  expires, read, returned, returns, player, alt, gm, ah, sale = { item, count, player, price, deposit,
  fee, eta }, items = { { id, name, count, quality } } } } }`; ITEMS section `mail` = `{ inbox, open,
  newMail }`.
- **Auction data**: `WowMCPDB.auction[realm] = { items, scan = { [id] = "price,listed,auctions,scan#" },
  scans, current, tracked }` (the full-scan table is never pruned); `WowMCPDB.auctionScan.asked` is
  account-wide, the time of the last full scan request. `ns.Auction.Price(id)` returns `src`
  ("scan"/"search") and `last` (`{price, listed, at}`) when the item isn't listed now. Container
  items carry `bound = true` when soulbound.
- **Core state** (read only): `ns.run` (`live`, `liveProblem`, `seq`, `req`), `ns.SlotsLeft()`,
  `ns.SLOT_COUNT`, and `ns.BUILD` (from `Build.lua`, which the installer generates).
- **Sending to the server**: `ns.Stream.Send(id, value)` (replies; Requests.lua does this for you).
- **Cost**: replies are JSON, then zlib, then streamed at about 1.5 ms of frame time per frame. Keep
  sections reasonably small. A few KB compressed is fine (about a second); avoid tens of KB.
- Lua 5.1 (WoW): no `goto`, no `//`, no bit operators. `string.format("%d", x)` needs an integer
  (`math.floor`), because the test stub runs Lua 5.5.

### Server (`server/wow_mcp/`)
- **Item markup**: `item_text.py` has `ITEM` (the token regex, matching Core.lua), `HINT` for tool
  fields that take AI text, `GUIDANCE` (registered in `tools/messages.py`, order 66) and `shorten()`
  (truncates without cutting a token).
- **Registering tools**: in your `tools/<feature>.py`, decorate with `@tool(annotations=ToolAnnotations(...))`
  from `wow_mcp.app`. The module is imported automatically. Add advice for agents with
  `guidance(text, order=N)` (existing: map 10, player 20, quests 30; use 40–90).
- **Asking the game**: `reply, version = await request(kinds.X)`, then `section(reply, "name")`
  (raises a clear `ToolError` if it's missing) and `as_list(value)` for Lua arrays. Return pydantic
  models that include `game: str = version`, so agents know which version the data is for.
- **Sending to the game**: `transport().publish(lambda state, seq: state.update(<key>=...))`. Only
  touch your own payload key. `state` is the full persistent state, so you can read your key's
  previous value to merge. Each publish uses one of 300 slots per UI session.
  The payload is rendered as Lua by `transport.to_lua`: dict keys must be Lua identifiers. For
  maps keyed by numbers (e.g. item IDs), send a list of records (`[{"item": 123, "text": ...}]`).
- `kinds.py` has the kind numbers (must match `Requests.lua`): STATE 0, QUESTS 1, ITEMS 2,
  REPUTATION 3, TRAVEL 4, KNOWLEDGE 5, STATUS 6, ITEM_DETAILS 7. All eight are taken, so new data
  goes in as a section of an existing kind (usually `STATE`).
- `install.addon_build()` is the build ID of the addon source in the repo. The installer writes
  the same ID into the installed `Build.lua`.

### Tests (`server/tests/`)
- Add the game APIs your feature needs to `stub/apis/<feature>.lua`. All files there are loaded
  automatically. Mimic the real client's behaviour, including its quirks.
- Stub items can have an `icon` (file ID); `ITEM_QUALITY_COLORS` and `C_Item.GetItemIconByID` exist.
  Font strings measure text as shown: colour codes and link data take no room, an inline texture is 12 px.
- Fixtures (`conftest.py`):
  - `addons`: an installed copy of the addon.
  - `run_client(lua)`: a scripted stub session; time advances with `client.Tick()` / `client.Wait()`.
  - `live_game(setup_lua)`: the addon ticking in real time while your test awaits your real tool
    functions. Use it for an end-to-end test of every tool.
- Done means `cd server && uv run pytest` passes and every `.lua` file passes `luac -p`.

### Checking the real API
The Forever client's own UI source and generated API docs:
`git clone --depth 1 --branch forever --filter=blob:none --sparse https://github.com/Gethe/wow-ui-source.git`
then look in `Interface/AddOns/Blizzard_APIDocumentationGenerated/`. Older global functions
(e.g. `GetInventoryItemLink`, `GetMerchantItemInfo`) aren't documented there; search the
Blizzard UI code for real uses before relying on them. Forever runs the Midnight (12.x) client
with vanilla content. Midnight's "secret values" can hide some unit and combat data in
restricted content. Wrap anything doubtful in `pcall`.

## Workstreams

### W1 Stats
- Section `stats` on kind STATE: money (copper), level, XP, XP to next level, rested XP, hearthstone
  location (`GetBindLocation`), /played total and this level.
- /played comes from `RequestTimePlayed()` → `TIME_PLAYED_MSG`, which also prints to chat. Request
  it once at login and cache it in the session. Add the time elapsed since then when answering,
  rather than spamming chat on every request.
- Tool `get_character_stats()`. Guidance: money, leveling and "where's my hearth" questions.

### W2 Inventory
- Kind ITEMS, sections:
  - `bags`: per item, itemID, name, count, quality, bag and slot.
  - `equipment`: per slot, itemID, name, item level if available, durability current/max.
  - `bank`: the bank as last seen, with a timestamp. Cached on `BANKFRAME_OPENED` and on changes
    while it's open, kept in `WowMCPDB.bank`.
- Tool `get_inventory(include_bank: bool = False)`. Guidance: "what can I vendor / do I have X / gear".

### W3 Quest objectives
- Extend the `quests` section's log entries with `objectives`: text, fulfilled, required,
  finished. Keep `get_quest_status` working unchanged.
- New tool `get_quest_log()`: each quest with title, ready-to-turn-in, and objective progress.
- (W16) Log entries also carry rewards: `xp` (`GetQuestLogRewardXP`, already scaled for the
  character's level), `money` (copper), `items` / `choices` (`{ id, name, count }`), and `rewards` =
  "loading" | "hidden" | "unknown" when not all were read. Missing values are unknown, never 0.
  `get_quest_log` asks again (up to 2× 1 s) while any are loading.

### W4 Reputations
- Section `factions` on kind REPUTATION: name, standing (localized label), progress within the
  standing (current/max), and header/group structure flattened.
- Tool `get_reputations()`.

### W5 Flight paths
- Section `flightPaths` on kind TRAVEL: the flight masters the character knows, with name, zone,
  coordinates and faction.
- First find out what's readable without a flight master open. If the data needs the flight map,
  cache it on `TAXIMAP_OPENED` in `WowMCPDB.travel` and include a timestamp.
- Tool `get_flight_paths()`. Guidance: route planning, "how do I get to X fastest".

### W6 Recipes and vendors
- Kind KNOWLEDGE, sections:
  - `recipes`: per profession, the recipes known (name, and reagents if cheap to read). Cached
    when the profession window opens.
  - `vendors`: per vendor visited, the NPC name, zone, coordinates (player position at
    `MERCHANT_SHOW`) and items with prices. Cached in `WowMCPDB.knowledge`.
- Tools `get_known_recipes(profession: str | None)` and `get_vendor_items(vendor: str | None)`.
- Each cached recipe has `item` (what it makes). `recipes` also carries `learned`: per character,
  `{ id?, name, at, profession?, item?, reagents? }` for recipes learned (event or chat line) since a
  profession window last listed them. `ns.Knowledge.Learned()` gives plans this session's learns too.
- (W17) Several plans, under tabs: payload `plan` is `{ id, list = { { id, tab, kind, profession, title,
  steps }, ... } }` (one plan `{ id, title, steps }` from before tabs is still read). `show_plan(tab=...)`
  replaces the plan in that tab or adds one (default tab `Plan`, up to 10); `kind` (questing, class,
  profession, general) picks the tab's icon and is worked out from the steps when left out. In game one plan
  is open: `WowMCPDB.plan` is its progress as before, `WowMCPDB.plans` everyone's by id.
- (W17) Section `plan` on kind STATE: the open plan's progress, or `{ none, archived, cleared }`, plus `tabs`
  (`{ id, tab, name, kind, title, steps, done, complete, archived, shown }`: `tab` is the name the server sent,
  which its tools match on; `name` the player's own, if they renamed it), `removed` (ids of the plans the player
  removed: `get_plan_progress`, and `show_plan` at the limit, drop them from the server's state without sending
  anything) and `seen` (`WowMCPDB.planSeen`, the id of the last plan payload received: another than the
  server's means its newest hasn't arrived).
- (W17) `show_plan` is async: it looks up named `have` items with `items.look_up` (shared with
  `get_item_details`) and refuses a plan whose IDs are other items. In game, a `have` whose ID is
  another item counts the named item.
- (W17) A step can name its NPCs: `npcs = { { id, name, display } }` (key left out when there are none). Hovering
  the step, and only then, shows their models beside the tooltip (shared `PlayerModel` frames, `SetCreature`).

### W7 Messages
- Payload key `messages`: a list (newest last, at most 10) of
  `{ id = <int, increasing>, text = <string>, style = "chat" | "center" | "both", from = <string> }`.
  The addon shows the entries with an `id` it hasn't shown yet (track it in `WowMCPDB.messageId`).
  A `/reload` must not repeat old messages.
- Center style: a raid-warning-style message on screen. Chat: printed with the sender's name.
- Tool `show_message(text, style="chat")`, plus `publish_message(text, style, sender) -> int` in
  `tools/messages.py`. That helper does the publish; W8's bridge imports it.
- In the addon, expose `ns.ShowMessage(text, style, sender)` (reminders and others use it).

### W8 /ai chat
- `Chat.lua`: `/ai <question>` sends `{ question, player = <name> }` as a stream **post**. Post is a new
  `ns.Stream.Post(value)` in `Stream.lua`: the same framing as `Send`, but with magic `WM\2` and an
  increasing id that's unique per UI session (include a session token so a `/reload` doesn't
  clash). Show "Asking…" in chat.
- `stream.py`: decode posts. `bridge.py`: a long-running service (`wow-mcp-bridge`) that watches
  `out/`. For each new question it runs an agent with the wow MCP tools available, e.g.
  `claude -p` with this server in its MCP config, and web search allowed for lookups. It then shows
  the answer with W7's `publish_message` (sender "Claude"), plus any markers the agent placed
  itself via `show_on_map`.
- A systemd user unit (or documented command) to keep the bridge running.
- Until W7 is merged, test against W7's contract (the `messages` payload shape) rather than its code.

### W9 Routes + navigator arrow
- Payload key `routes`: `{ id, title, loop, stops = [{ zone, x, y, label, note, mapID }] }`, one active
  route. Empty `stops` clears it.
- On the world map, draw numbered stop pins with lines between them. Use your own data
  provider and templates in `Routes.xml`; don't touch Map.lua's pins.
- Navigator: a small movable frame with an arrow pointing at the current stop and the distance in
  yards. It advances when within about 12 yards and wraps on `loop`. It also sets the game's waypoint
  on the current stop (`ns.Map.SetWaypoint`). Player facing (`GetPlayerFacing`) and position aren't
  available in instances, so show "no position here" there.
- `/mcp route` commands: show, next, prev, stop.
- Tools `show_route(stops, title, loop=False)` and `clear_route()`. Guidance: turn-in routes and
  farming loops.

### W10 Area highlights
- Payload key `areas`: `{ id, areas = [{ zone, x, y, radius, label, color }] }`, where `radius` is in
  map percent. Polygons are optional if you have time.
- Draw shaded, labelled regions on the world map: your own data provider and templates in
  `Areas.xml`. Show them projected onto parent maps too (`ns.Map.Project`).
- Tools `highlight_areas(areas)` and `clear_areas()`. Guidance: "where are mob X / herb Y".

### W11 Reminders
- Payload key `reminders`: a list of `{ id, text, at, zone, subzone, near }`:
  - `at` is a unix time; or
  - `zone` / `subzone` names; or
  - `near = { zone, x, y, yards }`.
- Each fires once, through `ns.ShowMessage` (style "both") with a sound. Fired IDs are kept in
  `WowMCPDB.reminders` so they don't fire again after a `/reload`.
- Time checks every few seconds. Place checks on zone-change events and a slow timer.
- Tools `set_reminder(text, in_minutes | at_time | zone | near)`, `list_reminders()` and
  `cancel_reminder(id)`. The server keeps the list in state; fired ones can be reported via a
  STATE-kind section if useful.

### W12 Item tooltip notes
- Payload key `itemNotes`: `{ id, notes = [{ item = <itemID>, text }] }`. Add the note as a coloured
  line on item tooltips: check the Forever client for `TooltipDataProcessor.AddTooltipPostCall`
  with `Enum.TooltipDataType.Item`, or tooltip hooks.
- Tools `set_item_notes(notes, replace=False)` (merges by item) and `clear_item_notes()`.

### W13 Version source hints
- `tools/sources.py` only. At import, work out the game version (`app.addons_dir()`,
  `game.situation`), then add `guidance(order=5)`: which sites have data for that version and
  how to spot information for a different one. For Forever: Forever-specific pages first;
  classic-era data as an explicit fallback. Research which sources cover Forever.

### W14 Status / version tool
- Section `status` on kind STATUS: `{ build = ns.BUILD, live, liveProblem, slotsLeft, slotCount,
  seq, req }`.
- Tool `get_status()`: the in-game build compared with the installed `Build.lua` and with
  `install.addon_build()`. The advice is "/reload" (installed is newer than what's loaded),
  "run wow-mcp-install" (the repo is newer than what's installed) or "restart WoW" (files missing).
  Also report live updates, slots left, and the game version. If the game doesn't answer, still
  return what the server knows, with the reason.

## Agents
Each agent works in its own git worktree and branch, owns the files of its workstreams, and
commits its work there.

| Agent | Workstreams |
|---|---|
| A1 | W1 Stats, W4 Reputations |
| A2 | W2 Inventory |
| A3 | W3 Quest objectives |
| A4 | W5 Flight paths, W6 Recipes and vendors |
| A5 | W7 Messages, W11 Reminders |
| A6 | W8 `/ai` chat |
| A7 | W9 Routes, W10 Area highlights |
| A8 | W12 Item tooltip notes, W13 Version source hints, W14 Status |

## Integration (integrator only)
1. Merge workstreams one at a time and run the full test suite after each.
2. Install with `uv run wow-mcp-install <AddOns>`, `/reload` in game, and verify the feature live
   with the player, one feature at a time.
3. Update `README.md` (tools table, in-game commands) for each merged feature.
4. Shared files: `Core.lua`, `Requests.lua`, `Map.lua`, `Map.xml`, `WowMCP.toc`, `app.py`, `kinds.py`,
   `transport.py`, `install.py`, `server.py`, `conftest.py`, `stub/client.lua`, the existing
   `stub/apis/*.lua` files (except `quests.lua`, which W3 owns), `pyproject.toml`, `README.md`. If a workstream needs a change here, it
   reports it instead of making it.
