"""Reading the Questie addon's database from disk (questie.py) and the lookup tools on it (tools/questie.py).
The QuestieDB here is a tiny one written the way QuestieDB's baked .toc is: CBOR rows, base64, and a zlib'd
ID list, with long values split into "~N~" chunks."""

import base64
import struct
import zlib

import pytest

from wow_mcp import questie
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
}
OBJECTS = {60: {1: b"Wanted: Gath'Ilzogg", 2: [7001], 4: {44: [[24.53, 46.16]]}}}
QUESTS = {
    7001: {1: b"WANTED: Gath'Ilzogg", 2: [None, [60]], 3: [[3365]], 4: 15, 5: 26,
           8: [b"Kill Gath'Ilzogg and bring his head."], 10: {1: [[9001]]}, 13: [7000], 22: 7002},
    7000: {1: b"Gathering Idols", 2: [[3365]], 3: [[3365]], 5: 18},
    7002: {1: b"Gath's Revenge", 5: 27},
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
    for kind, table in (("Npc", NPCS), ("Object", OBJECTS), ("Quest", QUESTS)):
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

    assert bridge.tool_step("mcp__wow__find_npc") == "looking up NPCs"
    assert bridge.tool_step("mcp__wow__find_quest") == "looking up quests"
