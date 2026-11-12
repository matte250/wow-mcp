"""The player's own lists: named lists of items (or anything else) they keep for good, e.g. 'Good flips' or a
shopping list. It's a building block: the AI builds views of a list (a curated named visual, visualize's list, which
reads it on every refresh), but what's on it is the player's to decide. Nothing leaves a list because it's bad right
now, only when they say so.

Lists are files on this computer, not game data: lists.json in the data folder (~/.local/share/wow-mcp,
$XDG_DATA_HOME, next to memory.py's files), per character by name, never pruned.
"""

import contextlib
import json
import logging
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator, model_validator

from wow_mcp import item_text, locking, memory
from wow_mcp.app import guidance, tool, transport
from wow_mcp.tools.memory import Player, whose
from wow_mcp.tools.visuals import MAX_LIST, named_visuals, same_name
from wow_mcp.transport import write_atomic

log = logging.getLogger("wow_mcp.lists")

GUIDANCE = (
    "The player can keep lists of their own, per character and for good: items (by item ID) or anything else (by "
    "label), each with their note on it and when and why it was added, e.g. 'Good flips', a shopping list, things "
    "to farm. get_lists and get_list read them; update_list adds and removes entries; delete_list deletes a list. "
    "A list is the player's: change it only when they ask ('add X to Good flips', 'remove Y', 'make a list of …'). "
    "You may suggest additions, in your answer or in a 'Suggestions' block of a view, but add them only once the "
    "player agrees. Never remove an entry on your own, not even one that's bad right now: what's bad now may not "
    "stay bad, so show its current state instead ('not worth it right now: flooded, 40 listed at 15s'). Put what "
    "the player says about an entry in its note, in their words ('buy at Wenna Silkbeard; sells Saturdays'), and "
    "give items their item ID and name. The game's chat box takes 255 characters, so the player may add several "
    "items in one message ('add Heavy Leather Ball, Simple Wood and Bronze Tube to Good flips') or go on in the next "
    "ones ('also Coarse Thread', 'yes, add the first two'): your recent exchanges come with each question, so "
    "follow up on them."
)
# Refresh instructions for the guidance's example view; they fit a named visual's 300 characters.
GOOD_FLIPS = (
    "Show the items of my list 'Good flips' (get_list), each with its profit after the AH cut, the cheapest vendor "
    "and where, how many are listed, how it sold for me (get_auction_history); mark the ones bad right now, don't "
    "drop them. Then a Suggestions block of new candidates for me to approve."
)
VIEWS_GUIDANCE = (
    "When the player wants control over what a named view shows (a shopping or watch list, 'my good flips'), make "
    "it a curated view of a list: point it at one of their lists, or make one (update_list), and give visualize "
    "list=<the list's name> and refresh instructions that name the list. Every refresh then reads the list afresh "
    "(get_list) and shows what's on it now; a list not made yet shows as empty, with suggestions. Views of derived "
    "data (results, trackers, reports) stay automatic; if you can't tell which they want, ask (in game: make it "
    "automatic and say it can be a list they control instead). After changing a "
    "list, update its view (in the same layout), or say its next Refresh shows the change. For example 'Good "
    "flips' (vendor items worth reselling on the auction house), with list='Good flips' and the refresh \""
    + GOOD_FLIPS + "\": one block with every item on the list and its current profit after the auction house's cut "
    "(get_auction_prices, get_vendor_items), the cheapest vendor and where it is, how many are listed now, and how "
    "it sold for the player before (get_auction_history). Items that aren't worth it right now stay in it, marked "
    "(red, with why), never dropped. Then a separate 'Suggestions' block of new candidates you found (in their "
    "auction history or at vendors), marked as suggestions for the player to approve ('add X to Good flips'). Make "
    "it with the view, so its layout has it: a row saying so when there are none."
)
guidance(GUIDANCE, order=69)
guidance(VIEWS_GUIDANCE, order=69)

LISTS_FILE = "lists.json"
MAX_NAME = MAX_LIST  # a curated view names its list (visualize's list)
MAX_DESCRIPTION = 300
MAX_LABEL = 80
MAX_NOTE = memory.MAX_NOTE
MAX_WHY = 200
MAX_ENTRIES = 200  # per list: a view shows each in a table row, and a table holds 200
MAX_SHOWN = 1500  # characters of the entries a deleted list had, in delete_list's answer
# With every list read back, as a reminder for whoever builds a view from it.
RULE = ("This list is the player's: show every entry, also those that aren't worth it right now (say why), and add or "
        "remove entries only when they ask.")


def one_line(text: str | None) -> str | None:
    return " ".join(text.split()) if text is not None else None


def item_token(text: str) -> tuple[int, str | None] | None:
    """An item written as {item:<id>} or {item:<id>:<name>}, as the AI writes items: its ID and name."""
    text = text.strip()
    if not item_text.ITEM.fullmatch(text):
        return None
    _, raw_id, *name = text[1:-1].split(":", 2)
    return int(raw_id), (one_line(name[0]) or None) if name else None


def describe(entry: dict) -> str:
    """An entry as the AI writes it: an item as {item:<id>:<name>}, anything else as its label."""
    label = entry.get("label")
    if entry.get("item"):
        shown = label and not any(c in label for c in "{}|")  # what a token's name can't hold
        return f"{{item:{entry['item']}:{label}}}" if shown else f"{{item:{entry['item']}}}"
    return repr(label)


def _listed(things: list[str]) -> str:
    return things[0] if len(things) == 1 else ", ".join(things[:-1]) + " and " + things[-1]


# The store -----------------------------------------------------------------------------------


def _key(saved: dict, player: str) -> str:
    """The player's name as first saved: names are typed in any case."""
    player = player.strip()
    return next((key for key in saved if key.casefold() == player.casefold()), player)


def _valid(lists: object) -> list[dict]:
    """The lists that can be read; anything else in the file is left alone, never dropped."""
    if not isinstance(lists, list):
        return []
    return [x for x in lists if isinstance(x, dict) and isinstance(x.get("name"), str)]


def _entries(found: dict) -> list[dict]:
    return [e for e in found.get("entries") or [] if isinstance(e, dict)]


class Lists:
    """Every player's lists, in lists.json: {player: [{name, description, at, entries}]}, each entry {item?, label?,
    note?, at, why?}. Lists keep the order they were made in, entries the order they were added."""

    def __init__(self, path: Path | None = None):
        self.path = path or memory.data_dir() / LISTS_FILE

    def of(self, player: str) -> list[dict]:
        saved = self._load()
        return _valid(saved.get(_key(saved, player)))

    def find(self, player: str, name: str) -> dict | None:
        return next((x for x in self.of(player) if same_name(x["name"], name)), None)

    def _load(self, changing: bool = False) -> dict:
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                raise ValueError("not a JSON object")
            return saved
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            if changing:  # saving now would lose them all
                raise ValueError(f"can't read the lists in {self.path}: {e}") from e
            log.warning("can't read the lists in %s: %s", self.path, e)
            return {}

    @contextlib.contextmanager
    def change(self, player: str) -> Iterator[list[dict]]:
        """The player's lists to change (as saved: _valid gives the ones to work on); saved afterwards. One change at
        a time: the bridge's agent and other sessions may change them at once."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_name(self.path.name + ".lock"), "a+b") as lock:
            locking.acquire(lock)
            saved = self._load(changing=True)
            mine = saved.setdefault(_key(saved, player), [])
            if not isinstance(mine, list):
                raise ValueError(f"can't read {player}'s lists in {self.path}")
            yield mine
            write_atomic(self.path, json.dumps(saved, ensure_ascii=False, indent=1).encode("utf-8"))


def matches(entry: dict, ref: int | str) -> bool:
    """Whether an entry is the one meant: by its item ID (also as text, or {item:<id>}) or its label (any case)."""
    if isinstance(ref, str):
        token = item_token(ref)
        ref = token[0] if token else int(ref) if ref.strip().isdigit() else ref
    if isinstance(ref, int):
        return entry.get("item") == ref
    return bool(entry.get("label")) and same_name(entry["label"], ref)


def on_list(entries: list[dict], new: "NewEntry") -> dict | None:
    """The entry already on a list that a new one is: the same item, or the same label where one of them has no
    item ID (it was added by name before)."""
    if new.item_id and (same := next((e for e in entries if e.get("item") == new.item_id), None)):
        return same
    return next((e for e in entries if new.label and e.get("label") and same_name(e["label"], new.label)
                 and not (new.item_id and e.get("item"))), None)


# Tools ---------------------------------------------------------------------------------------

Name = Annotated[
    str, Field(min_length=1, max_length=MAX_NAME, description="The list's name, e.g. 'Good flips' (any case).")
]


class NewEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: Annotated[int | None, Field(gt=0, description="The item's ID, for an item.")] = None
    label: Annotated[
        str | None,
        Field(max_length=MAX_LABEL, description="The item's name; for an entry that isn't an item, what it is, e.g. "
              "'Rugged Leather from Cherry'."),
    ] = None
    note: Annotated[
        str | None,
        Field(max_length=MAX_NOTE, description="What the player said about it, in their words, e.g. 'buy at Wenna "
              "Silkbeard; sells Saturdays'. Adding an entry that's on the list again with a note updates its note."),
    ] = None
    why: Annotated[
        str | None,
        Field(max_length=MAX_WHY, description="Why it was added, in a few words, e.g. 'the player asked' or 'your "
              "suggestion from their auction history (5 sold at 63s), which they approved'."),
    ] = None

    @field_validator("label", "note", "why")
    @classmethod
    def _one_line(cls, value: str | None) -> str | None:
        return one_line(value) or None

    @model_validator(mode="after")
    def _what(self) -> "NewEntry":
        if self.label and (token := item_token(self.label)) and self.item_id in (None, token[0]):
            self.item_id, self.label = token  # written as {item:<id>:<name>}
        if self.item_id is None and not self.label:
            raise ValueError("an entry needs an item_id or a label")
        return self

    def saved(self, at: float) -> dict:
        entry = {"item": self.item_id, "label": self.label, "note": self.note, "at": at, "why": self.why}
        return {key: value for key, value in entry.items() if value is not None}


class ListSummary(BaseModel):
    name: str
    description: str | None = Field(default=None, description="What the list is for.")
    entries: int
    made: str = Field(description="The local date it was made.")


class PlayerLists(BaseModel):
    player: str
    lists: list[ListSummary] = Field(description="In the order they were made.")
    note: str | None = None


class ListEntry(BaseModel):
    item_id: int | None = None
    label: str | None = Field(default=None, description="The item's name, or what the entry is when it isn't an item.")
    note: str | None = Field(default=None, description="The player's note on it.")
    added: str = Field(description="When it was added, local time.")
    why: str | None = Field(default=None, description="Why it was added.")


class PlayerList(BaseModel):
    player: str
    name: str
    description: str | None = Field(default=None, description="What the list is for.")
    made: str = Field(description="The local date it was made.")
    entries: list[ListEntry] = Field(description="In the order they were added.")
    note: str


def _date(at: object, form: str = "%Y-%m-%d") -> str:
    return f"{datetime.fromtimestamp(at):{form}}" if isinstance(at, (int, float)) else "unknown"


def _names(lists: list[dict]) -> str:
    return ", ".join(repr(x["name"]) for x in lists)


def _missing(player: str, name: str, lists: list[dict]) -> ToolError:
    return ToolError(f"{player} has no list {name!r}. " + (f"Their lists: {_names(lists)}." if lists else
                                                            "They have no lists yet."))


def _views_note(name: str, deleted: bool = False) -> str:
    """What to tell of the named visuals in game that show a list: its curated views (visualize's list)."""
    try:
        named = named_visuals(transport().load_state())
    except (ToolError, OSError, ValueError):  # no game set up here: no views to tell of
        return ""
    views = [repr(v["name"]) for v in named if isinstance(v.get("list"), str) and same_name(v["list"], name)]
    if not views:
        return ""
    many = len(views) > 1
    which = f" The named visual{'s' if many else ''} {_listed(views)}"
    if deleted:
        return (f"{which} still {'show' if many else 'shows'} it: tell the player, who may want "
                f"{'them' if many else 'it'} removed too (remove_visual).")
    return (f"{which} {'show' if many else 'shows'} this list: update {'them' if many else 'it'}, or say the next "
            "Refresh shows the change.")


@tool(annotations=ToolAnnotations(title="The player's lists", read_only_hint=True, open_world_hint=False))
async def get_lists(player: Player = None) -> PlayerLists:
    """List the player's own lists (e.g. 'Good flips', a shopping list): each one's name, what it's for and how many
    entries it has. The lists are theirs, kept for good per character; get_list reads one."""
    name = await whose(player)
    lists = Lists().of(name)
    return PlayerLists(
        player=name, note=None if lists else f"{name} has no lists yet.",
        lists=[ListSummary(name=x["name"], description=x.get("description"), entries=len(_entries(x)),
                           made=_date(x.get("at"))) for x in lists],
    )


@tool(annotations=ToolAnnotations(title="One of the player's lists", read_only_hint=True, open_world_hint=False))
async def get_list(name: Name, player: Player = None) -> PlayerList:
    """Get the entries of one of the player's lists, in the order they were added: each item (ID and name) or other
    entry, the player's note on it, and when and why it was added. A view built from a list calls this on every
    refresh, so it shows what's on the list now. Show every entry, also those that aren't worth it right now."""
    who = await whose(player)
    found = Lists().find(who, name)
    if found is None:
        raise _missing(who, name, Lists().of(who))
    entries = _entries(found)
    return PlayerList(
        player=who, name=found["name"], description=found.get("description"), made=_date(found.get("at")),
        entries=[ListEntry(item_id=e.get("item"), label=e.get("label"), note=e.get("note"),
                           added=_date(e.get("at"), "%Y-%m-%d %H:%M"), why=e.get("why")) for e in entries],
        note=RULE if entries else "The list is empty. " + RULE,
    )


@tool(
    annotations=ToolAnnotations(
        title="Change one of the player's lists", read_only_hint=False, destructive_hint=True, open_world_hint=False
    )
)
async def update_list(
    name: Name,
    add: Annotated[
        list[NewEntry] | None,
        Field(max_length=50, description="Entries to add, each an item (item_id, with its name as label) or anything "
              "else (label), with the player's note and why it's added. One that's on the list already isn't added "
              "again; with a note, its note is updated."),
    ] = None,
    remove: Annotated[
        list[int | str] | None,
        Field(max_length=MAX_ENTRIES, description="Entries to remove: item IDs, or labels (any case)."),
    ] = None,
    description: Annotated[
        str | None,
        Field(max_length=MAX_DESCRIPTION, description="What the list is for, e.g. 'Vendor items worth reselling on "
              "the auction house'. Left out: as it was; '' clears it."),
    ] = None,
    player: Player = None,
) -> str:
    """Change one of the player's lists, or make a new one: add entries, remove entries, say what it's for. Only when
    the player asks for it ('add X to Good flips', 'remove Y', 'make a shopping list') or agrees to your suggestion:
    the list is theirs. Never remove an entry on your own, not even one that's bad right now. Removing the last
    entry keeps the list, empty; delete_list deletes it."""
    if not any(c.isalnum() for c in name):
        raise ToolError("Nothing was changed: name the list in a few words, e.g. 'Good flips'.")
    try:
        add = TypeAdapter(list[NewEntry]).validate_python(add or [])  # as the MCP layer would
    except ValidationError as e:
        raise ToolError("Nothing was changed:\n" + "\n".join(
            f"add.{'.'.join(map(str, p['loc']))}: {p['msg'].removeprefix('Value error, ')}" for p in e.errors()
        )) from None
    who = await whose(player)
    now = time.time()
    made, added, updated, already, removed, unknown = False, [], [], [], [], []
    try:
        with Lists().change(who) as mine:
            found = next((x for x in _valid(mine) if same_name(x["name"], name)), None)
            if found is None:
                if remove:
                    raise _missing(who, name, _valid(mine))
                found = {"name": one_line(name), "at": now, "entries": []}
                mine.append(found)
                made = True
            if not isinstance(found.get("entries"), list):
                found["entries"] = []
            entries = found["entries"]
            for ref in remove or []:
                gone = [e for e in _entries(found) if matches(e, ref)]
                entries[:] = [e for e in entries if e not in gone]
                removed += gone
                if not gone:
                    unknown.append(repr(ref) if isinstance(ref, str) else f"{{item:{ref}}}")
            for new in add:
                old = on_list(_entries(found), new)
                if old is None:
                    if len(_entries(found)) >= MAX_ENTRIES:
                        raise ToolError(f"Nothing was changed: a list holds at most {MAX_ENTRIES} entries, and "
                                        f"{found['name']!r} would have more. Ask the player what to remove first.")
                    entries.append(new.saved(now))
                    added.append(describe(entries[-1]))
                    continue
                changed = False
                if new.item_id and not old.get("item"):  # a label alone until now: it's this item
                    old["item"], changed = new.item_id, True
                if new.label and not old.get("label"):
                    old["label"], changed = new.label, True
                if new.note and new.note != old.get("note"):
                    old["note"], changed = new.note, True
                (updated if changed else already).append(describe(old))
            if description is not None and (one_line(description) or None) != found.get("description"):
                found["description"] = one_line(description) or None
                if not made:
                    updated.append("what it's for")
            if found.get("description") is None:
                found.pop("description", None)
            count, list_name = len(_entries(found)), found["name"]
    except (OSError, ValueError) as e:
        raise ToolError(f"Couldn't change the lists: {e}") from None

    changes = []
    if added:
        changes.append(f"added {_listed(added)}")
    if removed:
        changes.append(f"removed {_listed([describe(e) for e in removed])}")
    if updated:
        changes.append(f"updated {_listed(updated)}")
    if made:
        result = f"Made the list {list_name!r} for {who}" + (f": {'; '.join(changes)}." if changes else ".")
    elif changes:
        result = f"Changed {who}'s list {list_name!r}: {'; '.join(changes)}."
    else:
        result = f"Nothing changed in {who}'s list {list_name!r}."
    if already:
        result += f" Already on it: {_listed(already)}."
    if unknown:
        result += f" Not on it: {_listed(unknown)}."
    result += f" It has {count} entr{'y' if count == 1 else 'ies'}."
    return result + (_views_note(list_name) if added or removed else "")


@tool(
    annotations=ToolAnnotations(
        title="Delete one of the player's lists", read_only_hint=False, destructive_hint=True, open_world_hint=False
    )
)
async def delete_list(name: Name, player: Player = None) -> str:
    """Delete one of the player's lists with all its entries. Only when the player asks for it: the list is theirs.
    To take entries off a list, update_list with remove."""
    who = await whose(player)
    try:
        with Lists().change(who) as mine:
            gone = [x for x in _valid(mine) if same_name(x["name"], name)]
            if not gone:
                raise _missing(who, name, _valid(mine))
            mine[:] = [x for x in mine if x not in gone]
    except (OSError, ValueError) as e:
        raise ToolError(f"Couldn't change the lists: {e}") from None
    entries = _entries(gone[0])
    result = f"Deleted {who}'s list {gone[0]['name']!r}"
    if entries:
        result += (f" with its {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}: "
                   + item_text.shorten(", ".join(describe(e) for e in entries), MAX_SHOWN))
    return result + "." + _views_note(gone[0]["name"], deleted=True)
