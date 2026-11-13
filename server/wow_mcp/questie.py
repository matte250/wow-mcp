"""Read the Questie addon's quest, NPC and object database from disk, when the player has it installed.

Questie (https://github.com/Questie/Questie) ships its data as a separate addon, QuestieDB. In its
"baked" mode the whole database lives in one .toc file per game flavour, as metadata lines:

    ## X-Npc-IDS: <base64 of zlib of CBOR: list of every NPC ID>
    ## X-Npc-3352-S: <base64 of CBOR: the NPC's scalar fields, plus "p", a bitmask of which table fields exist>
    ## X-Npc-3352-7: <base64 of CBOR: table field 7 (spawns)>

A value too long for one line is stored as "~N~" and continued in keys "<key>-1" .. "<key>-N". Field
numbers come from QuestieDB's src/meta/*Meta.lua (the "contract"); X-Contract-Version says which one.

This is an optional integration: nothing else in wow-mcp needs Questie. `load()` returns None when
QuestieDB isn't installed, isn't in baked mode, or has a contract this reader doesn't know, and the
tools then tell the agent to look things up elsewhere.
"""

import base64
import math
import re
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The QuestieDB contract this reader was written against. Contracts are additive: a newer one keeps
# these field numbers (QuestieDB's ADR 0003), so newer versions are read too.
CONTRACT = 4

# Field numbers (QuestieDB contract 4, src/meta/*Meta.lua).
NPC = {"name": 1, "minLevel": 4, "maxLevel": 5, "spawns": 7, "zoneID": 9, "questStarts": 10, "questEnds": 11,
       "friendlyToFaction": 13, "subName": 14, "npcFlags": 15}
OBJECT = {"name": 1, "questStarts": 2, "questEnds": 3, "spawns": 4, "zoneID": 5}
QUEST = {"name": 1, "startedBy": 2, "finishedBy": 3, "requiredLevel": 4, "questLevel": 5, "requiredRaces": 6,
         "requiredClasses": 7, "objectivesText": 8, "triggerEnd": 9, "objectives": 10, "preQuestGroup": 12,
         "preQuestSingle": 13, "exclusiveTo": 16, "zoneOrSort": 17, "requiredSkill": 18, "requiredMinRep": 19,
         "nextQuestInChain": 22, "specialFlags": 24, "parentQuest": 25, "breadcrumbForQuestId": 27,
         "requiredMaxLevel": 32}
ITEM = {"name": 1, "npcDrops": 2, "objectDrops": 3, "itemDrops": 4, "startQuest": 5, "questRewards": 6,
        "requiredLevel": 10, "vendors": 14}
TYPES = {"Npc": NPC, "Object": OBJECT, "Quest": QUEST, "Item": ITEM}

# Race and class bits in quests' requiredRaces / requiredClasses (QuestieDB's Enum: raceMaskById, classKeys),
# by the names the game's UnitRace / UnitClass give on an English client. Skyborne has one bit per faction.
RACE_BITS = {"Human": 1, "Orc": 2, "Dwarf": 4, "Night Elf": 8, "Undead": 16, "Scourge": 16, "Tauren": 32,
             "Gnome": 64, "Troll": 128, "Goblin": 256, "Blood Elf": 512, "Draenei": 1024}
SKYBORNE_BITS = {"Alliance": 2 ** 32, "Horde": 2 ** 33}
CLASS_BITS = {"Warrior": 1, "Paladin": 2, "Hunter": 4, "Rogue": 8, "Priest": 16, "Death Knight": 32, "Shaman": 64,
              "Mage": 128, "Warlock": 256, "Monk": 512, "Druid": 1024}

# NPC roles from npcFlags. These values are the Classic family's (QuestieDB's Enum.byExpansion.Classic and
# .Forever); later expansions number them differently, so roles are only named for these flavours.
_CLASSIC_ROLES = {2: "quest giver", 4: "vendor", 8: "flight master", 16: "trainer", 128: "innkeeper", 256: "banker",
                  1024: "tabard designer", 2048: "battlemaster", 4096: "auctioneer", 8192: "stable master",
                  16384: "repairs"}
NPC_ROLES = {flavour: _CLASSIC_ROLES for flavour in ("Forever", "Classic", "Era")}

# Which QuestieDB .toc the client loads for each game folder (the client picks a .toc by its suffix).
FLAVOURS = {
    "_forever_": ("Forever",),
    "_classic_era_": ("Vanilla", "Classic"),
    "_anniversary_": ("TBC", "BCC"),
    "_classic_": ("Mists",),
}


# --- CBOR (RFC 8949), the subset C_EncodingUtil.SerializeCBOR writes -------------------------------------

class _Cbor:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def _take(self, n: int) -> bytes:
        chunk = self.data[self.pos:self.pos + n]
        if len(chunk) < n:
            raise ValueError("truncated CBOR")
        self.pos += n
        return chunk

    def _length(self, info: int) -> int:
        if info < 24:
            return info
        size = {24: 1, 25: 2, 26: 4, 27: 8}.get(info)
        if size is None:
            raise ValueError(f"unsupported CBOR length {info}")
        return int.from_bytes(self._take(size), "big")

    def item(self) -> Any:
        initial = self._take(1)[0]
        major, info = initial >> 5, initial & 31
        if major == 0:
            return self._length(info)
        if major == 1:
            return -1 - self._length(info)
        if major in (2, 3):
            raw = self._take(self._length(info))
            return raw.decode("utf-8", "replace")
        if major == 4:
            return [self.item() for _ in range(self._length(info))]
        if major == 5:
            out = {}
            for _ in range(self._length(info)):
                key = self.item()
                out[key] = self.item()
            return out
        if major == 6:  # a tag: keep the value it wraps
            self._length(info)
            return self.item()
        if info == 20:
            return False
        if info == 21:
            return True
        if info in (22, 23):
            return None
        if info == 25:
            return struct.unpack(">e", self._take(2))[0]
        if info == 26:
            return struct.unpack(">f", self._take(4))[0]
        if info == 27:
            return struct.unpack(">d", self._take(8))[0]
        raise ValueError(f"unsupported CBOR item {initial:#x}")


def cbor_loads(data: bytes) -> Any:
    return _Cbor(data).item()


def _lua_list(value: Any) -> list:
    """A Lua array as CBOR carries it: a list, or a map with integer keys 1..n when it has holes."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value[k] for k in sorted(k for k in value if isinstance(k, int))]
    return []


# --- The database ---------------------------------------------------------------------------------------

def _read_toc(path: Path) -> dict[str, str]:
    meta: dict[str, str] = {}
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("## "):
                key, sep, value = line[3:].rstrip("\r\n").partition(": ")
                if sep:
                    meta[key] = value
    return meta


@dataclass
class Spawn:
    area_id: int
    zone: str  # the map's name, as the game and show_on_map know it
    subzone: str | None
    map_id: int | None
    points: list[tuple[float, float]]


@dataclass
class Entity:
    kind: str  # "Npc", "Object", "Quest"
    id: int
    fields: dict[int, Any] = field(default_factory=dict)

    def get(self, name: str) -> Any:
        return self.fields.get(TYPES[self.kind][name])


class QuestieDB:
    def __init__(self, toc: Path, meta: dict[str, str], areas: dict[int, tuple[int | None, str]]):
        self.toc = toc
        self.meta = meta
        self.areas = areas  # areaId -> (uiMapID, name)
        self.flavour = meta.get("X-Flavor", "")
        self.contract = int(meta.get("X-Contract-Version", "0") or 0)
        self._ids: dict[str, list[int]] = {}
        self._rows: dict[tuple[str, int], dict[int, Any] | None] = {}

    def _stored(self, key: str) -> str | None:
        value = self.meta.get(key)
        if value and value.startswith("~") and re.fullmatch(r"~\d+~", value):
            parts = int(value.strip("~"))
            value = "".join(self.meta.get(f"{key}-{i}", "") for i in range(1, parts + 1))
        return value

    def ids(self, kind: str) -> list[int]:
        if kind not in self._ids:
            stored = self._stored(f"X-{kind}-IDS")
            self._ids[kind] = [int(i) for i in _lua_list(cbor_loads(zlib.decompress(base64.b64decode(stored))))] \
                if stored else []
        return self._ids[kind]

    def _row(self, kind: str, id: int) -> dict[int, Any] | None:
        key = (kind, id)
        if key not in self._rows:
            stored = self._stored(f"X-{kind}-{id}-S")
            if stored is None:
                self._rows[key] = None
            else:
                raw = cbor_loads(base64.b64decode(stored))
                if isinstance(raw, list):
                    raw = {i + 1: v for i, v in enumerate(raw) if v is not None}
                self._rows[key] = raw
        return self._rows[key]

    def get(self, kind: str, id: int, *names: str) -> Entity | None:
        """The entity with the named fields (all known ones if none are named), or None if it doesn't exist."""
        row = self._row(kind, id)
        if row is None:
            return None
        keys = TYPES[kind]
        wanted = [keys[n] for n in names if n in keys] if names else list(keys.values())
        present = int(row.get("p", 0) or 0)
        entity = Entity(kind, id)
        for index in wanted:
            if index in row:
                entity.fields[index] = row[index]
            elif present >> (index - 1) & 1:
                stored = self._stored(f"X-{kind}-{id}-{index}")
                if stored is not None:
                    entity.fields[index] = cbor_loads(base64.b64decode(stored))
        return entity

    def name(self, kind: str, id: int) -> str | None:
        row = self._row(kind, id)
        return row.get(1) if row else None

    def roles(self, entity: Entity) -> list[str]:
        flags = int(entity.get("npcFlags") or 0)
        table = NPC_ROLES.get(self.flavour, {})
        return [name for bit, name in table.items() if flags & bit]

    def zone(self, area_id: int) -> tuple[int | None, str, str | None]:
        """(uiMapID, zone, subzone) for an AreaTable ID."""
        map_id, name = self.areas.get(area_id, (None, f"area {area_id}"))
        sub, sep, zone = name.partition(" -> ")
        return (map_id, zone, sub) if sep else (map_id, name, None)

    def spawns(self, entity: Entity) -> list[Spawn]:
        raw = entity.get("spawns") or {}
        if isinstance(raw, list):
            raw = {i + 1: v for i, v in enumerate(raw)}
        out = []
        for area_id, points in raw.items():
            map_id, zone, subzone = self.zone(int(area_id))
            pts = []
            for p in _lua_list(points):
                p = _lua_list(p)
                if len(p) >= 2 and p[0] is not None and p[1] is not None and p[0] >= 0 and p[1] >= 0:
                    pts.append((round(float(p[0]), 2), round(float(p[1]), 2)))
            out.append(Spawn(int(area_id), zone, subzone, map_id, pts))
        return out

    def search(self, kind: str, words: list[str], fields: tuple[str, ...] = ("name",)) -> list[int]:
        """IDs whose named fields together contain every word (case-insensitive), best first: an exact name,
        then names where every word starts a word, then the rest. No words matches everything."""
        words = [w.lower() for w in words if w]
        keys = [TYPES[kind][f] for f in fields]
        phrase = " ".join(words)
        ranked = []
        for id in self.ids(kind):
            row = self._row(kind, id)
            if not row:
                continue
            text = " ".join(str(row.get(k) or "") for k in keys).lower()
            if not all(w in text for w in words):
                continue
            name = str(row.get(keys[0]) or "").lower()
            starts = all(re.search(r"(^|[^a-z0-9])" + re.escape(w), text) for w in words)
            ranked.append((0 if name == phrase else 1 if starts else 2, id))
        return [id for _, id in sorted(ranked)]

def race_bit(race: str | None, faction: str | None) -> int | None:
    if race == "Skyborne":
        return SKYBORNE_BITS.get(faction or "")
    return RACE_BITS.get(race or "")


@dataclass
class Cluster:
    x: float
    y: float
    radius: float  # in percent of the map's width, like highlight_areas
    count: int


def clusters(points: list[tuple[float, float]], reach: float = 6.0) -> list[Cluster]:
    """Group spawn points that lie within `reach` (map percent) of one another into areas, biggest first."""
    groups: list[list[tuple[float, float]]] = []
    for point in points:
        near = [g for g in groups if any(math.dist(point, q) <= reach for q in g)]
        merged = [point] + [q for g in near for q in g]
        groups = [g for g in groups if g not in near] + [merged]
    out = []
    for group in groups:
        cx = sum(p[0] for p in group) / len(group)
        cy = sum(p[1] for p in group) / len(group)
        radius = max(2.0, max(math.dist((cx, cy), p) for p in group) + 1.0)
        out.append(Cluster(round(cx, 1), round(cy, 1), round(min(radius, 50.0), 1), len(group)))
    return sorted(out, key=lambda c: -c.count)


_BLACKLIST_LINE = re.compile(r"^\s*\[(\d+)\]\s*=\s*true\s*,")


def read_blacklist(addons_dir: Path) -> set[int]:
    """Quests the Questie addon itself hides as not in the game (duplicates, removed, world events), from
    Questie/Database/Corrections/QuestieQuestBlacklist.lua. Only unconditional entries; empty without Questie."""
    path = addons_dir / "Questie" / "Database" / "Corrections" / "QuestieQuestBlacklist.lua"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    return {int(m[1]) for line in text.splitlines() if (m := _BLACKLIST_LINE.match(line))}


@dataclass
class Character:
    level: int
    faction: str | None = None  # "Alliance" / "Horde"
    race: str | None = None
    player_class: str | None = None


def gray_below(level: int) -> int:
    """Quests at or below this level are gray (no XP worth having) for a character of `level`; the Classic
    rule of thumb, approximate."""
    if level <= 5:
        return 0
    if level <= 39:
        return level - 5 - level // 10
    return level - 1 - level // 5


def why_unavailable(db: "QuestieDB", quest: Entity, who: Character, completed: set[int], in_log: set[int],
                    blacklist: set[int]) -> str | None:
    """None if `who` can pick up the quest now as far as Questie's data tells, otherwise the reason."""
    id = quest.id
    if id in completed:
        return "done"
    if id in in_log:
        return "in log"
    if id in blacklist:
        return "not in the game"
    if (quest.get("requiredLevel") or 0) > who.level:
        return "level"
    if (max_level := quest.get("requiredMaxLevel")) and who.level > max_level:
        return "level"
    races = quest.get("requiredRaces") or 0
    bit = race_bit(who.race, who.faction)
    if races and bit is not None and not races & bit:
        return "race"
    classes = quest.get("requiredClasses") or 0
    cls = CLASS_BITS.get(who.player_class or "")
    if classes and cls is not None and not classes & cls:
        return "class"
    if quest.get("requiredSkill"):
        return "profession"
    group = _lua_list(quest.get("preQuestGroup"))
    if group and not all(q in completed for q in group):
        return "prerequisite"
    single = _lua_list(quest.get("preQuestSingle"))
    if single and not any(q in completed for q in single):
        return "prerequisite"
    if any(q in completed or q in in_log for q in _lua_list(quest.get("exclusiveTo"))):
        return "exclusive"
    parent = quest.get("parentQuest")
    if parent and parent not in in_log:
        return "prerequisite"
    return None


def _flavour_tocs(folder: Path, game_folder: str | None) -> list[Path]:
    suffixes = FLAVOURS.get(game_folder or "", ())
    tocs = [folder / f"QuestieDB_{s}.toc" for s in suffixes] + [folder / "QuestieDB.toc"]
    return [t for t in tocs if t.is_file()]


_AREA_LINE = re.compile(r"^\s*\[(\d+)\]\s*=\s*(\d+)\s*,\s*--\s*(.+?)\s*$")


def read_areas(folder: Path, flavour: str) -> dict[int, tuple[int | None, str]]:
    """areaId -> (uiMapID, zone name) from QuestieDB's support/<flavour>/Zones/areaIdToUiMapId.lua,
    whose entries are commented with the zone's name ("Subzone -> Zone" for subzones). Overrides (Questie's display policy) are skipped."""
    path = folder / "support" / flavour / "Zones" / "areaIdToUiMapId.lua"
    areas: dict[int, tuple[int | None, str]] = {}
    if not path.is_file():
        return areas
    in_main = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "areaIdToUiMapId = [[" in line:
            in_main = True
            continue
        if in_main and line.startswith("]]"):
            break
        if in_main and (m := _AREA_LINE.match(line)):
            area, ui_map, name = int(m[1]), int(m[2]), m[3]
            areas[area] = (ui_map or None, name)
    return areas


_cache: dict[tuple[Path, float], QuestieDB] = {}


def installed(addons_dir: Path | None, game_folder: str | None = None) -> bool:
    """Whether a baked QuestieDB is there for this game folder (a quick check: its .toc says so near the top)."""
    if not addons_dir:
        return False
    for toc in _flavour_tocs(addons_dir / "QuestieDB", game_folder):
        try:
            with toc.open(encoding="utf-8", errors="replace") as f:
                head = [next(f, "") for _ in range(60)]
        except OSError:
            continue
        if any(line.startswith("## X-Mode: baked") for line in head):
            return True
    return False


def load(addons_dir: Path | None, game_folder: str | None = None) -> QuestieDB | None:
    """The installed QuestieDB for this game folder, or None when there is none this reader can use."""
    if not addons_dir:
        return None
    folder = addons_dir / "QuestieDB"
    for toc in _flavour_tocs(folder, game_folder):
        try:
            mtime = toc.stat().st_mtime
        except OSError:
            continue
        if (toc, mtime) in _cache:
            return _cache[(toc, mtime)]
        meta = _read_toc(toc)
        if meta.get("X-Mode") != "baked":
            continue
        try:
            contract = int(meta.get("X-Contract-Version", "0"))
        except ValueError:
            continue
        if contract < CONTRACT:
            continue
        db = QuestieDB(toc, meta, read_areas(folder, meta.get("X-Flavor", "")))
        try:
            db.ids("Npc")
        except (ValueError, zlib.error, TypeError):
            continue
        _cache.clear()
        _cache[(toc, mtime)] = db
        return db
    return None
