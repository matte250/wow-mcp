"""Gear upgrades: what could beat the player's gear, from the auction house, quest rewards and vendors (payload key
"upgradeQuery", section "upgrades" of request kind ITEM_DETAILS, Auction.lua).

Only the game knows items' stats, slots and requirements, and the auction house's full scan has thousands of items,
so the search runs in game. The server first reads the character (class, level, talent specialization and money;
request kind STATE) to pick stat weights, then publishes the query (one live-update slot; none when the same query is
still answered) and asks until the game's answer is there: per slot, the equipped gear and the best candidates the
character can use, with their stats and where to get them. Scores and their explanations are worked out here, from
the game's stats and the same weights the game ranked by.
"""

import math
import re
import time
from typing import Annotated, Any, Literal, get_args

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from wow_mcp import kinds
from wow_mcp.app import as_list, guidance, request, section, tool, transport
from wow_mcp.tools import items
from wow_mcp.tools.auction import _ago
from wow_mcp.tools.inventory import SLOTS, STAT_NAMES, Quality, Stats, quality, stat_name, stats
from wow_mcp.tools.stats import format_money

guidance(
    "For 'what are my best gear upgrades', 'what should I buy for my gear' or 'is X an upgrade', call find_upgrades "
    "(with item_ids for specific items). It searches the auction house's last full scan, the rewards of quests in the "
    "player's log and the vendors they've visited, keeps what this character can use, and scores it with the game's "
    "own stats; never take item stats from websites for this. Mention free quest rewards first, then what's cheap. "
    "Compare prices with the player's money (in the result; affordable is false when they can't pay), and say how "
    "old auction prices are. An upgrade the player is a level or two short of can go in a plan or goal ('buy at "
    "27'). A random-suffix item ('of the Bear') is priced and scored per suffix; one marked as a random suffix with "
    "unknown stats needs the player to check the listings. If the player says how they play (e.g. caster gear for an "
    "Elemental Shaman), pass weights.",
    order=47,
)

Slot = Literal["head", "neck", "shoulder", "back", "chest", "wrist", "hands", "waist", "legs", "feet", "finger",
               "trinket", "main hand", "off hand", "ranged"]
Source = Literal["auction", "quests", "vendors"]
SOURCES: tuple[Source, ...] = get_args(Source)
MAX_PER_SLOT = 5
MAX_ASKED = 10
REUSE = 90  # seconds the same query is asked again instead of sent anew (the game keeps an answer 120 s)
WORK_RETRIES, WORK_WAIT = 20, 1.0  # the game is still searching

DPS_KEY = "ITEM_MOD_DAMAGE_PER_SECOND_SHORT"
ARMOR_KEY = "RESISTANCE0_NAME"
RANGED_LOCS = {"INVTYPE_RANGED", "INVTYPE_RANGEDRIGHT", "INVTYPE_THROWN"}
MELEE_DPS, RANGED_DPS = "damage per second", "ranged damage per second"
ALIASES = {"dps": MELEE_DPS, "weapon dps": MELEE_DPS, "melee dps": MELEE_DPS, "ranged dps": RANGED_DPS}
STAT = re.compile(r"^[a-z][a-z0-9 ]*$")

# Leveling stat weights per class (UnitClass's token), and per talent specialization where it changes what's
# wanted: the first word of the spec's name, lower case. Strength 1 is the yardstick; armor is per point, weapon
# DPS per point of DPS (melee for main and off hand, ranged for bows, guns, crossbows, thrown and wands).
MELEE = {"strength": 1.0, "agility": 0.6, "stamina": 0.6, "attack power": 0.5, "armor": 0.03, MELEE_DPS: 6.0}
CASTER = {"intellect": 1.0, "spell power": 1.0, "spell damage done": 1.0, "stamina": 0.6, "spirit": 0.5,
          "mana per 5 sec": 1.5, "armor": 0.005}
HEALER = {"intellect": 1.0, "spell healing done": 1.0, "spell power": 0.9, "mana per 5 sec": 2.0, "spirit": 0.6,
          "stamina": 0.4, "armor": 0.005}
TANK = {"stamina": 1.0, "armor": 0.06, "strength": 0.6, "agility": 0.6, "defense skill rating": 1.0, MELEE_DPS: 3.0}
WAND = {RANGED_DPS: 3.0}
WEIGHTS: dict[str, dict[str | None, tuple[str, dict[str, float]]]] = {
    "WARRIOR": {None: ("melee (Arms, Fury)", {**MELEE, RANGED_DPS: 0.5}), "protection": ("tank", TANK)},
    "PALADIN": {
        None: ("melee (Retribution)", {**MELEE, "agility": 0.5, "intellect": 0.4, "spirit": 0.1}),
        "holy": ("healer", HEALER),
        "protection": ("tank", {**TANK, "intellect": 0.3, "spell power": 0.3}),
    },
    "HUNTER": {None: ("ranged physical", {
        "agility": 1.0, "ranged attack power": 0.5, "attack power": 0.1, "stamina": 0.5, "intellect": 0.4,
        "strength": 0.1, "spirit": 0.1, "armor": 0.02, RANGED_DPS: 6.0, MELEE_DPS: 0.5})},
    "ROGUE": {None: ("melee", {"agility": 1.0, "strength": 0.5, "stamina": 0.5, "attack power": 0.5, "armor": 0.02,
                               MELEE_DPS: 6.0, RANGED_DPS: 0.3})},
    "PRIEST": {None: ("caster with a wand (Shadow, Discipline)", {**CASTER, "spirit": 0.8, **WAND}),
               "holy": ("healer", {**HEALER, RANGED_DPS: 1.0})},
    "SHAMAN": {
        None: ("melee (Enhancement)", {**MELEE, "intellect": 0.4, "spirit": 0.1}),
        "elemental": ("caster", CASTER),
        "restoration": ("healer", HEALER),
    },
    "MAGE": {None: ("caster with a wand", {**CASTER, **WAND})},
    "WARLOCK": {None: ("caster with a wand", {**CASTER, "stamina": 0.8, "intellect": 0.8, **WAND})},
    "DRUID": {
        None: ("feral (cat and bear forms, where weapons don't count)", {
            "strength": 1.0, "agility": 0.8, "stamina": 0.6, "attack power": 0.5, "intellect": 0.3, "spirit": 0.1,
            "armor": 0.04}),
        "balance": ("caster", CASTER),
        "restoration": ("healer", HEALER),
    },
}
GENERIC = ("balanced (class not recognized)", {
    "strength": 0.5, "agility": 0.5, "stamina": 0.6, "intellect": 0.5, "spirit": 0.3, "armor": 0.02, MELEE_DPS: 3.0,
    RANGED_DPS: 1.0})


class Offer(BaseModel):
    source: Literal["quest reward", "vendor", "auction house"]
    cost: str = Field(description="e.g. 'free: quest reward from The Defias Brotherhood (ready to turn in)', '1g 20s "
                      "on the auction house (3 listed, full scan 2 hours ago)', '45s from Brother Danil (Northshire "
                      "Abbey, Elwynn Forest)'.")
    price: int | None = Field(description="Copper per item. Null for quest rewards.")
    affordable: bool | None = Field(description="The player has that much money now. Null for quest rewards.")
    quest_id: int | None = None


class Equipped(BaseModel):
    slot: str
    item_id: int
    name: str | None = Field(description="With its random suffix, if it has one.")
    quality: Quality | None
    item_level: int | None
    armor: int | None
    stats: Stats | None
    score: float = Field(description="With the weights used.")
    broken: bool = Field(description="Durability 0: it gives no stats until repaired.")


class Candidate(BaseModel):
    item_id: int
    name: str | None = Field(description="With the random suffix it's scored for, if any.")
    quality: Quality | None
    item_level: int | None
    required_level: int | None
    levels_to_go: int = Field(description="Levels until the player can wear it; 0: now.")
    type: str | None = Field(description="E.g. 'Leather', 'Shields', 'Two-Handed Maces'.")
    armor: int | None
    stats: Stats | None = Field(description="As the game reports them, including weapon DPS ('damage per second').")
    random_suffix: str | None = Field(
        description="Set for random-suffix items: either this exact suffix (its own stats and price), or unknown: the "
        "stats are the item's without a suffix and the price the cheapest of any suffix, so check the listings."
    )
    score_change: float = Field(description="How much better than what it replaces, with the weights used.")
    why: str = Field(description="The stat differences behind it, biggest first, and what it replaces.")
    get_it: list[Offer] = Field(description="Where to get it: free quest rewards first, then the cheapest.")
    can_use: bool
    cant_use_because: str | None = Field(description="What the game shows in red, e.g. 'Mail' or 'Classes: Paladin'.")
    asked: bool = Field(description="One of the item_ids asked about: listed whether it's an upgrade or not.")


class SlotUpgrades(BaseModel):
    slot: str
    equipped: list[Equipped] = Field(description="Two for finger and trinket; empty if nothing is worn there.")
    upgrades: list[Candidate] = Field(description="Best first.")
    note: str | None = None


class Upgrades(BaseModel):
    game: str = Field(description="The WoW version being played; stats and prices are this version's.")
    character: str
    money: str
    money_copper: int | None
    weights_used: str = Field(description="Which weights scored the items, and why those.")
    weights: dict[str, float] = Field(description="Per point of each stat; armor per point, DPS per point of DPS.")
    searched: list[str]
    slots: list[SlotUpgrades]
    not_gear: list[int] = Field(description="Items asked about that aren't worn.")
    note: str | None = None


def _keys(name: str) -> list[str]:
    """The game's stat keys (GetItemStats) for a stat name as get_inventory shows it."""
    known = [key for key, shown in STAT_NAMES.items() if shown == name]
    if known:
        return known
    if name.endswith(" socket"):
        return ["EMPTY_SOCKET_" + name.removesuffix(" socket").upper().replace(" ", "_")]
    return ["ITEM_MOD_" + name.upper().replace(" ", "_") + "_SHORT"]


def _clean(weights: dict[str, float]) -> dict[str, float]:
    cleaned = {}
    for raw, value in weights.items():
        name = ALIASES.get(raw.strip().lower(), raw.strip().lower())
        if not STAT.match(name):
            raise ToolError(f"{raw!r} isn't a stat name. Use names as get_inventory shows stats, e.g. 'agility', "
                            "'attack power', 'armor', 'damage per second', 'ranged damage per second'.")
        if not math.isfinite(value):
            raise ToolError(f"The weight for {raw!r} must be a number.")
        cleaned[name] = float(value)
    if not cleaned:
        raise ToolError("Give at least one stat weight, or leave weights out for the class defaults.")
    return cleaned


def _defaults(character: dict) -> tuple[str, dict[str, float]]:
    """The class's weights for the character's talent specialization, and what they are."""
    token = str(character.get("classFile") or character.get("class") or "").upper()
    shown = character.get("class") or "this class"
    spec = character.get("spec") if isinstance(character.get("spec"), str) else None
    sets = WEIGHTS.get(token)
    if not sets:
        return f"{shown}: {GENERIC[0]} leveling weights", GENERIC[1]
    key = spec.split()[0].lower() if spec else None
    if key not in sets and character.get("role") in ("HEALER", "TANK"):
        role = "healer" if character["role"] == "HEALER" else "tank"
        key = next((k for k, (label, _) in sets.items() if label == role), None)
    label, chosen = sets.get(key) or sets[None]
    if spec:
        return f"{shown}, {spec}: {label} leveling weights", chosen
    others = [label for k, (label, _) in sets.items() if k]
    hint = f"; pass weights for {' or '.join(others)} gear" if others else ""
    return f"{shown} (no talent specialization yet): {label} leveling weights{hint}", chosen


def _payload_weights(weights: dict[str, float]) -> dict:
    """The weights as the game scores with them: by stat key, plus armor and melee or ranged weapon DPS."""
    out: dict[str, Any] = {"stats": {}, "armor": 0.0, "melee": 0.0, "ranged": 0.0}
    for name, value in weights.items():
        if name == "armor":
            out["armor"] = value
        elif name == MELEE_DPS:
            out["melee"] = value
        elif name == RANGED_DPS:
            out["ranged"] = value
        else:
            for key in _keys(name):
                out["stats"][key] = value
    return out


def _terms(raw: dict, loc: Any) -> dict[str, float]:
    """An item's stats by name, with armor, and weapon DPS as melee or ranged."""
    terms: dict[str, float] = {}
    for key, value in (raw.get("stats") if isinstance(raw.get("stats"), dict) else {}).items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            name = (RANGED_DPS if loc in RANGED_LOCS else MELEE_DPS) if key == DPS_KEY else stat_name(key)
            terms[name] = terms.get(name, 0) + value
    if isinstance(raw.get("armor"), (int, float)):
        terms["armor"] = terms.get("armor", 0) + raw["armor"]
    return terms


def _score(weights: dict[str, float], terms: dict[str, float]) -> float:
    return sum(value * weights.get(name, 0) for name, value in terms.items())


def _number(value: float) -> str:
    return f"{int(value):+d}" if float(value).is_integer() else f"{value:+.1f}"


def _why(weights: dict[str, float], terms: dict[str, float], replaced: dict[str, float], what: str) -> str:
    diffs = {name: terms.get(name, 0) - replaced.get(name, 0) for name in {*terms, *replaced}}
    diffs = {name: round(d, 2) for name, d in diffs.items() if abs(d) > 1e-9}
    weighted = sorted((n for n in diffs if weights.get(n)), key=lambda n: -abs(diffs[n] * weights[n]))
    unweighted = sorted(n for n in diffs if not weights.get(n))
    parts = [f"{_number(diffs[n])} {'DPS' if n == MELEE_DPS else 'ranged DPS' if n == RANGED_DPS else n}"
             for n in weighted]
    if unweighted:
        parts.append("unweighted: " + ", ".join(f"{_number(diffs[n])} {n}" for n in unweighted))
    return f"{', '.join(parts) or 'the same stats'} vs {what}"


def _offer(raw: dict, money: int | None, random: bool) -> Offer | None:
    """Where to get an item; random: it has random suffixes and no suffix of it was seen, so an auction price is the
    cheapest of any."""
    price = raw.get("price") if isinstance(raw.get("price"), int) else None
    affordable = price <= money if price is not None and money is not None else None
    src = raw.get("src")
    if src == "quest":
        cost = f"free: quest reward from {raw.get('quest') or 'a quest in the log'}"
        if raw.get("ready"):
            cost += " (ready to turn in)"
        if isinstance(raw.get("choice"), int):
            cost += f", one of {raw['choice']} to choose from"
        return Offer(source="quest reward", cost=cost, price=None, affordable=None, quest_id=raw.get("questID"))
    if src == "vendor" and price is not None:
        where = ", ".join(p for p in (raw.get("subzone"), raw.get("zone")) if isinstance(p, str) and p)
        cost = f"{format_money(price)} from {raw.get('vendor') or 'a vendor'}" + (f" ({where})" if where else "")
        if raw.get("cost"):
            cost += f" plus {raw['cost']}"
        if isinstance(raw.get("at"), int):
            cost += f", seen {_ago(raw['at'])}"
        return Offer(source="vendor", cost=cost, price=price, affordable=affordable)
    if src == "auction" and price is not None:
        seen = f"{'search' if raw.get('search') else 'full scan'} {_ago(int(raw.get('at') or 0))}"
        listed = f"{raw['listed']} listed, " if isinstance(raw.get("listed"), int) else ""
        if random:
            cost = (f"from {format_money(price)} on the auction house, the cheapest of any random suffix ({listed}{seen}): "
                    "check the listings for the suffix's stats")
        else:
            cost = f"{format_money(price)} on the auction house ({listed}{seen})"
        return Offer(source="auction house", cost=cost, price=price, affordable=affordable)
    return None


def _equipped(raw: dict, weights: dict[str, float]) -> Equipped:
    return Equipped(
        slot=SLOTS.get(raw.get("slot"), str(raw.get("slot"))), item_id=raw["id"], name=raw.get("name"),
        quality=quality(raw.get("q")), item_level=raw.get("ilvl"), armor=raw.get("armor"),
        stats=stats(raw.get("stats")), score=round(_score(weights, _terms(raw, raw.get("loc"))), 1),
        broken=bool(raw.get("broken")),
    )


def _candidate(raw: dict, weights: dict[str, float], worn: dict[int, dict], level: int, money: int | None) -> Candidate:
    terms = _terms(raw, raw.get("loc"))
    replaced: dict[str, float] = {}
    names = []
    for slot in as_list(raw.get("vs")):
        if slot in worn:
            for name, value in _terms(worn[slot], worn[slot].get("loc")).items():
                replaced[name] = replaced.get(name, 0) + value
            names.append(worn[slot].get("name") or f"item {worn[slot]['id']}")
    what = " and ".join(names) if names else "an empty slot"
    raw_offers = [r for r in as_list(raw.get("offers")) if isinstance(r, dict)]
    random = not raw.get("suffix") and bool(raw.get("random") or any(r.get("random") for r in raw_offers))
    offers = [o for o in (_offer(r, money, random) for r in raw_offers) if o]
    suffix = None
    if raw.get("suffix"):
        suffix = "this exact suffix: its own stats and price"
    elif random:
        suffix = "unknown: random suffix; these are the item's stats without one"
        if any(o.source == "auction house" for o in offers):
            suffix += ", and the auction price is the cheapest of any suffix, so check the listings"
        elif any(o.source == "quest reward" for o in offers):
            suffix += ", which the quest rolls when it gives the item"
    required = raw.get("req") if isinstance(raw.get("req"), int) else None
    return Candidate(
        item_id=raw["id"], name=raw.get("name"), quality=quality(raw.get("q")), item_level=raw.get("ilvl"),
        required_level=required if required and required > 1 else None,
        levels_to_go=max(0, (required or 0) - level), type=raw.get("sub") or None, armor=raw.get("armor"),
        stats=stats(raw.get("stats")), random_suffix=suffix,
        score_change=round(_score(weights, terms) - _score(weights, replaced), 1),
        why=_why(weights, terms, replaced, what), get_it=offers, can_use=not raw.get("cant"),
        cant_use_because=raw.get("cant") or None, asked=bool(raw.get("asked")),
    )


def _query(state: dict) -> dict:
    query = dict(state.get("upgradeQuery") or {})
    query.pop("id", None)
    query.pop("at", None)
    return query


def _published(query: dict) -> int | None:
    """The query the game has, if it's this one and recent enough that the game still has its answer."""
    current = transport().load_state().get("upgradeQuery") or {}
    if _query({"upgradeQuery": current}) == query and time.time() - (current.get("at") or 0) < REUSE:
        return current.get("id")
    return None


def _publish(query: dict) -> int:
    return transport().publish(
        lambda state, seq: state.update(upgradeQuery={"id": seq, "at": int(time.time()), **query}))


@tool(annotations=ToolAnnotations(title="Gear upgrades", read_only_hint=True, open_world_hint=False))
async def find_upgrades(
    slots: Annotated[list[Slot] | None, Field(description="Slots to search. All by default.")] = None,
    max_price: Annotated[
        int | None, Field(ge=0, description="The most to pay per item, in copper (1g = 10000). No limit by default.")
    ] = None,
    level_ahead: Annotated[
        int, Field(ge=0, le=10, description="Also items up to this many levels above the player, to plan for.")
    ] = 2,
    sources: Annotated[
        list[Source] | None,
        Field(description="Where to look: auction (the last full scan, and searches), quests (rewards of quests in "
              "the log), vendors (the ones the player has visited). All by default; none when item_ids are given."),
    ] = None,
    per_slot: Annotated[int, Field(ge=1, le=MAX_PER_SLOT, description="Upgrades per slot, best first.")] = 3,
    weights: Annotated[
        dict[str, float] | None,
        Field(description="Stat weights, e.g. {'intellect': 1, 'spell power': 1.2, 'stamina': 0.5}, with stat names "
              "as get_inventory shows them; 'armor' is per point of armor, 'damage per second' and 'ranged damage "
              "per second' per point of weapon DPS. Leave out for leveling defaults for the player's class and talent "
              "specialization."),
    ] = None,
    item_ids: Annotated[
        list[Annotated[int, Field(gt=0)]] | None,
        Field(max_length=MAX_ASKED, description="Specific items to compare ('is X an upgrade?'): each is compared "
              "in its slot and listed whether it's better or not."),
    ] = None,
) -> Upgrades:
    """Find the player's best gear upgrades: items on the auction house (from the game's last full scan), rewards of
    the quests in their log and what vendors they've visited sell, that this character can use (armor and weapon
    types, class, and required level up to level_ahead above theirs), scored against what they wear with the game's
    own stats. Per slot: the equipped item with its stats, and the best candidates with stats, armor and DPS,
    required level, where to get it and what it costs (free quest reward, auction price and age, or vendor), and a
    score change with the stat differences behind it. Random-suffix items are priced and scored per suffix."""
    reply, version = await request(kinds.STATE)
    character, money_stats = section(reply, "character"), section(reply, "stats")
    character = character if isinstance(character, dict) else {}
    money_stats = money_stats if isinstance(money_stats, dict) else {}
    level = int(character.get("level") or money_stats.get("level") or 0)
    money = money_stats.get("money") if isinstance(money_stats.get("money"), int) else None
    if weights:
        chosen, used = _clean(weights), "as asked"
    else:
        used, chosen = _defaults(character)
    query = {
        "slots": list(dict.fromkeys(slots)) if slots else None,
        "sources": list(dict.fromkeys(sources)) if sources is not None else ([] if item_ids else list(SOURCES)),
        "maxPrice": max_price, "levelAhead": level_ahead, "perSlot": per_slot,
        "items": list(dict.fromkeys(item_ids or [])), "weights": _payload_weights(chosen),
    }
    data, version = await items.ask(
        "upgrades", _published(query), lambda: _publish(query),
        answered=lambda data: None if data.get("expired") else data.get("query"),
        busy=lambda data: data.get("status") == "working", retries=WORK_RETRIES, wait=WORK_WAIT,
        what="the search",
    )
    if data.get("status") == "error":
        raise ToolError(f"The search failed in game: {data.get('error')}")
    if data.get("status") != "done":
        raise ToolError(
            f"The game is still searching ({data.get('checked') or 0} items checked so far). Call find_upgrades again "
            "with the same arguments in a few seconds: it picks up the same search."
        )
    worn = {e["slot"]: e for e in as_list(data.get("equipped")) if isinstance(e, dict) and isinstance(e.get("id"), int)}
    groups = []
    for raw in as_list(data.get("slots")):
        if not isinstance(raw, dict):
            continue
        slot_numbers = {"finger": (11, 12), "trinket": (13, 14)}.get(raw.get("slot"))
        number = next((n for n, name in SLOTS.items() if name == raw.get("slot")), None)
        equipped = [_equipped(worn[n], chosen) for n in (slot_numbers or (number,)) if n in worn]
        upgrades = [_candidate(c, chosen, worn, level, money) for c in as_list(raw.get("items"))
                    if isinstance(c, dict) and isinstance(c.get("id"), int)]
        note = None
        if raw.get("twoHand"):
            note = "A two-hander is equipped, so off hands aren't compared (they'd need a one-hand weapon instead)."
        elif not upgrades:
            note = "Nothing better found."
        groups.append(SlotUpgrades(slot=raw.get("slot") or "?", equipped=equipped, upgrades=upgrades, note=note))
    searched, notes = [], []
    wanted = query["sources"]
    if "auction" in wanted:
        scan = data.get("scan") if isinstance(data.get("scan"), dict) else None
        if scan:
            searched.append(f"the auction house: full scan {_ago(int(scan.get('at') or 0))} "
                            f"({scan.get('items') or 0} items), and later searches")
        else:
            searched.append("the auction house: no full scan yet")
            notes.append("No auction house prices yet: the game takes a full scan when the player next opens the "
                          "auction house.")
    if "quests" in wanted:
        searched.append("rewards of the quests in the log")
    if "vendors" in wanted:
        searched.append("vendors the player has visited")
    if item_ids:
        searched.append("the items asked about")
    if data.get("questsLoading"):
        notes.append("Some quests' rewards were still loading in game; ask again for them.")
    if data.get("notLoaded"):
        notes.append(f"{data['notLoaded']} item(s) didn't load in time and were left out.")
    if data.get("problem"):
        notes.append(f"Something failed in game: {data['problem']}")
    spec = character.get("spec")
    who = f"level {level} {character.get('class') or 'character'}" + (f" ({spec})" if spec else "")
    return Upgrades(
        game=version, character=who, money=format_money(money) if money is not None else "unknown",
        money_copper=money, weights_used=used, weights=chosen, searched=searched, slots=groups,
        not_gear=[i for i in as_list(data.get("notGear")) if isinstance(i, int)], note=" ".join(notes) or None,
    )
