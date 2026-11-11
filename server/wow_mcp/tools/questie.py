"""Where NPCs, objects and quests are, from the Questie addon's database when the player has it
installed (wow_mcp/questie.py). An optional integration: without Questie the tools say so and the
agent looks things up on the web as before."""

from typing import Annotated, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from wow_mcp import questie
from wow_mcp.app import addons_dir, guidance, tool

guidance(
    "For where an NPC, object or quest is (trainers, vendors, quest givers, mobs, chests, herbs on a quest), call "
    "find_npc, find_object or find_quest first. They read the Questie addon's database for this game version "
    "from disk: exact zone-map coordinates and uiMapIDs you can pass straight to show_on_map (map_id) or "
    "highlight_areas. If they say Questie isn't available or find nothing, look it up as described in the "
    "sources guidance instead. Say which source an answer came from when it matters.",
    order=6,
)

MAX_POINTS = 6  # coordinates listed per zone; the rest are counted

NOT_AVAILABLE = (
    "Questie's database isn't available (the QuestieDB addon isn't installed for this game version, or its "
    "format is one this server doesn't read). Look it up on the web instead (see the sources guidance)."
)

def _db() -> questie.QuestieDB | None:
    folder = addons_dir()
    if not folder:
        return None
    try:
        game_folder = folder.resolve().parents[1].name
    except IndexError:
        game_folder = None
    try:
        return questie.load(folder, game_folder)
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


@tool(annotations=ToolAnnotations(title="Find an NPC (Questie)", read_only_hint=True, destructive_hint=False, open_world_hint=False))
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


@tool(annotations=ToolAnnotations(title="Find an object (Questie)", read_only_hint=True, destructive_hint=False, open_world_hint=False))
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


@tool(annotations=ToolAnnotations(title="Find a quest (Questie)", read_only_hint=True, destructive_hint=False, open_world_hint=False))
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
