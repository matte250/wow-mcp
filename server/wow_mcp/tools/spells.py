"""The character's spellbook and talents (sections "spellbook" and "talents" of request kind REPUTATION, Player.lua),
and any spell's tooltip and numbers by ID (payload key "spellLookup", section "spellDetails" of kind ITEM_DETAILS).

The spellbook and talent trees are a few KB, so they ride on REPUTATION, the kind asked for least: on STATE, every
location request would carry them. Forever's talents are retail's C_Traits trees, with the class's classic trees as
groups of one tree. Spell details go like item details (tools/items.py): the IDs are published (one live-update slot),
then asked for, again while the client loads them. The game answers a spell lookup for KEEP seconds (Player.lua), so
the same IDs are asked again without a slot only within REUSE.
"""

import re
import time
from typing import Annotated, Any, Literal, get_args

from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from wow_mcp import kinds
from wow_mcp.app import as_list, guidance, request, section, tool, transport
from wow_mcp.tools import items

MAX_SPELLS = 20
REUSE = 90  # seconds the same lookup is asked again instead of sent anew (the game answers one 120 s)

guidance(
    "For questions about the character's spells and talents (what they have, their rotation, what to train next, "
    "which talents to take, what a spell or talent does, a spell's cost, damage or cooldown), use get_spells, "
    "get_talents and get_spell_details: the game's own spellbook, talent trees and numbers. WoW: Forever changed "
    "classes, so websites, guides and other WoW versions can be wrong for this character: use them only as a "
    "fallback (e.g. to find a spell's ID, or for build ideas to check against get_talents), and say when you do. "
    "What can be trained next isn't in the spellbook, apart from spells it lists as not learned yet: new spells "
    "and new ranks of known ones are shown by a class trainer, or need an outside list (Wowhead's Forever database, "
    "else Classic Era data); tell the player which it comes from.",
    order=48,
)

Role = Literal["tank", "healer", "damage"]
ROLES: dict[str, Role] = {"TANK": "tank", "HEALER": "healer", "DAMAGER": "damage"}
GroupKind = Literal["general", "class", "guild", "profession", "pet"]
GROUP_KINDS: tuple[GroupKind, ...] = get_args(GroupKind)
RANK = re.compile(r"^Rank (\d+)$")
# A cooldown as spell tooltips state it (enUS), e.g. "8 sec cooldown", "1.5 min cooldown".
COOLDOWN = re.compile(r"(\d+(?:\.\d+)?) (sec|min|hr) cooldown")
UNITS = {"sec": 1, "min": 60, "hr": 3600}
RANK_FIELD = "The rank, from the game's 'Rank 3'. Null for a spell without ranks."
SUBTEXT = "Other text the game shows under the name, e.g. 'Racial' or 'Journeyman'. Null if none."


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _rank(subtext: Any) -> tuple[int | None, str | None]:
    """'Rank 3' as a rank; any other subtext as it is ('Passive' is left out: passive says it)."""
    text = _text(subtext)
    if text and (m := RANK.match(text)):
        return int(m[1]), None
    return None, (None if text == "Passive" else text)


class Spell(BaseModel):
    spell_id: int
    name: str
    rank: int | None = Field(description=RANK_FIELD + " Lower ranks the character knows are in lower_ranks.")
    subtext: str | None = Field(description=SUBTEXT)
    passive: bool
    lower_ranks: list[int] = Field(
        description="Spell IDs of the lower ranks the character also knows, lowest first: cheaper versions they can "
        "still cast (downranking)."
    )
    cooldown_left: int | None = Field(description="Seconds left on its cooldown right now. Null: ready.")


class UnlearnedSpell(BaseModel):
    spell_id: int
    name: str
    rank: int | None = Field(description=RANK_FIELD)
    subtext: str | None = Field(description=SUBTEXT)
    learn_at_level: int | None = Field(description="The level the game says it's learned at.")
    trainable_now: bool = Field(description="The character has reached that level: a class trainer can teach it now.")


class SpellGroup(BaseModel):
    name: str = Field(description="As the spellbook names it, e.g. 'General', a tab of the class, 'Alchemy', 'Pet'.")
    kind: GroupKind
    spells: list[Spell] = Field(description="The spells the character knows, in the spellbook's order.")
    not_learned: list[UnlearnedSpell] = Field(
        description="Spells the spellbook shows greyed out with the level they're learned at. Not all of what can be "
        "trained: new ranks of known spells aren't among them."
    )


class Spellbook(BaseModel):
    game: str = Field(description="The WoW version being played; the spells are this version's.")
    player_class: str | None
    level: int | None
    groups: list[SpellGroup]
    note: str | None = None


class Talent(BaseModel):
    name: str
    spell_id: int | None = Field(description="Its spell: get_spell_details says what it does at each rank.")
    rank: int = Field(description="Points in it, as applied.")
    max_rank: int
    tier: int = Field(description="Its row in the tree, 1 at the top.")
    points_in_tree_needed: int | None = Field(
        description="Points that must be spent in this tree before its tier opens, as the game sets it. Null: none."
    )
    requires: list[str] = Field(description="Talents it requires first (the arrows in the talent window).")
    can_take: bool = Field(
        description="Its tier and required talents are met and it isn't at max rank: a point can go in now if "
        "unspent_points > 0, else with the next point."
    )
    choices: list[str] | None = Field(description="A talent that's a choice between several: their names.")
    pending_rank: int | None = Field(
        description="A rank picked in the talent window but not applied yet. Null: none pending."
    )


class TalentTree(BaseModel):
    name: str
    points_spent: int
    talents: list[Talent]


class Talents(BaseModel):
    game: str = Field(description="The WoW version being played; the talents are this version's.")
    player_class: str | None
    level: int | None
    specialization: str | None = Field(
        description="The character's primary talent tree as the game names it (character window). Null: none yet."
    )
    role: Role | None
    unspent_points: int | None
    points_spent: int | None
    talent_set: int | None = Field(
        description="With dual specialization: which of the character's talent sets is active, 1 or 2 (the trees are "
        "that set's). Null without."
    )
    trees: list[TalentTree]
    note: str | None = None


class PowerCost(BaseModel):
    power: str = Field(description="E.g. 'mana', 'rage', 'energy'.")
    amount: int
    percent: float | None = Field(description="The cost as a percentage of base mana, if the game gives one.")
    per_second: int | None = Field(description="For channelled or held spells: the cost per second.")


class TalentRanks(BaseModel):
    rank: int = Field(description="The character's rank in the talent, as applied.")
    max_rank: int
    this_rank: list[str] = Field(description="What it does at their rank, as the talent window says. Empty at rank 0.")
    next_rank: list[str] = Field(description="What the next rank does. Empty at max rank.")
    max_rank_text: list[str] = Field(description="What max rank does, when that's beyond the next.")


class SpellDetails(BaseModel):
    spell_id: int
    status: items.Status = Field(
        description="ok; loading: the game was still loading the spell, ask again in a moment; unknown: the game "
        "doesn't know this spell ID (not in this game version?); error: see error."
    )
    name: str | None
    rank: int | None = Field(description=RANK_FIELD)
    subtext: str | None = Field(description=SUBTEXT)
    passive: bool | None
    known: bool | None = Field(description="The character knows it.")
    cast_time: float | None = Field(description="Seconds; 0 is instant.")
    min_range: float | None = Field(description="Yards.")
    max_range: float | None = Field(description="Yards; 0 for spells on oneself.")
    costs: list[PowerCost]
    cooldown: float | None = Field(description="Seconds, as the tooltip states it. Null if it states none.")
    cooldown_left: int | None = Field(description="Seconds left on its cooldown for the character right now.")
    description: str | None
    tooltip: list[str] = Field(
        description="The tooltip lines as the game shows them, top to bottom (a right-hand column follows a tab): "
        "cost, range, cast time, cooldown, requirements, what it does with this game's numbers."
    )
    shown_in_red: list[str] = Field(description="Tooltip lines the game shows in red: what the character lacks.")
    talent: TalentRanks | None = Field(description="For a talent in the character's trees: what each rank does.")
    error: str | None


class SpellDetailsList(BaseModel):
    game: str = Field(description="The WoW version being played; the details are this version's.")
    spells: list[SpellDetails] = Field(description="In the order asked for.")


def _spell(raw: dict) -> Spell:
    rank, subtext = _rank(raw.get("rank"))
    return Spell(
        spell_id=raw["id"], name=_text(raw.get("name")) or f"spell {raw['id']}", rank=rank, subtext=subtext,
        passive=bool(raw.get("passive")), lower_ranks=[i for i in as_list(raw.get("lower")) if isinstance(i, int)],
        cooldown_left=_int(raw.get("cd")),
    )


def _unlearned(raw: dict, level: int | None) -> UnlearnedSpell:
    rank, subtext = _rank(raw.get("rank"))
    learn = _int(raw.get("learn")) or None
    return UnlearnedSpell(
        spell_id=raw["id"], name=_text(raw.get("name")) or f"spell {raw['id']}", rank=rank, subtext=subtext,
        learn_at_level=learn, trainable_now=learn is not None and level is not None and learn <= level,
    )


def _group(raw: dict, level: int | None) -> SpellGroup:
    entries = [s for s in as_list(raw.get("spells")) if isinstance(s, dict) and isinstance(s.get("id"), int)]
    kind = raw.get("kind")
    return SpellGroup(
        name=_text(raw.get("name")) or "?", kind=kind if kind in GROUP_KINDS else "class",
        spells=[_spell(s) for s in entries if "learn" not in s],
        not_learned=[_unlearned(s, level) for s in entries if "learn" in s],
    )


def _talent(raw: dict) -> Talent:
    rank, max_rank = _int(raw.get("rank")) or 0, _int(raw.get("max")) or 0
    pending = _int(raw.get("pending"))
    choices = [c for c in as_list(raw.get("choices")) if isinstance(c, str)]
    return Talent(
        name=_text(raw.get("name")) or "?", spell_id=_int(raw.get("spell")), rank=rank, max_rank=max_rank,
        tier=_int(raw.get("tier")) or 1, points_in_tree_needed=_int(raw.get("needs")) or None,
        requires=[r for r in as_list(raw.get("after")) if isinstance(r, str)], can_take=bool(raw.get("open")),
        choices=choices or None, pending_rank=pending if pending is not None and pending != rank else None,
    )


async def _abilities(name: str) -> tuple[dict, str]:
    reply, version = await request(kinds.REPUTATION)
    data = section(reply, name)
    return data if isinstance(data, dict) else {}, version


@tool(annotations=ToolAnnotations(title="Spellbook", read_only_hint=True, open_world_hint=False))
async def get_spells() -> Spellbook:
    """Get the character's spellbook from the game: every spell they know, grouped as the spellbook groups them
    (General, the class's tabs, professions, the pet's), each with its spell ID, rank, whether it's passive and the
    cooldown left right now; lower ranks they also know are listed with the highest. Spells the spellbook shows as not
    learned yet come with the level they're learned at. Use get_spell_details for what a spell does in this game.
    New ranks of known spells aren't listed: a class trainer shows those."""
    data, version = await _abilities("spellbook")
    level = _int(data.get("level"))
    groups = [_group(g, level) for g in as_list(data.get("tabs")) if isinstance(g, dict)]
    return Spellbook(
        game=version, player_class=_text(data.get("class")), level=level, groups=groups,
        note=f"The game couldn't read the spellbook: {data['problem']}" if data.get("problem") else None,
    )


@tool(annotations=ToolAnnotations(title="Talents", read_only_hint=True, open_world_hint=False))
async def get_talents() -> Talents:
    """Get the character's talents from the game: each talent tree as the talent window shows it, with the points
    spent in it, and every talent's rank and max rank, tier, the points its tier needs in the tree, the talents it
    requires and whether a point can go in; plus unspent points, and the primary tree (specialization) and role the
    game gives the character. Use get_spell_details with talents' spell IDs for what they do at each rank."""
    data, version = await _abilities("talents")
    trees = [
        TalentTree(name=_text(t.get("name")) or "?", points_spent=_int(t.get("spent")) or 0,
                   talents=[_talent(x) for x in as_list(t.get("talents")) if isinstance(x, dict)])
        for t in as_list(data.get("trees")) if isinstance(t, dict)
    ]
    note = None
    if data.get("problem"):
        note = f"The game couldn't read the talents: {data['problem']}"
    elif data.get("none"):
        note = "The character has no talents yet: the game has no talent tree for them."
    return Talents(
        game=version, player_class=_text(data.get("class")), level=_int(data.get("level")),
        specialization=_text(data.get("spec")), role=ROLES.get(data.get("role")),
        unspent_points=_int(data.get("unspent")), points_spent=_int(data.get("spent")),
        talent_set=_int(data.get("group")), trees=trees, note=note,
    )


def _cooldown(tooltip: list[str]) -> float | None:
    for line in tooltip:
        if m := COOLDOWN.search(line):
            return float(m[1]) * UNITS[m[2]]
    return None


def _lines(raw: Any) -> list[str]:
    return [text for line in as_list(raw) if isinstance(line, dict) and (text := items._line(line))]


def _ranks(raw: Any) -> TalentRanks | None:
    if not isinstance(raw, dict):
        return None
    return TalentRanks(rank=_int(raw.get("rank")) or 0, max_rank=_int(raw.get("max")) or 0,
                       this_rank=_lines(raw.get("now")), next_rank=_lines(raw.get("next")),
                       max_rank_text=_lines(raw.get("top")))


def _details(spell_id: int, raw: Any, problem: str | None = None) -> SpellDetails:
    if not isinstance(raw, dict):
        raw = {"status": "error", "error": problem or "The game didn't report this spell."}
    tooltip, red = [], []
    for line in as_list(raw.get("lines")):
        if isinstance(line, dict) and (text := items._line(line)):
            tooltip.append(text)
            if line.get("red"):
                red.append(text)
    status = raw.get("status", "ok")
    rank, subtext = _rank(raw.get("rank"))
    ok = status == "ok"
    cast = raw.get("castTime")
    return SpellDetails(
        spell_id=spell_id,
        status=status if status in items.STATUSES else "error",
        name=_text(raw.get("name")), rank=rank, subtext=subtext,
        passive=bool(raw.get("passive")) if ok else None, known=bool(raw.get("known")) if ok else None,
        cast_time=round(cast / 1000, 2) if isinstance(cast, (int, float)) else None,
        min_range=raw.get("minRange"), max_range=raw.get("maxRange"),
        costs=[PowerCost(power=str(c.get("power") or "?").lower(), amount=_int(c.get("cost")) or 0,
                         percent=c.get("percent"), per_second=_int(c.get("perSec")))
               for c in as_list(raw.get("costs")) if isinstance(c, dict)],
        cooldown=_cooldown(tooltip), cooldown_left=_int(raw.get("cd")),
        description=_text(raw.get("description")), tooltip=tooltip, shown_in_red=red,
        talent=_ranks(raw.get("talent")),
        error=raw.get("error") or (None if status in items.STATUSES else f"unexpected status {status!r}"),
    )


def _publish(ids: list[int]) -> int:
    return transport().publish(
        lambda state, seq: state.update(spellLookup={"id": seq, "at": int(time.time()), "spells": ids}))


def _published(ids: list[int]) -> int | None:
    """The lookup for exactly these spells, if it's the latest one sent and the game still answers it: then asking
    costs no slot."""
    current = transport().load_state().get("spellLookup") or {}
    if current.get("spells") == ids and time.time() - (current.get("at") or 0) < REUSE:
        return current.get("id")
    return None


@tool(annotations=ToolAnnotations(title="Spell details from the game", read_only_hint=True, open_world_hint=False))
async def get_spell_details(
    spell_ids: Annotated[
        list[Annotated[int, Field(gt=0)]],
        Field(min_length=1, max_length=MAX_SPELLS,
              description="Spell IDs for this game version: from get_spells or get_talents, or e.g. 133 (from "
              "wowhead.com/forever/spell=133)."),
    ],
) -> SpellDetailsList:
    """Get spells' details from the game itself, for any spell ID, also ones the character doesn't know: the tooltip
    lines as the game shows them, cost, cast time, range, cooldown, description, whether it's passive and whether the
    character knows it; for a talent in the character's trees, what it does at their rank, the next and max rank.
    Use it for "what does this spell or talent do" and for a spell's real numbers (damage, healing, cost, cooldown):
    WoW: Forever changed classes, so website numbers can be wrong for this game."""
    ids = list(dict.fromkeys(spell_ids))

    def loading(details: dict) -> bool:
        return any(isinstance(s, dict) and s.get("status") == "loading" for s in as_list(details.get("spells")))

    details, version = await items.ask(
        "spellDetails", _published(ids), lambda: _publish(ids),
        answered=lambda data: None if data.get("expired") else data.get("lookup"),
        busy=loading, what="the list of spells",
    )
    problem = details.get("problem")
    found = {s.get("id"): s for s in as_list(details.get("spells")) if isinstance(s, dict)}
    return SpellDetailsList(game=version, spells=[_details(i, found.get(i), problem) for i in ids])
