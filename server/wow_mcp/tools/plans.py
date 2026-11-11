"""Plans: step-by-step guides shown in game as a checklist that ticks itself off (payload key "plan",
Plans.lua). The game reports progress in the "plan" section of request kind STATE.

There can be several plans at once, each under its own tab above the checklist: the payload is
{ id, list = { plan, ... } }, and a plan is { id, tab, kind, profession, title, steps }. Its kind
(questing, class, profession, general) picks the tab's icon in game.
"""

import re
from collections import Counter
from typing import Annotated, Literal

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag

from wow_mcp import item_text, kinds
from wow_mcp.app import as_list, guidance, request, section, tool, transport
from wow_mcp.tools.areas import Area
from wow_mcp.tools.items import MAX_ITEMS, look_up
from wow_mcp.tools.messages import delivery_note
from wow_mcp.tools.routes import Stop

guidance(
    "When the player wants something done that takes several steps (craft an item, a quest chain, 'plan my next "
    "quests', leveling, gathering or farming materials, getting a recipe, a flight path or a mount), build a plan "
    "with show_plan. It shows in game as a checklist that guides them step by step: the navigator arrow points to "
    "each step's places, and every step ticks itself off as the game sees it done. First check what the player "
    "already has and knows, so the plan only covers what's missing: get_inventory (include_bank=True) for "
    "materials, get_vendor_items for what vendors they've visited sell, get_quest_log and get_quest_status for "
    "quests, get_professions and get_known_recipes, get_flight_paths and get_player_location for travel. Prefer "
    "exact game data for this game version: item IDs for 'have' conditions (with the item's name, so the game can "
    "check the ID; get_known_recipes has the item each recipe makes), quest IDs for quest steps, recipe "
    "spell IDs, real coordinates. Give every step conditions the game can check (have, quest_*, level, skill, "
    "recipe, at, zone) so it ticks off by itself; a step without conditions needs the player to tick it. Put "
    "places on every step the player has to walk to (bank, vendors, trainers, quest givers, flight masters, "
    "farming spots with loop=true) and areas on kill or gathering regions. Add the NPC IDs, for this game "
    "version, of the characters and mobs a step is about (npcs: quest givers, vendors, trainers, mobs to kill), "
    "so the checklist shows their models when the player hovers the step. A plan replaces the route from "
    "show_route. The player can keep several plans side by side, each under its own tab above the checklist: "
    "give a plan a short tab name (e.g. 'Level 30', 'Leatherworking', 'Rotation') to add it next to the others, "
    "or the name of an existing tab to replace that plan; without a tab name it replaces the plan in the tab "
    "'Plan'. The tab's icon follows the plan's kind: questing, class (a guide to the player's class: skills to "
    "train, rotation, talents; it gets their class icon), profession (its profession's icon) or general. It's "
    "worked out from the steps when you leave it out, but say kind='class' for class guides. When the player "
    "asks about their plan or seems stuck, call get_plan_progress (it lists the tabs too); to re-plan, send a "
    "new plan to the same tab, by the name it was sent with (the player may have renamed it in game: "
    "get_plan_progress gives both names).",
    order=12,
)

guidance(
    "Gathering needs the profession. Before a plan step or goal asks the player to mine, pick herbs, skin, fish or "
    "open locked boxes, check get_professions: they need that profession, at a skill high enough for the node or "
    "mob. Quest objectives count too, e.g. a quest that asks for ore from veins needs Mining. When they can't "
    "gather it, or buying is cheap next to the time farming takes, make it a buy step instead: a 'have' "
    "condition for the item with the auction house (or vendor) as the place, and its price from "
    "get_auction_prices in the step text. A price there means the item can be traded. Say why you chose buying or "
    "gathering, and ask when it's a close call.",
    order=15,
)


class _Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Have(_Condition):
    """Done when the player has `count` of an item: in their bags, or bags plus bank with bank=true."""

    have: Annotated[
        int,
        Field(gt=0, description="Item ID for this game version: get_known_recipes has the item each recipe makes, "
              "get_inventory and get_vendor_items the items the player has or saw for sale; else Wowhead."),
    ]
    count: Annotated[int, Field(ge=1, le=10000, description="How many they need.")] = 1
    bank: Annotated[
        bool,
        Field(description="Also count the bank. Use it for 'take it out of the bank' steps; leave it off for "
              "steps that need the items carried (crafting, turning in)."),
    ] = False
    name: Annotated[
        str | None,
        Field(max_length=80, description="The item's name as the game shows it. show_plan checks the ID against it, "
              "and the game counts the named item if the ID turns out to be another."),
    ] = None


class Spot(BaseModel):
    zone: Annotated[str, Field(min_length=1, max_length=60, description="Zone or city name as shown in game.")]
    x: Annotated[float, Field(ge=0, le=100, description="X on that zone's map, 0-100.")]
    y: Annotated[float, Field(ge=0, le=100, description="Y on that zone's map, 0-100.")]
    map_id: Annotated[int | None, Field(gt=0, description="Optional uiMapID, only if you're sure.")] = None


class At(_Condition):
    """Done when the player is within `yards` of a spot. Only checked while the step is the current one."""

    at: Spot
    yards: Annotated[float, Field(ge=3, le=300, description="How close they have to get.")] = 12


class InZone(_Condition):
    """Done when the player is in this zone or city (or a map inside it). Only checked for the current step."""

    zone: Annotated[str, Field(min_length=1, max_length=60, description="As named in game, e.g. 'Ironforge'.")]


class InSubzone(_Condition):
    """Done when the player is in this subzone, e.g. 'Razor Hill'. Only checked for the current step."""

    subzone: Annotated[str, Field(min_length=1, max_length=60)]


class QuestAccepted(_Condition):
    """Done when the quest is in the player's quest log (or already turned in)."""

    quest_accepted: Annotated[int, Field(gt=0, description="Quest ID for this game version.")]
    name: Annotated[str | None, Field(max_length=80, description="Quest title, shown until the game knows it.")] = None


class QuestComplete(_Condition):
    """Done when all the quest's objectives are done (ready to turn in), or it's turned in."""

    quest_complete: Annotated[int, Field(gt=0, description="Quest ID for this game version.")]
    name: Annotated[str | None, Field(max_length=80, description="Quest title, shown until the game knows it.")] = None


class QuestObjective(_Condition):
    """Done when one objective of a quest in the log is done (e.g. the kills, before the items)."""

    quest_objective: Annotated[int, Field(gt=0, description="Quest ID for this game version.")]
    index: Annotated[int, Field(ge=1, le=10, description="Which objective, 1 = the first the quest log lists.")]
    name: Annotated[str | None, Field(max_length=80, description="Quest title, shown until the game knows it.")] = None


class QuestTurnedIn(_Condition):
    """Done when the quest is turned in."""

    quest_turned_in: Annotated[int, Field(gt=0, description="Quest ID for this game version.")]
    name: Annotated[str | None, Field(max_length=80, description="Quest title, shown until the game knows it.")] = None


class Level(_Condition):
    """Done when the character reaches this level."""

    level: Annotated[int, Field(ge=2, le=100)]


class Skill(_Condition):
    """Done when a profession or skill reaches this rank."""

    skill: Annotated[str, Field(min_length=1, max_length=60, description="As named in game, e.g. 'Leatherworking'.")]
    rank: Annotated[int, Field(ge=1, le=1000)]


class Recipe(_Condition):
    """Done when the player knows a recipe (learned from a trainer or a recipe item)."""

    recipe: Annotated[
        int | str,
        Field(description="The recipe's spell ID (best; from Wowhead's spell page for this version) or its exact "
              "name as the profession window shows it."),
    ]


class Manual(_Condition):
    """Never done by itself: the player ticks the step off. Only for what the game can't see."""

    manual: Literal[True]


GOAL_ID = r"^[a-z0-9][a-z0-9_-]{0,39}$"


class GoalDone(_Condition):
    """Done when a goal set with set_goals is done (and stays done, like a step). For gathering or anything else
    done alongside other steps: the step links the goal instead of describing it."""

    goal: Annotated[str, Field(pattern=GOAL_ID, description="The goal's id, as given to set_goals.")]


CONDITIONS = {
    "have": Have, "at": At, "zone": InZone, "subzone": InSubzone, "quest_accepted": QuestAccepted,
    "quest_complete": QuestComplete, "quest_objective": QuestObjective, "quest_turned_in": QuestTurnedIn,
    "level": Level, "skill": Skill, "recipe": Recipe, "manual": Manual, "goal": GoalDone,
}


def _kind(value: object) -> str | None:
    """Which kind of condition this is: its one kind key (None if it has none or several)."""
    if isinstance(value, _Condition):
        return next(k for k, cls in CONDITIONS.items() if isinstance(value, cls))
    if isinstance(value, dict):
        found = [k for k in value if k in CONDITIONS]
        return found[0] if len(found) == 1 else None
    return None


Condition = Annotated[
    Annotated[Have, Tag("have")] | Annotated[At, Tag("at")] | Annotated[InZone, Tag("zone")]
    | Annotated[InSubzone, Tag("subzone")] | Annotated[QuestAccepted, Tag("quest_accepted")]
    | Annotated[QuestComplete, Tag("quest_complete")] | Annotated[QuestObjective, Tag("quest_objective")]
    | Annotated[QuestTurnedIn, Tag("quest_turned_in")] | Annotated[Level, Tag("level")]
    | Annotated[Skill, Tag("skill")] | Annotated[Recipe, Tag("recipe")] | Annotated[Manual, Tag("manual")]
    | Annotated[GoalDone, Tag("goal")],
    Discriminator(
        _kind,
        custom_error_type="condition_kind",
        custom_error_message="A condition needs exactly one of these keys: " + ", ".join(CONDITIONS),
    ),
]

LUA_KEYS = {
    "quest_accepted": "questAccepted", "quest_complete": "questComplete", "quest_objective": "questObjective",
    "quest_turned_in": "questTurnedIn", "map_id": "mapID",
}


class Npc(BaseModel):
    """A character or mob a step is about. The game draws its model when the player hovers the step."""

    id: Annotated[
        int,
        Field(gt=0, description="The NPC (creature) ID for this game version, as on its Wowhead NPC page."),
    ]
    name: Annotated[
        str | None,
        Field(max_length=60, description="The NPC's name, shown under its model and in the step's tooltip."),
    ] = None
    display_id: Annotated[
        int | None,
        Field(gt=0, description="Optional creature display ID, used if the game can't show the NPC by its ID. "
              "Only if you're sure."),
    ] = None


class Step(BaseModel):
    text: Annotated[
        str,
        Field(min_length=1, max_length=300, description="The instruction, e.g. 'Buy 2 {item:2320:Coarse Thread} and "
              "1 {item:4289:Salt} from Duokna'. Say what to do and where; the checklist shows it as written. "
              + item_text.HINT),
    ]
    places: Annotated[
        list[Stop],
        Field(max_length=40, description="Where to go for this step. One place: the navigator arrow points there. "
              "Several: a route through them in this order (e.g. farming spots, with loop=true)."),
    ] = []
    loop: Annotated[bool, Field(description="Go round the places until the step is done (farming, gathering).")] = False
    areas: Annotated[
        list[Area],
        Field(max_length=10, description="Regions shaded on the map while this step is current (where the mobs or "
              "herbs are)."),
    ] = []
    conditions: Annotated[
        list[Condition],
        Field(max_length=8, description="All must hold for the step to be done; the game checks them live. Each is "
              "an object with exactly one kind. Item, quest, level, skill and recipe conditions count even before the "
              "step is current; position ones (at, zone, subzone) only while it is. Empty: the player ticks it off."),
    ] = []
    npcs: Annotated[
        list[Npc],
        Field(max_length=4, description="The characters and mobs this step is about: the quest giver, vendor or "
              "trainer to find, the mobs to kill. The player sees what they look like (their models) when they "
              "hover the step in the checklist. Give them on every step that names a character or mob: a step "
              "without them shows no model. Look the NPC IDs up (Wowhead's NPC pages) rather than leaving them out."),
    ] = []


def _step(s: Step) -> dict:
    """A step as the addon reads it. Its NPCs only when it has some."""
    sent = {
        "text": s.text,
        "places": [_place(p) for p in s.places],
        "loop": s.loop,
        "areas": [_area(a) for a in s.areas],
        "conditions": [_lua(c.model_dump()) for c in s.conditions],
    }
    if s.npcs:
        sent["npcs"] = [_lua({"id": n.id, "name": n.name, "display": n.display_id}) for n in s.npcs]
    return sent


def _lua(value: object) -> object:
    """A model dump with the keys the addon reads (Lua identifiers, camelCase)."""
    if isinstance(value, dict):
        return {LUA_KEYS.get(k, k): _lua(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_lua(v) for v in value]
    return value


def _place(p: Stop) -> dict:
    return {"zone": p.zone, "x": round(p.x, 2), "y": round(p.y, 2), "label": p.label, "note": p.note, "mapID": p.map_id}


def _area(a: Area) -> dict:
    return {"zone": a.zone, "x": round(a.x, 2), "y": round(a.y, 2), "radius": round(a.radius, 2), "label": a.label,
            "color": a.color, "mapID": a.map_id}


def describe(c: _Condition) -> str:
    """A condition in words, for the tool's summary."""
    match c:
        case Have():
            return f"{c.count} x {c.name or f'item {c.have}'}{' (bags + bank)' if c.bank else ''}"
        case At():
            return f"within {c.yards:g} yd of {c.at.zone} {c.at.x:.1f}, {c.at.y:.1f}"
        case InZone():
            return f"in {c.zone}"
        case InSubzone():
            return f"in {c.subzone}"
        case QuestAccepted():
            return f"quest {c.quest_accepted} accepted"
        case QuestComplete():
            return f"quest {c.quest_complete} ready to turn in"
        case QuestObjective():
            return f"objective {c.index} of quest {c.quest_objective} done"
        case QuestTurnedIn():
            return f"quest {c.quest_turned_in} turned in"
        case Level():
            return f"level {c.level}"
        case Skill():
            return f"{c.skill} {c.rank}"
        case Recipe():
            return f"recipe {c.recipe} known"
        case GoalDone():
            return f"goal {c.goal!r} done"
    return "ticked by the player"


MAX_PLANS = 10  # with the ones the player archived in game
DEFAULT_TAB = "Plan"
Kind = Literal["questing", "class", "profession", "general"]
# For working out a plan's profession from its title; the game knows the icons (Plans.lua).
PROFESSIONS = ("Alchemy", "Blacksmithing", "Cooking", "Enchanting", "Engineering", "First Aid", "Fishing",
               "Herbalism", "Inscription", "Jewelcrafting", "Leatherworking", "Mining", "Skinning", "Tailoring")
QUEST_CONDITIONS = (QuestAccepted, QuestComplete, QuestObjective, QuestTurnedIn)


def _profession_in(text: str) -> str | None:
    """The profession a title or tab name names, as a word of its own: not the 'mining' in 'Undermining'."""
    return next((name for name in PROFESSIONS if re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE)), None)


def detect_kind(title: str, steps: list["Step"]) -> tuple[str, str | None]:
    """A plan's kind and, for a profession plan, its profession, from what its steps wait for: a profession's
    skill rank or a recipe makes it a profession plan, quests a questing plan. A skill that isn't a profession
    (Riding, a weapon skill) says nothing. A class guide can't be told from its steps: the agent says so."""
    conditions = [c for s in steps for c in s.conditions]
    skills = Counter(
        name for c in conditions if isinstance(c, Skill)
        for name in PROFESSIONS if name.lower() == c.skill.strip().lower()
    )
    if skills:
        return "profession", skills.most_common(1)[0][0]
    named = _profession_in(title)
    if named or any(isinstance(c, Recipe) for c in conditions):
        return "profession", named
    if any(isinstance(c, QUEST_CONDITIONS) for c in conditions):
        return "questing", None
    return "general", None


def sent_plans(state: dict) -> list[dict]:
    """The plans in the server's state, oldest first. Before tabs the state held one plan, { id, title, steps }."""
    sent = state.get("plan") or {}
    if isinstance(sent.get("list"), list):
        return [p for p in sent["list"] if isinstance(p, dict)]
    if sent.get("steps"):
        return [{"id": sent.get("id"), "tab": DEFAULT_TAB, "kind": "general", "title": sent.get("title"),
                 "steps": sent["steps"]}]
    return []


def _same_tab(a: object, b: str) -> bool:
    return isinstance(a, str) and a.strip().lower() == b.strip().lower()


def _removed(game: object) -> set[int]:
    """The ids of the plans the player removed in game, from the game's plan section."""
    listed = as_list(game.get("removed")) if isinstance(game, dict) else []
    return {int(i) for i in listed if isinstance(i, (int, float))}


def _forget(removed: set[int]) -> None:
    """Drops the plans the player removed in game from the server's state, so they stop counting and aren't
    sent again. Nothing is sent for it: the game already has them gone."""
    if not removed:
        return
    t = transport()
    with t._locked():
        state = t.load_state()
        current = sent_plans(state)
        kept = [p for p in current if p.get("id") not in removed]
        if len(kept) != len(current):
            state["plan"] = {"id": (state.get("plan") or {}).get("id"), "list": kept}
            t._save(state)


async def _forget_removed() -> bool:
    """Asks the game which plans the player removed and forgets those. False if it can't be asked."""
    if delivery_note():  # not running, or another version: it can't answer
        return False
    try:
        reply, _ = await request(kinds.STATE)
    except ToolError:
        return False
    _forget(_removed(reply.get("plan")))
    return True


def _check_goals(steps: list["Step"]) -> None:
    """A step can only wait for a goal the game has, or it never ticks off."""
    goals = {g.get("id") for g in (transport().load_state().get("goals") or {}).get("goals") or []}
    for n, s in enumerate(steps, 1):
        for c in s.conditions:
            if isinstance(c, GoalDone) and c.goal not in goals:
                raise ToolError(f"Step {n} waits for goal {c.goal!r}, which isn't set: call set_goals first.")


def _same(a: str, b: str) -> bool:
    """Whether two names are the same the way Plans.lua's Same() sees it: case, a leading 'The' and anything but
    letters and digits don't count."""

    def normal(name: str) -> str:
        return re.sub(r"[^0-9a-z]", "", re.sub(r"^the\s+", "", name.lower()))

    return normal(a) == normal(b)


async def _check_items(steps: list[Step]) -> str:
    """Checks the item IDs of named 'have' conditions in the game (one lookup) and raises a ToolError listing the
    ones that are another item, or none. Returns a note when they couldn't be checked."""
    named: dict[int, list[tuple[int, str]]] = {}
    for n, s in enumerate(steps, 1):
        for c in s.conditions:
            if isinstance(c, Have) and c.name:
                named.setdefault(c.have, []).append((n, c.name))
    if not named:
        return ""
    if delivery_note():  # not running, or another version: it can't answer
        return "Its item IDs weren't checked against the game."
    ids = list(named)
    try:
        found = (await look_up(ids[:MAX_ITEMS])).items
    except ToolError as e:
        return f"Its item IDs weren't checked against the game: {e}"
    wrong = []
    for item in found:
        for n, name in named[item.item_id]:
            if item.status == "unknown":
                wrong.append((n, f"step {n}: item {item.item_id} ({name}) isn't in this game"))
            elif item.name and not _same(item.name, name):
                wrong.append((n, f"step {n}: item {item.item_id} is {item.name}, not {name}"))
    if wrong:
        raise ToolError(
            "Nothing was sent: these 'have' conditions have another item's ID in this game.\n"
            + "\n".join(text for _, text in sorted(wrong))
            + "\nFix the IDs and send the plan again: get_known_recipes has the item each known recipe makes, "
            "get_inventory and get_vendor_items have the items the player has or has seen for sale, and "
            "get_item_details looks up any ID."
        )
    # Still loading, or more than one lookup takes.
    unchecked = [i.item_id for i in found if not i.name and i.status != "unknown"] + ids[MAX_ITEMS:]
    return f"Item {', '.join(map(str, unchecked))} couldn't be checked against the game." if unchecked else ""


@tool(
    annotations=ToolAnnotations(
        title="Show a plan in game", read_only_hint=False, destructive_hint=False, open_world_hint=False
    )
)
async def show_plan(
    title: Annotated[
        str,
        Field(min_length=1, max_length=80, description="e.g. 'Leatherworking 113 to 125' or "
              "'{item:4253:Toughened Leather Gloves}'. " + item_text.HINT),
    ],
    steps: Annotated[list[Step], Field(min_length=1, max_length=50, description="In the order to do them.")],
    tab: Annotated[
        str | None,
        Field(min_length=1, max_length=20, description="A short name for the plan's tab above the checklist, e.g. "
              "'Level 30', 'Leatherworking' or 'Rotation'. A new name adds the plan next to the player's other "
              f"plans (up to {MAX_PLANS}); the name of an existing tab replaces that plan. Left out: the tab "
              f"'{DEFAULT_TAB}', replacing the plan there."),
    ] = None,
    kind: Annotated[
        Kind | None,
        Field(description="What the plan is about, for the tab's icon: 'questing' (quests, leveling routes), "
              "'class' (a guide to the player's class: skills, rotation, talents; shows their class icon), "
              "'profession' (shows the profession's icon) or 'general' (anything else). Left out: worked out "
              "from the steps (skill or recipe conditions: profession; quest conditions: questing; else "
              "general). Always say 'class' for a class guide."),
    ] = None,
    profession: Annotated[
        str | None,
        Field(min_length=1, max_length=40, description="For a profession plan: the profession as named in game, "
              "e.g. 'Leatherworking'. Left out: taken from the steps' skill conditions or the title. Given "
              "without a kind, the kind is 'profession'."),
    ] = None,
) -> str:
    """Show the player a step-by-step plan in World of Warcraft: a checklist window that guides them through
    each step and ticks it off by itself as the game sees it done, then moves on.

    Use it for anything that takes several steps: crafting (bank, vendors, farming, trainer, crafting), quest
    chains and leveling routes, gathering. The current step's places drive the navigator arrow and the game's
    waypoint (a looping route for farming spots) and its areas are shaded on the map. Hovering a step shows
    the models of the NPCs it names (npcs), so the player knows who or what to look for: add them to every
    step about a quest giver, vendor, trainer or mob, or the player sees no models. The player can keep
    several plans, each under its own tab above the checklist, with a name and an icon for its kind: this plan
    replaces the one in the tab of the same name (the tab 'Plan' if none is given) or else adds a tab, and
    becomes the one shown. It also replaces the route from show_route. The player can switch tabs, tick steps
    off, go back, hide the window, and close a plan with the x on its tab: archived (they can bring it back)
    or removed. The item IDs of 'have' conditions with a name are checked in the game
    first: if one is another item, nothing is sent and the error lists them.
    """
    _check_goals(steps)
    tab = " ".join((tab or DEFAULT_TAB).split()) or DEFAULT_TAB
    found_kind, found_profession = detect_kind(title, steps)
    kind = kind or ("profession" if profession else found_kind)
    if kind == "profession":
        profession = profession or found_profession or _profession_in(tab)
    else:
        profession = None

    def others() -> list[dict]:
        return [p for p in sent_plans(transport().load_state()) if not _same_tab(p.get("tab"), tab)]

    full = len(others()) >= MAX_PLANS
    asked = full and await _forget_removed()  # the ones the player removed in game don't count
    if full and len(others()) >= MAX_PLANS:
        raise ToolError(
            f"Nothing was sent: the player already has {len(others())} plans (tabs: "
            + ", ".join(repr(p.get("tab")) for p in others())
            + "). Send this one to one of those tabs to replace it, or remove one with clear_plan(tab=...) first."
            + ("" if asked else " Plans the player removed in game still count until the game has said so: "
               "get_plan_progress asks it.")
        )
    checked = await _check_items(steps)
    plan = {
        "tab": tab,
        "kind": kind,
        "profession": profession,
        "title": title,
        "steps": [_step(s) for s in steps],
    }
    tabs: list[str] = []

    def change(state: dict, seq: int) -> None:
        # In place when it replaces a tab's plan, so the tabs keep their order; the new id makes it the one shown.
        kept = sent_plans(state)
        new = {"id": seq, **plan}
        for i, old in enumerate(kept):
            if _same_tab(old.get("tab"), tab):
                kept[i] = new
                break
        else:
            kept.append(new)
        tabs[:] = [str(p.get("tab")) for p in kept]
        state["plan"] = {"id": seq, "list": kept}

    plan_id = transport().publish(change)

    lines = []
    for n, s in enumerate(steps, 1):
        done = "; ".join(describe(c) for c in s.conditions) if s.conditions else "ticked by the player"
        lines.append(f"{n}. {s.text} [done when: {done}]")
    manual = sum(1 for s in steps if not any(not isinstance(c, Manual) for c in s.conditions))
    result = f"Sent plan #{plan_id} {title!r} with {len(steps)} steps to the game:\n" + "\n".join(lines)
    if len(tabs) > 1 or tab != DEFAULT_TAB or kind != "general":
        icon = f"{profession or 'profession'} icon" if kind == "profession" else f"{kind} icon"
        result += f"\nIt's in the tab {tab!r} ({icon})"
        result += f"; the player's tabs are now: {', '.join(tabs)}." if len(tabs) > 1 else "."
    if manual:
        result += f"\n{manual} step(s) have nothing the game can check, so the player ticks them off."
    if not any(s.npcs for s in steps):
        result += ("\nNo step names its NPCs (npcs), so hovering the steps shows no models. If steps are about "
                   "characters or mobs (quest givers, vendors, trainers, mobs to kill), send the plan again with "
                   "their NPC IDs.")
    return result + "\nSteps already done in game tick off right away; get_plan_progress shows where they are." \
        + (f" {checked}" if checked else "") + delivery_note()


class ConditionProgress(BaseModel):
    kind: str = Field(description="have, at, zone, subzone, questAccepted, questTurnedIn, questComplete, "
                      "questObjective, level, skill, recipe or manual.")
    progress: str = Field(
        description="As the checklist shows it, e.g. \"Spider's Silk 1/2\" or 'Go to Ironforge'. A 'have' condition "
        "whose item ID is another item in game says so, and counts the named item instead."
    )
    met: bool


class StepProgress(BaseModel):
    step: int
    text: str | None
    done: bool = Field(description="Done stays done, e.g. a farming step after the items were crafted with.")
    ticked_by_hand: bool = Field(description="The player ticked it off rather than the game seeing it done.")
    current: bool
    needs_click: bool = Field(description="Not done, and nothing the game checks will tick it: the player has to.")
    progress: list[ConditionProgress] = Field(
        description="Live, for steps not done. Position conditions only count for the current step."
    )


class PlanTab(BaseModel):
    plan_id: int
    tab: str | None = Field(description="The tab's name as sent with show_plan: the name show_plan and clear_plan "
                            "take to replace or remove this plan.")
    renamed_to: str | None = Field(default=None, description="What the player renamed the tab to in game, if they "
                                   "did. Use it when talking to them; the tools only know `tab`.")
    kind: str | None = Field(description="questing, class, profession or general.")
    title: str | None
    shown: bool = Field(description="The tab the player has open: the plan that guides them and ticks itself off.")
    archived: bool = Field(default=False, description="The player put this plan away (the x on its tab): it has no "
                           "tab until they bring it back from the archive, and keeps its progress.")
    steps_done: int = Field(description="For a tab that isn't open: as of when the player last had it open.")
    total_steps: int
    complete: bool = False


class PlanProgress(BaseModel):
    game: str = Field(description="The WoW version being played.")
    active: bool = Field(description="Whether the game has a plan it's tracking.")
    plan_id: int | None = None
    tab: str | None = Field(default=None, description="The name of the plan's tab as sent with show_plan: the "
                            "name show_plan and clear_plan take to replace or remove it.")
    renamed_to: str | None = Field(default=None, description="What the player renamed the tab to in game, if they "
                                   "did. Use it when talking to them; the tools only know `tab`.")
    kind: str | None = None
    title: str | None = None
    current_step: int | None = Field(default=None, description="Null when all steps are done.")
    steps_done: int = 0
    total_steps: int = 0
    complete: bool = False
    hidden: bool = Field(default=False, description="The player hid the window; progress is still tracked.")
    steps: list[StepProgress] = []
    tabs: list[PlanTab] = Field(
        default=[], description="All the player's plans, one per tab. Only the open one is checked live; steps of "
        "the others catch up when the player opens their tab."
    )
    note: str | None = None


def game_says(game: object, tabs: list[PlanTab]) -> str | None:
    """Why the game has no plan open, if it says: the player archived what's left ('archived'), or removed the
    plan ('cleared')."""
    if not isinstance(game, dict):
        return None
    if game.get("archived") or any(t.archived for t in tabs):
        return "archived"
    return "cleared" if game.get("cleared") else None


@tool(annotations=ToolAnnotations(title="Progress on the in-game plan", read_only_hint=True, open_world_hint=False))
async def get_plan_progress() -> PlanProgress:
    """Get how far the player is with the plan they have open (sent with show_plan): which steps are done, the
    current step, and live progress on each step's conditions (e.g. "Spider's Silk 1/2"), plus their other
    plans' tabs. Use it when the player asks about their plan or says they're stuck, and before re-planning.

    A tab's `tab` is the name to give show_plan (to replace its plan) or clear_plan; `renamed_to` is what the
    player calls it, if they renamed it in game. Plans the player removed in game are gone from here on."""
    reply, version = await request(kinds.STATE)
    game = section(reply, "plan")
    _forget(_removed(game))  # the plans the player removed in game: no longer theirs, or sent again
    state = transport().load_state()
    sent = sent_plans(state)
    names = {p.get("id"): p.get("tab") for p in sent}  # the server's name for a tab is the one its tools take
    tabs = [
        PlanTab(plan_id=int(t["id"]), tab=names.get(t["id"]) or t.get("tab"), renamed_to=t.get("name"),
                kind=t.get("kind"), title=t.get("title"),
                shown=bool(t.get("shown")), steps_done=int(t.get("done") or 0), total_steps=int(t.get("steps") or 0),
                complete=bool(t.get("complete")), archived=bool(t.get("archived")))
        for t in as_list(game.get("tabs") if isinstance(game, dict) else None)
        if isinstance(t, dict) and isinstance(t.get("id"), (int, float))
    ]
    # The game says which payload it last received: another than the server's last means that one hasn't
    # arrived (live updates off). An addon that doesn't say is behind if it doesn't show a plan that was sent.
    payload = (state.get("plan") or {}).get("id")
    seen = game.get("seen") if isinstance(game, dict) else None
    behind = seen != payload if seen and payload else None
    if not isinstance(game, dict) or not game.get("id"):
        note = "There's no plan in game. show_plan sends one."
        if sent and (behind or (behind is None and not game_says(game, tabs))):
            note = "The game hasn't received the plan yet (live updates off?): the player should type /reload."
        elif game_says(game, tabs) == "archived":
            note = ("The player archived their plans (the x on a tab); the archive button above the plan window, or "
                    "/mcp plan restore, brings one back. show_plan sends a new one.")
        elif game_says(game, tabs) == "cleared":
            note = "The player removed the plan (/mcp plan clear). show_plan sends a new one."
        return PlanProgress(game=version, active=False, tabs=tabs, note=note)

    shown = next((p for p in sent if p.get("id") == game["id"]), None)
    texts = [s.get("text") for s in shown.get("steps") or []] if shown else []
    current = game.get("current")
    steps = []
    for n, entry in enumerate(as_list(game.get("steps")), 1):
        entry = entry if isinstance(entry, dict) else {}
        steps.append(StepProgress(
            step=n,
            text=texts[n - 1] if n <= len(texts) else None,
            done=bool(entry.get("done")),
            ticked_by_hand=bool(entry.get("byHand")),
            current=n == current,
            needs_click=bool(entry.get("needsClick")),
            progress=[
                ConditionProgress(kind=str(c.get("kind")), progress=str(c.get("text") or ""), met=bool(c.get("met")))
                for c in as_list(entry.get("progress")) if isinstance(c, dict)
            ],
        ))
    notes = []
    if behind is None:
        behind = bool(sent) and not shown
    if behind and not sent:
        notes.append("The game still shows a plan that was cleared: that hasn't arrived (the player should /reload).")
    elif behind and not shown:
        notes.append("The game still shows an older plan: the newest hasn't arrived (the player should /reload).")
    elif behind:
        notes.append("The game hasn't received the latest change to the player's plans (a plan sent or cleared "
                     "since): its tabs are out of date (the player should /reload).")
    if game.get("problem"):
        notes.append(f"A condition couldn't be checked in game: {game['problem']}")
    return PlanProgress(
        game=version,
        active=True,
        plan_id=game["id"],
        tab=(shown or {}).get("tab") or game.get("tab"),
        renamed_to=game.get("name"),
        kind=game.get("kind") or (shown or {}).get("kind"),
        title=game.get("title"),
        current_step=current,
        steps_done=sum(s.done for s in steps),
        total_steps=len(steps),
        complete=bool(game.get("complete")),
        hidden=bool(game.get("hidden")),
        steps=steps,
        tabs=tabs,
        note=" ".join(notes) or None,
    )


@tool(
    annotations=ToolAnnotations(
        title="Clear the in-game plan", read_only_hint=False, destructive_hint=True, open_world_hint=False
    )
)
def clear_plan(
    tab: Annotated[
        str | None,
        Field(max_length=20, description="The tab of the plan to remove, by the name it was sent with "
              "(get_plan_progress lists them, as `tab`). Left out: all the player's plans."),
    ] = None,
) -> str:
    """Remove a plan sent with show_plan, or all of them: the tab and its checklist, and the navigator
    directions and highlighted areas of the plan that was showing."""
    removed: list[str] = []

    def change(state: dict, seq: int) -> None:
        current = sent_plans(state)
        if tab is not None and not any(_same_tab(p.get("tab"), tab) for p in current):
            names = ", ".join(repr(p.get("tab")) for p in current) or "none"
            raise ToolError(f"No plan in a tab {tab!r}; the player's tabs: {names}.")
        kept = [] if tab is None else [p for p in current if not _same_tab(p.get("tab"), tab)]
        removed[:] = [str(p.get("tab")) for p in current if p not in kept]
        state["plan"] = {"id": seq, "list": kept}

    transport().publish(change)
    if tab is None:
        return "Cleared the plan." if len(removed) <= 1 else f"Cleared the {len(removed)} plans."
    return f"Cleared the plan in the tab {removed[0]!r}."
