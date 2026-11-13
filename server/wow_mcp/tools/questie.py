"""Where NPCs, objects, items and quests are, which quests the player can pick up nearby, and where the
quests in their log are done, from the Questie addon's database (wow_mcp/questie.py).

An optional integration: these tools are only offered when QuestieDB is installed for this game version
(checked when the server starts). If it goes missing later, they say so and the agent looks things up on
the web as before."""

import math
from typing import Annotated, Any, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from wow_mcp import kinds, questie
from wow_mcp.app import addons_dir, as_list, guidance, request, section, tool


def _game_folder() -> str | None:
    folder = addons_dir()
    try:
        return folder.resolve().parents[1].name if folder else None
    except IndexError:
        return None


INSTALLED = questie.installed(addons_dir(), _game_folder())


def _register(**options: Any):
    """Offer a tool only when Questie is installed, so agents without it never see these."""
    return tool(**options) if INSTALLED else (lambda fn: fn)


def _read_only(title: str) -> ToolAnnotations:
    return ToolAnnotations(title=title, read_only_hint=True, destructive_hint=False, open_world_hint=False)


if INSTALLED:
    guidance(
        "The player has the Questie addon, so this game version's quest, NPC, object and item database is on disk. "
        "Use it before the web: find_npc, find_object and find_item for where things are (trainers, vendors, quest "
        "givers, mobs, herbs, where an item drops or is sold), find_quest for a quest's givers, objectives and "
        "chain, find_available_quests for what the player can pick up nearby (it checks their level, race, class "
        "and finished quests in game), and get_quest_log_locations for where the quests in their log are done, "
        "with areas ready for highlight_areas and points for show_on_map or show_route. Coordinates are zone-map "
        "0-100 with uiMapIDs (pass map_id). If a tool says Questie isn't available or finds nothing, fall back to "
        "the sources guidance. Questie's data can miss or misplace newer content; say which source an answer "
        "came from when it matters.",
        order=6,
    )

MAX_POINTS = 6  # coordinates listed per zone; the rest are counted

NOT_AVAILABLE = (
    "Questie's database isn't available (the QuestieDB addon isn't installed for this game version, or its "
    "format is one this server doesn't read). Look it up on the web instead (see the sources guidance)."
)

def _db() -> questie.QuestieDB | None:
    try:
        return questie.load(addons_dir(), _game_folder())
    except OSError:
        return None


def _in_zone(zone: str, wanted: str) -> bool:
    """'Shen' matches "Shen'dralas" and 'orgrimmar' matches 'Orgrimmar', but 'shen' doesn't match 'Ashenvale'."""
    zone, wanted = zone.lower(), wanted.lower().strip()
    return zone == wanted or zone.startswith(wanted) or f" {wanted}" in zone


def _places(db: questie.QuestieDB, entity: questie.Entity, zone: str | None = None) -> list[str]:
    lines = []
    for spawn in db.spawns(entity):
        if zone and not (_in_zone(spawn.zone, zone) or (spawn.subzone and _in_zone(spawn.subzone, zone))):
            continue
        name = f"{spawn.zone} ({spawn.subzone})" if spawn.subzone else spawn.zone
        map_id = f", map_id {spawn.map_id}" if spawn.map_id else ""
        if not spawn.points:
            lines.append(f"    {name} (inside; no map position{map_id})")
            continue
        points = "; ".join(f"{x}, {y}" for x, y in spawn.points[:MAX_POINTS])
        more = f" (+{len(spawn.points) - MAX_POINTS} more)" if len(spawn.points) > MAX_POINTS else ""
        lines.append(f"    {name} {points}{more}{map_id}")
    return lines


def _source(db: questie.QuestieDB) -> str:
    return f"From Questie's database (QuestieDB, {db.flavour}, contract {db.contract})."


def _words(query: str) -> list[str]:
    return [w for w in query.lower().split() if w]


@_register(annotations=_read_only("Find an NPC (Questie)"))
def find_npc(
    query: Annotated[
        str,
        Field(max_length=80, description="Words in the NPC's name or title, e.g. 'Karolek', 'leatherwork' (finds "
              "'Expert Leatherworker'), or an NPC ID. May be empty when role or zone narrows the search, e.g. "
              "role 'flight master' in zone 'Orgrimmar' (their titles vary: 'Wind Rider Master', 'Gryphon Master')."),
    ],
    faction: Annotated[
        Literal["Alliance", "Horde"] | None,
        Field(description="Only NPCs friendly to this faction (the player's, for trainers and vendors)."),
    ] = None,
    zone: Annotated[str | None, Field(max_length=60, description="Only NPCs in this zone, e.g. 'Orgrimmar'.")] = None,
    role: Annotated[
        Literal["trainer", "vendor", "flight master", "quest giver", "innkeeper", "banker", "auctioneer",
                "stable master", "repairs"] | None,
        Field(description="Only NPCs with this function."),
    ] = None,
    limit: Annotated[int, Field(ge=1, le=30)] = 10,
) -> str:
    """Find NPCs by name or title in the Questie addon's database: where they stand (zone, x, y,
    uiMapID), level, which faction they're friendly to, and which quests they start and end."""
    if not query.strip() and not (role or zone):
        return "Give a name or title to search for, or a role or zone."
    db = _db()
    if db is None:
        return NOT_AVAILABLE
    ids = [int(query)] if query.strip().isdigit() else db.search("Npc", _words(query), ("name", "subName"))
    side = {"Alliance": "A", "Horde": "H"}.get(faction or "")
    lines, skipped = [], 0
    for id in ids:
        npc = db.get("Npc", id)
        if npc is None:
            continue
        friendly = npc.get("friendlyToFaction") or ""
        if side and side not in friendly:
            continue
        roles = db.roles(npc)
        if role and role not in roles:
            continue
        places = _places(db, npc, zone)
        if zone and not places:
            continue
        if not npc.get("spawns") and not zone:
            skipped += 1  # placeholders such as "World Leatherworking Trainer" have no spawns
            continue
        if len(lines) >= limit:
            lines.append("(more matches; narrow the search or raise limit)")
            break
        title = f" <{npc.get('subName')}>" if npc.get("subName") else ""
        level = npc.get("minLevel")
        if level and npc.get("maxLevel") and npc.get("maxLevel") != level:
            level = f"{level}-{npc.get('maxLevel')}"
        side_text = {"A": "friendly to Alliance", "H": "friendly to Horde", "AH": "friendly to both factions"}.get(
            friendly, "hostile to both factions")
        details = [f"level {level}" if level else None, side_text, ", ".join(roles) or None]
        lines.append(f"- {npc.get('name')}{title} (NPC {id}; {'; '.join(d for d in details if d)})")
        lines += places or ["    (no known position)"]
        for key, label in (("questStarts", "starts"), ("questEnds", "ends")):
            quests = questie._lua_list(npc.get(key))
            if quests:
                names = [f"{db.name('Quest', q) or '?'} ({q})" for q in quests[:8]]
                lines.append(f"    {label} quests: {', '.join(names)}{' ...' if len(quests) > 8 else ''}")
    if not lines:
        note = " (only unplaced placeholder NPCs matched)" if skipped else ""
        return f"No NPC in Questie's database matches '{query}' with those filters{note}. " \
               "Try another word, fewer filters, or the web."
    return "\n".join([_source(db), *lines])


@_register(annotations=_read_only("Find an object (Questie)"))
def find_object(
    query: Annotated[
        str,
        Field(min_length=1, max_length=80, description="Words in the object's name, e.g. 'Peacebloom', "
              "'Mailbox', 'Wanted Poster', or an object ID."),
    ],
    zone: Annotated[str | None, Field(max_length=60, description="Only objects in this zone.")] = None,
    limit: Annotated[int, Field(ge=1, le=30)] = 10,
) -> str:
    """Find world objects (herbs, ore, chests, quest objects, wanted posters, mailboxes) in the Questie addon's
    database: where they are and which quests they start or end."""
    db = _db()
    if db is None:
        return NOT_AVAILABLE
    ids = [int(query)] if query.strip().isdigit() else db.search("Object", _words(query))
    lines = []
    for id in ids:
        obj = db.get("Object", id)
        if obj is None:
            continue
        places = _places(db, obj, zone)
        if not places:
            continue
        if len(lines) >= limit:
            lines.append("(more matches; narrow the search or raise limit)")
            break
        lines.append(f"- {obj.get('name')} (object {id})")
        lines += places
        for key, label in (("questStarts", "starts"), ("questEnds", "ends")):
            quests = questie._lua_list(obj.get(key))
            if quests:
                lines.append(f"    {label} quests: " + ", ".join(f"{db.name('Quest', q) or '?'} ({q})" for q in quests[:8]))
    if not lines:
        return f"No placed object in Questie's database matches '{query}'. Try another word or the web."
    return "\n".join([_source(db), *lines])


def _who(db: questie.QuestieDB, groups, label: str) -> list[str]:
    """Quest starters/finishers: {npcIds, objectIds, itemIds} with where each one is."""
    groups = questie._lua_list(groups) if not isinstance(groups, dict) else [groups.get(i) for i in (1, 2, 3)]
    lines = []
    for kind, ids in zip(("Npc", "Object", "Item"), groups):
        for id in questie._lua_list(ids):
            if kind == "Item":
                lines.append(f"  {label}: item {id} (it starts the quest)")
                continue
            entity = db.get(kind, id)
            if entity is None:
                continue
            noun = "NPC" if kind == "Npc" else "object"
            lines.append(f"  {label}: {entity.get('name')} ({noun} {id})")
            lines += _places(db, entity) or ["    (no known position)"]
    return lines


@_register(annotations=_read_only("Find a quest (Questie)"))
def find_quest(
    query: Annotated[
        str,
        Field(min_length=1, max_length=80, description="Words in the quest's name, or its quest ID."),
    ],
    limit: Annotated[int, Field(ge=1, le=10, description="Quests to describe in full.")] = 3,
) -> str:
    """Look up quests in the Questie addon's database: level, who starts and ends them and where they stand,
    the objective text, which NPCs and objects the objectives need and where they are, and prerequisites."""
    db = _db()
    if db is None:
        return NOT_AVAILABLE
    ids = [int(query)] if query.strip().isdigit() else db.search("Quest", _words(query))
    lines = []
    for count, id in enumerate(ids):
        quest = db.get("Quest", id)
        if quest is None:
            continue
        if count >= limit:
            rest = ", ".join(f"{db.name('Quest', q)} ({q})" for q in ids[limit:limit + 15])
            lines.append(f"Other matches: {rest}{' ...' if len(ids) > limit + 15 else ''}")
            break
        level = quest.get("questLevel")
        needs = quest.get("requiredLevel")
        lines.append(f"- {quest.get('name')} (quest {id}; level {level}{f', needs {needs}' if needs else ''})")
        lines += _who(db, quest.get("startedBy"), "starts at")
        lines += _who(db, quest.get("finishedBy"), "ends at")
        for text in questie._lua_list(quest.get("objectivesText")):
            lines.append(f"  objective: {text}")
        objectives = quest.get("objectives") or {}
        groups = questie._lua_list(objectives) if isinstance(objectives, list) else \
            [objectives.get(i) for i in (1, 2)]
        for kind, group in zip(("Npc", "Object"), groups):
            for entry in questie._lua_list(group):
                target = questie._lua_list(entry)
                if not target or not isinstance(target[0], int):
                    continue
                entity = db.get(kind, target[0])
                if entity is None:
                    continue
                noun = "kill/use NPC" if kind == "Npc" else "object"
                lines.append(f"  needs {noun}: {entity.get('name')} ({target[0]})")
                lines += _places(db, entity) or ["    (no known position)"]
        pre = questie._lua_list(quest.get("preQuestSingle")) + questie._lua_list(quest.get("preQuestGroup"))
        if pre:
            lines.append("  after: " + ", ".join(f"{db.name('Quest', q) or '?'} ({q})" for q in pre[:8]))
        after = quest.get("nextQuestInChain")
        if after:
            lines.append(f"  leads to: {db.name('Quest', after) or '?'} ({after})")
    if not lines:
        return f"No quest in Questie's database matches '{query}'. Try another word or the web."
    return "\n".join([_source(db), *lines])


MAX_SOURCES = 8  # NPCs or objects listed per item source; the rest are counted


def _sources(db: questie.QuestieDB, kind: str, ids: list[int], label: str, zone: str | None) -> list[str]:
    lines, shown = [], 0
    for id in ids:
        entity = db.get(kind, id)
        if entity is None:
            continue
        places = _places(db, entity, zone)
        if zone and not places:
            continue
        if shown == MAX_SOURCES:
            lines.append(f"    (+{len(ids) - shown} more {label})")
            break
        shown += 1
        noun = "NPC" if kind == "Npc" else "object"
        title = f" <{entity.get('subName')}>" if kind == "Npc" and entity.get("subName") else ""
        lines.append(f"  {label}: {entity.get('name')}{title} ({noun} {id})")
        lines += places or ["    (no known position)"]
    return lines


@_register(annotations=_read_only("Find an item's sources (Questie)"))
def find_item(
    query: Annotated[
        str, Field(min_length=1, max_length=80, description="Words in the item's name, or its item ID."),
    ],
    zone: Annotated[str | None, Field(max_length=60, description="Only sources in this zone.")] = None,
    limit: Annotated[int, Field(ge=1, le=10, description="Items to describe.")] = 3,
) -> str:
    """Where an item comes from, per the Questie addon's database: the NPCs that drop it, the objects it's in
    (herbs, ore, chests, quest objects), the vendors that sell it, the quests that reward it, and the quest it
    starts. Each source with where it is. Lists sources Questie knows about, not drop chances."""
    db = _db()
    if db is None:
        return NOT_AVAILABLE
    ids = [int(query)] if query.strip().isdigit() else db.search("Item", _words(query))
    lines: list[str] = []
    for count, id in enumerate(ids):
        item = db.get("Item", id)
        if item is None:
            continue
        if count >= limit:
            rest = ", ".join(f"{db.name('Item', i)} ({i})" for i in ids[limit:limit + 15])
            lines.append(f"Other matches: {rest}{' ...' if len(ids) > limit + 15 else ''}")
            break
        lines.append(f"- {item.get('name')} (item {id})")
        before = len(lines)
        lines += _sources(db, "Npc", questie._lua_list(item.get("vendors")), "sold by", zone)
        lines += _sources(db, "Npc", questie._lua_list(item.get("npcDrops")), "dropped by", zone)
        lines += _sources(db, "Object", questie._lua_list(item.get("objectDrops")), "found in", zone)
        for container in questie._lua_list(item.get("itemDrops"))[:MAX_SOURCES]:
            lines.append(f"  inside item: {db.name('Item', container) or '?'} ({container})")
        rewards = questie._lua_list(item.get("questRewards"))
        if rewards:
            lines.append("  reward from: " + ", ".join(f"{db.name('Quest', q) or '?'} ({q})" for q in rewards[:8]))
        if start := item.get("startQuest"):
            lines.append(f"  starts quest: {db.name('Quest', start) or '?'} ({start})")
        if len(lines) == before:
            lines.append("  (no source in Questie's database" + (f" in {zone})" if zone else ")"))
    if not lines:
        return f"No item in Questie's database matches '{query}'. Try another word or the web."
    return "\n".join([_source(db), *lines])


def _completed(quests: dict) -> set[int]:
    """Turned-in quests from the game's QUESTS reply: ascending IDs sent as gaps (Quests.lua)."""
    done, total = set(), 0
    for gap in as_list(quests.get("completedGaps")):
        total += gap
        done.add(total)
    return done


def _character(state: dict) -> questie.Character:
    character = section(state, "character")
    return questie.Character(level=int(character.get("level") or 1), faction=character.get("faction"),
                             race=character.get("race"), player_class=character.get("class"))


def _nearest(spawns: list[questie.Spawn], map_id: int | None, here: tuple[float, float] | None,
             zone: str | None) -> tuple[float, questie.Spawn, tuple[float, float]] | None:
    """The spawn point closest to `here` on the player's map (or any point in `zone`), with its distance in
    map percent (0 when there is no position to measure from)."""
    best = None
    for spawn in spawns:
        if zone:
            if not (_in_zone(spawn.zone, zone) or (spawn.subzone and _in_zone(spawn.subzone, zone))):
                continue
        elif spawn.map_id is None or spawn.map_id != map_id:
            continue
        for point in spawn.points:
            distance = math.dist(point, here) if here and not zone else 0.0
            if best is None or distance < best[0]:
                best = (distance, spawn, point)
    return best


@_register(annotations=_read_only("Quests to pick up nearby (Questie)"))
async def find_available_quests(
    zone: Annotated[
        str | None,
        Field(max_length=60, description="Look in this zone instead of the one the player is in, e.g. 'Westfall'."),
    ] = None,
    include_gray: Annotated[bool, Field(description="Also list quests too low to give much XP.")] = False,
    limit: Annotated[int, Field(ge=1, le=40)] = 15,
) -> str:
    """Quests the player's character can pick up now in their zone (or the given one), closest first: who gives
    each and where, its level, and what the objectives are. Uses the Questie addon's database and checks in game
    which quests this character has done or has in their log, plus level, race, class and prerequisites. Distances
    are in percent of the zone map; pass the givers to show_on_map with nearest_first for walking distances."""
    db = _db()
    if db is None:
        return NOT_AVAILABLE
    state, _ = await request(kinds.STATE)
    quests_reply, _ = await request(kinds.QUESTS)
    location = section(state, "location")
    who = _character(state)
    quests = section(quests_reply, "quests")
    completed = _completed(quests)
    in_log = {q["id"] for q in as_list(quests.get("log")) if isinstance(q, dict) and isinstance(q.get("id"), int)}
    blacklist = questie.read_blacklist(addons_dir())
    map_id = location.get("mapID")
    x, y = location.get("x"), location.get("y")
    here = (float(x), float(y)) if isinstance(x, (int, float)) and isinstance(y, (int, float)) else None
    if not zone and not map_id:
        return "The game didn't say which map the player is on (in an instance?). Pass a zone name."
    gray = questie.gray_below(who.level)
    side = {"Alliance": "A", "Horde": "H"}.get(who.faction or "")
    found, skipped_gray = [], 0
    for id in db.ids("Quest"):
        quest = db.get("Quest", id, "name", "startedBy", "requiredLevel", "questLevel", "requiredRaces",
                       "requiredClasses", "preQuestGroup", "preQuestSingle", "exclusiveTo", "requiredSkill",
                       "parentQuest", "requiredMaxLevel", "objectivesText")
        if quest is None or questie.why_unavailable(db, quest, who, completed, in_log, blacklist):
            continue
        starters = quest.get("startedBy")
        groups = [starters.get(i) for i in (1, 2)] if isinstance(starters, dict) else questie._lua_list(starters)[:2]
        best = None
        for kind, ids in zip(("Npc", "Object"), groups):
            for giver_id in questie._lua_list(ids):
                giver = db.get(kind, giver_id, "name", "spawns", "friendlyToFaction")
                if giver is None:
                    continue
                if kind == "Npc" and side and side not in (giver.get("friendlyToFaction") or ""):
                    continue
                near = _nearest(db.spawns(giver), map_id, here, zone)
                if near and (best is None or near[0] < best[0]):
                    best = (near[0], near[1], near[2], kind, giver)
        if best is None:
            continue
        level = quest.get("questLevel") or 0
        if 0 < level <= gray and not include_gray:
            skipped_gray += 1
            continue
        found.append((best, quest))
    if not found:
        where = zone or "this zone"
        return (f"{_source(db)}\nNo quest to pick up in {where} for this character as far as Questie knows"
                f"{f' ({skipped_gray} gray ones left out; include_gray lists them)' if skipped_gray else ''}. "
                "Questie may not know every quest; the web can say more.")
    found.sort(key=lambda f: (f[0][0], f[1].get("questLevel") or 0))
    heading = (f"Quests {who.faction or ''} level {who.level} can pick up in {zone or found[0][0][1].zone}, closest "
               f"first ({len(found)} in all{f'; {skipped_gray} gray left out' if skipped_gray else ''}). Checked in "
               "game: done and in-log quests.")
    lines = [_source(db), heading]
    for (distance, spawn, (px, py), kind, giver), quest in found[:limit]:
        noun = "NPC" if kind == "Npc" else "object"
        far = f", {distance:.0f}% of the map away" if here and not zone else ""
        need = quest.get("requiredLevel")
        lines.append(f"- {quest.get('name')} (quest {quest.id}; level {quest.get('questLevel')}"
                     f"{f', needs {need}' if need else ''})")
        lines.append(f"    from {giver.get('name')} ({noun} {giver.id}): {spawn.zone} {px}, {py}"
                     f"{f', map_id {spawn.map_id}' if spawn.map_id else ''}{far}")
        for text in questie._lua_list(quest.get("objectivesText"))[:1]:
            lines.append(f"    {text[:200]}")
    if len(found) > limit:
        lines.append(f"(+{len(found) - limit} more; raise limit)")
    lines.append("Not checked: reputation and holiday requirements, and quests Questie doesn't know.")
    return "\n".join(lines)


def _areas(db: questie.QuestieDB, entity: questie.Entity) -> list[tuple[questie.Spawn, questie.Cluster]]:
    return [(spawn, c) for spawn in db.spawns(entity) for c in questie.clusters(spawn.points)]


def _area_lines(db: questie.QuestieDB, entities: list[questie.Entity], map_id: int | None,
                max_areas: int = 4) -> list[str]:
    """Spawn areas of the given NPCs/objects together, those on the player's map first, biggest first."""
    areas = [a for e in entities for a in _areas(db, e)]
    areas.sort(key=lambda a: (a[0].map_id != map_id, -a[1].count))
    lines = []
    for spawn, c in areas[:max_areas]:
        mid = f", map_id {spawn.map_id}" if spawn.map_id else ""
        lines.append(f"      {spawn.zone} around {c.x}, {c.y} radius {c.radius} ({c.count} spawn"
                     f"{'s' if c.count != 1 else ''}{mid})")
    if len(areas) > max_areas:
        lines.append(f"      (+{len(areas) - max_areas} smaller or farther areas)")
    inside = [s for e in entities for s in db.spawns(e) if not s.points]
    if not areas and inside:
        lines.append(f"      inside {inside[0].zone} (an instance; no map position)")
    return lines or ["      (no known position)"]


@_register(annotations=_read_only("Where the quest log's quests are done (Questie)"))
async def get_quest_log_locations() -> str:
    """For each quest in the player's log: where its objectives are done (the mobs to kill, objects to use,
    where needed items drop or are found, places to explore) as areas with a centre and radius ready for
    highlight_areas, and, for quests ready to turn in, where the turn-in NPC stands. Objectives the game says are
    finished are marked. From the Questie addon's database; areas on the player's current map come first."""
    db = _db()
    if db is None:
        return NOT_AVAILABLE
    state, _ = await request(kinds.STATE)
    quests_reply, _ = await request(kinds.QUESTS)
    map_id = section(state, "location").get("mapID")
    log = [q for q in as_list(section(quests_reply, "quests").get("log")) if isinstance(q, dict)]
    if not log:
        return "The player's quest log is empty."
    lines = [_source(db), "Areas: centre x, y and radius in percent of the zone map (as highlight_areas takes)."]
    for entry in log:
        id = entry.get("id")
        quest = db.get("Quest", id) if isinstance(id, int) else None
        title = entry.get("title") or (quest and quest.get("name")) or f"quest {id}"
        if quest is None:
            lines.append(f"- {title} (quest {id}): not in Questie's database; look it up on the web.")
            continue
        ready = bool(entry.get("ready"))
        lines.append(f"- {title} (quest {id}{'; ready to turn in' if ready else ''})")
        if ready:
            lines += _who(db, quest.get("finishedBy"), "turn in at")
            continue
        progress = [o for o in as_list(entry.get("objectives")) if isinstance(o, dict)]

        finished = [(o.get("text") or "").lower() for o in progress if o.get("finished")]

        def done(name: str | None, finished: list[str] = finished) -> bool:
            name = (name or "").lower()
            return bool(name) and any(name in text for text in finished)

        objectives = quest.get("objectives") or {}
        groups = [objectives.get(i) for i in (1, 2, 3)] if isinstance(objectives, dict) \
            else questie._lua_list(objectives)[:3]
        groups += [None] * (3 - len(groups))
        for kind, group in zip(("Npc", "Object", "Item"), groups):
            for target in questie._lua_list(group):
                target = questie._lua_list(target)
                if not target or not isinstance(target[0], int):
                    continue
                entity = db.get(kind, target[0])
                if entity is None:
                    continue
                name = entity.get("name")
                if kind == "Item":
                    sources = [e for e in (db.get("Npc", n) for n in questie._lua_list(entity.get("npcDrops"))) if e]
                    sources += [e for e in (db.get("Object", o) for o in questie._lua_list(entity.get("objectDrops")))
                                if e]
                    label = f"collect {name} (item {target[0]})"
                    if sources:
                        label += " from " + ", ".join(dict.fromkeys(str(e.get("name")) for e in sources[:4]))
                else:
                    sources = [entity]
                    label = f"{'kill or use' if kind == 'Npc' else 'use'} {name} ({'NPC' if kind == 'Npc' else 'object'} {target[0]})"
                if done(name):
                    lines.append(f"    done: {label}")
                    continue
                lines.append(f"    {label}")
                lines += _area_lines(db, sources, map_id)
        trigger = quest.get("triggerEnd")
        if trigger:
            trigger = questie._lua_list(trigger) if not isinstance(trigger, dict) else [trigger.get(1), trigger.get(2)]
            text, spots = (trigger + [None, None])[:2]
            pseudo = questie.Entity("Npc", 0, {questie.NPC["spawns"]: spots or {}})
            lines.append(f"    {text or 'go to the place'}")
            lines += _area_lines(db, [pseudo], map_id)
        if len(lines) and lines[-1].startswith("- "):
            lines.append("    (no objectives with places; talk or deliver to:)")
            lines += _who(db, quest.get("finishedBy"), "turn in at")
    return "\n".join(lines)
