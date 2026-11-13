"""Reading the Questie addon's database from disk (questie.py) and the lookup tools on it (tools/questie.py).
The QuestieDB here is a tiny one written the way QuestieDB's baked .toc is: CBOR rows, base64, and a zlib'd
ID list, with long values split into "~N~" chunks."""

import asyncio
import base64
import importlib
import struct
import zlib

import pytest

from wow_mcp import app, questie
from wow_mcp.tools import questie as tools


def cbor(value) -> bytes:
    def head(major, n):
        if n < 24:
            return bytes([major << 5 | n])
        for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
            if n < 256 ** size:
                return bytes([major << 5 | info]) + n.to_bytes(size, "big")
        raise ValueError(n)

    if value is None:
        return b"\xf6"
    if value is True or value is False:
        return b"\xf5" if value else b"\xf4"
    if isinstance(value, int):
        return head(0, value) if value >= 0 else head(1, -1 - value)
    if isinstance(value, float):
        return b"\xfb" + struct.pack(">d", value)
    if isinstance(value, str):
        raw = value.encode()
        return head(3, len(raw)) + raw
    if isinstance(value, bytes):  # the client writes strings as byte strings
        return head(2, len(value)) + value
    if isinstance(value, list):
        return head(4, len(value)) + b"".join(cbor(v) for v in value)
    if isinstance(value, dict):
        return head(5, len(value)) + b"".join(cbor(k) + cbor(v) for k, v in value.items())
    raise TypeError(value)


def b64(value) -> str:
    return base64.b64encode(cbor(value)).decode()


def entity_lines(kind: str, id: int, fields: dict[int, object]) -> list[str]:
    scalars = {k: v for k, v in fields.items() if not isinstance(v, (list, dict))}
    tables = {k: v for k, v in fields.items() if isinstance(v, (list, dict))}
    scalars["p"] = sum(2 ** (k - 1) for k in tables)
    lines = [f"## X-{kind}-{id}-S: {b64(scalars)}"]
    lines += [f"## X-{kind}-{id}-{k}: {b64(v)}" for k, v in tables.items()]
    return lines


NPCS = {
    3365: {1: b"Karolek", 4: 35, 5: 35, 7: {1637: [[62.81, 44.15]]}, 9: 1637, 11: [7001], 13: b"H",
           14: b"Expert Leatherworker", 15: 16},
    3310: {1: b"Doras", 4: 55, 5: 55, 7: {1637: [[45.12, 63.89]]}, 13: b"H", 14: b"Wind Rider Master", 15: 10},
    1632: {1: b"Adele Fielder", 4: 22, 5: 22, 7: {12: [[46.38, 62.05]]}, 13: b"A", 14: b"Journeyman Leatherworker",
           15: 16},
    5040: {1: b"World Leatherworking Trainer", 13: b"AH", 14: b"Leatherworking Trainer", 15: 16},  # no spawns
    252691: {1: b"Adarien", 7: {2657: [[42.79, 70.48], [44.0, 72.36]]}, 14: b"Innkeeper", 15: 128},
    883: {1: b"Deer", 4: 1, 5: 1, 7: {433: [[27.13, 21.82]]}, 13: b"AH"},
    9001: {1: b"Gath'Ilzogg", 7: {44: [[-1, -1]]}},
    3125: {1: b"Clattering Scorpid", 7: {1637: [[10.0, 10.0], [11.0, 12.0], [12.0, 10.5], [80.0, 80.0]]}, 13: b""},
    3143: {1: b"Gornek", 7: {1637: [[42.0, 68.0]]}, 13: b"H", 15: 2},
    4000: {1: b"Ally Giver", 7: {1637: [[41.0, 68.0]]}, 13: b"A", 15: 2},
}
OBJECTS = {60: {1: b"Wanted: Gath'Ilzogg", 2: [7001], 4: {44: [[24.53, 46.16]]}}}
ITEMS = {5466: {1: b"Scorpid Stinger", 2: [3125], 14: [3143], 6: [7001]}}
H, A = 2 + 16 + 32 + 128 + 2 ** 33, 1 + 4 + 8 + 64 + 2 ** 32  # all Horde / all Alliance races
QUESTS = {
    7001: {1: b"WANTED: Gath'Ilzogg", 2: [None, [60]], 3: [[3365]], 4: 15, 5: 26,
           8: [b"Kill Gath'Ilzogg and bring his head."], 10: {1: [[9001]]}, 13: [7000], 22: 7002},
    7000: {1: b"Gathering Idols", 2: [[3365]], 3: [[3365]], 5: 18},
    7002: {1: b"Gath's Revenge", 5: 27},
    # Quests Gornek and others give in Orgrimmar (area 1637), for find_available_quests.
    8001: {1: b"Sting Them", 2: [[3143]], 3: [[3143]], 4: 4, 5: 6, 6: H, 8: [b"Bring 4 Scorpid Stingers."],
           10: {3: [[5466, None, 0]]}},
    8002: {1: b"Too High", 2: [[3143]], 4: 20, 5: 22},
    8003: {1: b"Done Already", 2: [[3143]], 4: 1, 5: 5},
    8004: {1: b"Needs Done Already", 2: [[3143]], 4: 1, 5: 6, 13: [8003]},
    8005: {1: b"Needs Something Else", 2: [[3143]], 4: 1, 5: 6, 12: [8003, 7000]},
    8006: {1: b"Alliance Only", 2: [[4000]], 4: 1, 5: 6, 6: A},
    8007: {1: b"Mages Only", 2: [[3143]], 4: 1, 5: 6, 7: 128},
    8008: {1: b"Gray", 2: [[3143]], 4: 1, 5: 1},
    8009: {1: b"Blacklisted", 2: [[3143]], 4: 1, 5: 6},
    8010: {1: b"Explore", 2: [[3143]], 3: [[3143]], 4: 1, 5: 7, 9: [b"Scout the hill.", {1637: [[50.0, 50.0]]}]},
}
AREAS = """\
ZoneDB.private.areaIdToUiMapIdOverride = [[return {
    [0] = 0, -- Existing fail-safe
}]]
ZoneDB.private.areaIdToUiMapId = [[return {
    [12] = 1429, -- Elwynn Forest
    [44] = 1433, -- Redridge Mountains
    [433] = 1440, -- Falfarren River -> Ashenvale
    [1637] = 1454, -- Orgrimmar
    [2657] = 2652, -- Valley of Bones -> Shen'dralas
}]]
"""


def write_questie(addons_dir, contract=4, mode="baked", flavour="Forever", chunk_ids=True):
    folder = addons_dir / "QuestieDB"
    (folder / "support" / flavour / "Zones").mkdir(parents=True)
    (folder / "support" / flavour / "Zones" / "areaIdToUiMapId.lua").write_text(AREAS)
    lines = ["## Interface: 16001", "## Title: QuestieDB", f"## X-Contract-Version: {contract}",
             f"## X-Flavor: {flavour}", f"## X-Mode: {mode}"]
    for kind, table in (("Npc", NPCS), ("Object", OBJECTS), ("Quest", QUESTS), ("Item", ITEMS)):
        ids = base64.b64encode(zlib.compress(cbor(list(table)))).decode()
        if chunk_ids:  # long values come in parts
            third = len(ids) // 3 + 1
            parts = [ids[i:i + third] for i in range(0, len(ids), third)]
            lines.append(f"## X-{kind}-IDS: ~{len(parts)}~")
            lines += [f"## X-{kind}-IDS-{i}: {p}" for i, p in enumerate(parts, 1)]
        else:
            lines.append(f"## X-{kind}-IDS: {ids}")
        for id, fields in table.items():
            lines += entity_lines(kind, id, fields)
    (folder / f"QuestieDB_{flavour}.toc").write_text("\n".join(lines) + "\n")
    return folder


@pytest.fixture
def with_questie(addons):
    questie._cache.clear()
    write_questie(addons)
    return addons


def write_blacklist(addons_dir):
    folder = addons_dir / "Questie" / "Database" / "Corrections"
    folder.mkdir(parents=True)
    (folder / "QuestieQuestBlacklist.lua").write_text(
        "    local questsToBlacklist = {\n"
        "        [8009] = true, -- not in the game\n"
        "        [8008] = Expansions.Current >= Expansions.Cata,\n"
        "        [3861] = HIDE_ON_MAP,\n"
    )


def in_game(monkeypatch, level=12, race="Orc", player_class="Warrior", faction="Horde", log=(), completed=()):
    """The game's answers to the STATE and QUESTS requests (Player.lua, Quests.lua)."""
    state = {"location": {"zone": "Orgrimmar", "mapID": 1454, "x": 42.5, "y": 68.5},
             "character": {"level": level, "race": race, "class": player_class, "faction": faction}}
    gaps, last = [], 0
    for id in sorted(completed):
        gaps.append(id - last)
        last = id
    quests = {"quests": {"completedGaps": gaps, "log": list(log)}}

    async def request(kind):
        return (state if kind == 0 else quests), "World of Warcraft: Forever"

    monkeypatch.setattr(tools, "request", request)


def test_reads_the_baked_database(with_questie):
    db = questie.load(with_questie, "_forever_")
    assert db.flavour == "Forever" and db.contract == 4
    assert sorted(db.ids("Npc")) == sorted(NPCS)
    karolek = db.get("Npc", 3365)
    assert karolek.get("name") == "Karolek" and karolek.get("subName") == "Expert Leatherworker"
    assert db.roles(karolek) == ["trainer"]
    assert db.roles(db.get("Npc", 3310)) == ["quest giver", "flight master"]
    [spawn] = db.spawns(db.get("Npc", 252691))
    assert (spawn.zone, spawn.subzone, spawn.map_id) == ("Shen'dralas", "Valley of Bones", 2652)
    assert spawn.points == [(42.79, 70.48), (44.0, 72.36)]
    assert db.get("Npc", 1) is None
    # Exact names first, then names where every word starts a word, then the rest.
    assert db.search("Quest", ["gath'ilzogg"]) == [7001]
    assert db.search("Quest", ["gath"]) == [7000, 7001, 7002]


def test_find_npc(with_questie):
    found = tools.find_npc("leatherwork", faction="Horde")
    assert found.splitlines() == [
        "From Questie's database (QuestieDB, Forever, contract 4).",
        "- Karolek <Expert Leatherworker> (NPC 3365; level 35; friendly to Horde; trainer)",
        "    Orgrimmar 62.81, 44.15, map_id 1454",
        "    ends quests: WANTED: Gath'Ilzogg (7001)",
    ]
    # The placeholder with no spawns is left out; Alliance's trainer shows without a faction filter.
    assert "World Leatherworking" not in tools.find_npc("leatherwork")
    assert "Adele Fielder" in tools.find_npc("leatherwork")
    # Role and zone without a name: flight masters' titles differ, the role doesn't.
    doras = tools.find_npc("", role="flight master", zone="orgrimmar")
    assert "Doras <Wind Rider Master> (NPC 3310; level 55; friendly to Horde; quest giver, flight master)" in doras
    assert "Karolek" not in doras
    # 'Shen' is the start of a zone name, not a match inside 'Ashenvale'; subzones are named.
    shen = tools.find_npc("", zone="Shen")
    assert "Shen'dralas (Valley of Bones) 42.79, 70.48; 44.0, 72.36, map_id 2652" in shen
    assert "hostile to both factions; innkeeper" in shen and "Deer" not in shen
    assert "Ashenvale (Falfarren River)" in tools.find_npc("deer")
    assert tools.find_npc("3365").startswith("From Questie's") and "Karolek" in tools.find_npc("3365")
    assert tools.find_npc("nobody").startswith("No NPC in Questie's database matches 'nobody'")
    assert tools.find_npc("") == "Give a name or title to search for, or a role or zone."


def test_find_quest_and_object(with_questie):
    quest = tools.find_quest("gath'ilzogg")
    assert quest.splitlines() == [
        "From Questie's database (QuestieDB, Forever, contract 4).",
        "- WANTED: Gath'Ilzogg (quest 7001; level 26, needs 15)",
        "  starts at: Wanted: Gath'Ilzogg (object 60)",
        "    Redridge Mountains 24.53, 46.16, map_id 1433",
        "  ends at: Karolek (NPC 3365)",
        "    Orgrimmar 62.81, 44.15, map_id 1454",
        "  objective: Kill Gath'Ilzogg and bring his head.",
        "  needs kill/use NPC: Gath'Ilzogg (9001)",
        "    Redridge Mountains (inside; no map position, map_id 1433)",
        "  after: Gathering Idols (7000)",
        "  leads to: Gath's Revenge (7002)",
    ]
    assert "Other matches:" in tools.find_quest("gath", limit=1)
    obj = tools.find_object("wanted")
    assert "- Wanted: Gath'Ilzogg (object 60)" in obj and "starts quests: WANTED: Gath'Ilzogg (7001)" in obj


@pytest.mark.parametrize("change", [
    {"contract": 3},           # older than this reader
    {"mode": "source"},        # data in Lua files, not the .toc
])
def test_unusable_questie_is_skipped(addons, change):
    questie._cache.clear()
    write_questie(addons, **change)
    assert tools.find_npc("Karolek") == tools.NOT_AVAILABLE


def test_without_questie(addons, monkeypatch):
    questie._cache.clear()
    assert tools.find_npc("Karolek") == tools.NOT_AVAILABLE
    assert tools.find_quest("1") == tools.NOT_AVAILABLE
    monkeypatch.delenv("WOW_MCP_ADDONS_DIR")
    assert tools.find_object("chest") == tools.NOT_AVAILABLE


def test_newer_contract_and_unchunked_values(addons):
    questie._cache.clear()
    write_questie(addons, contract=5, chunk_ids=False)
    assert "Karolek" in tools.find_npc("karolek")


def test_bridge_status_line_names_the_lookups():
    from wow_mcp import bridge

    assert bridge.tool_step("mcp__wow__find_npc") == "looking up places"
    assert bridge.tool_step("mcp__wow__find_item") == "looking up items"
    assert bridge.tool_step("mcp__wow__find_available_quests") == "looking for quests nearby"


def test_find_item(with_questie):
    item = tools.find_item("stinger")
    assert item.splitlines()[1:] == [
        "- Scorpid Stinger (item 5466)",
        "  sold by: Gornek (NPC 3143)",
        "    Orgrimmar 42.0, 68.0, map_id 1454",
        "  dropped by: Clattering Scorpid (NPC 3125)",
        "    Orgrimmar 10.0, 10.0; 11.0, 12.0; 12.0, 10.5; 80.0, 80.0, map_id 1454",
        "  reward from: WANTED: Gath'Ilzogg (7001)",
    ]
    elsewhere = tools.find_item("5466", zone="Durotar")
    assert "sold by" not in elsewhere and "dropped by" not in elsewhere and "reward from" in elsewhere


def test_find_available_quests(with_questie, monkeypatch):
    write_blacklist(with_questie)
    in_game(monkeypatch, level=8, completed=[8003])
    found = asyncio.run(tools.find_available_quests())
    names = [line for line in found.splitlines() if line.startswith("- ")]
    # Not: too high, done, a prerequisite missing, Alliance's, a mage's, Questie's blacklist, gray for level 8.
    # Closest first, then lowest level.
    assert names == ["- Sting Them (quest 8001; level 6, needs 4)", "- Needs Done Already (quest 8004; level 6, needs 1)",
                     "- Explore (quest 8010; level 7, needs 1)", "- Gathering Idols (quest 7000; level 18)"]
    assert "    from Gornek (NPC 3143): Orgrimmar 42.0, 68.0, map_id 1454, 1% of the map away" in found
    assert "    from Karolek (NPC 3365): Orgrimmar 62.81, 44.15, map_id 1454, 32% of the map away" in found
    assert "Quests Horde level 8 can pick up in Orgrimmar, closest first (4 in all; 1 gray left out)" in found
    assert "Gray (quest 8008" in asyncio.run(tools.find_available_quests(include_gray=True))
    # In the log already, or another zone.
    in_game(monkeypatch, level=8, completed=[8003], log=[{"id": 8001}])
    assert "Sting Them" not in asyncio.run(tools.find_available_quests())
    assert "No quest to pick up in Elwynn" in asyncio.run(tools.find_available_quests(zone="Elwynn"))
    # An Alliance mage sees theirs and the mage quest; Horde's are left out by race.
    in_game(monkeypatch, level=6, race="Human", player_class="Mage", faction="Alliance")
    found = asyncio.run(tools.find_available_quests())
    assert "Alliance Only" in found and "Sting Them" not in found
    assert "Mages Only" not in found  # its giver, Gornek, is Horde's
    # Skyborne have a race bit per faction.
    in_game(monkeypatch, level=6, race="Skyborne", faction="Horde")
    assert "Sting Them" in asyncio.run(tools.find_available_quests())


def test_quest_log_locations(with_questie, monkeypatch):
    in_game(monkeypatch, log=[
        {"id": 8001, "title": "Sting Them", "objectives": [{"text": "Scorpid Stinger: 1/4", "finished": False}]},
        {"id": 7001, "title": "WANTED: Gath'Ilzogg", "ready": True},
        {"id": 8010, "title": "Explore"},
        {"id": 99999, "title": "Unknown To Questie"},
    ])
    lines = asyncio.run(tools.get_quest_log_locations()).splitlines()
    assert lines[2:] == [
        "- Sting Them (quest 8001)",
        "    collect Scorpid Stinger (item 5466) from Clattering Scorpid",
        "      Orgrimmar around 11.0, 10.8 radius 2.3 (3 spawns, map_id 1454)",
        "      Orgrimmar around 80.0, 80.0 radius 2.0 (1 spawn, map_id 1454)",
        "- WANTED: Gath'Ilzogg (quest 7001; ready to turn in)",
        "  turn in at: Karolek (NPC 3365)",
        "    Orgrimmar 62.81, 44.15, map_id 1454",
        "- Explore (quest 8010)",
        "    Scout the hill.",
        "      Orgrimmar around 50.0, 50.0 radius 2.0 (1 spawn, map_id 1454)",
        "- Unknown To Questie (quest 99999): not in Questie's database; look it up on the web.",
    ]
    in_game(monkeypatch, log=[{"id": 8001, "objectives": [{"text": "Scorpid Stinger: 4/4", "finished": True}]}])
    assert "    done: collect Scorpid Stinger (item 5466) from Clattering Scorpid" in \
        asyncio.run(tools.get_quest_log_locations())


def test_clusters():
    a, b = questie.clusters([(10, 10), (14, 10), (18, 10), (60, 60)])
    assert (a.x, a.y, a.count) == (14.0, 10.0, 3) and a.radius == 5.0
    assert (b.x, b.y, b.radius, b.count) == (60.0, 60.0, 2.0, 1)


def test_tools_offered_only_with_questie(addons, monkeypatch):
    def offered():
        monkeypatch.setattr(app, "TOOLS", [])
        monkeypatch.setattr(app, "GUIDANCE", [])
        importlib.reload(tools)
        return sorted(fn.__name__ for fn, _ in app.TOOLS), len(app.GUIDANCE)

    try:
        assert offered() == ([], 0)
        questie._cache.clear()
        write_questie(addons)
        assert offered() == (["find_available_quests", "find_item", "find_npc", "find_object", "find_quest",
                              "get_quest_log_locations"], 1)
    finally:
        importlib.reload(tools)
