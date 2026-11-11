"""Visuals: what the AI shows the player in a window of its own (payload key "visuals", the Visuals window
in Messages.lua). A visual is a title and a stack of blocks shown top to bottom: tables that sort and scroll,
lists of items with values, short texts. The game keeps the last few to flip back through.

Blocks are a registry: a block type is one pydantic model here (in BLOCK_TYPES, told apart by its `type`, with
an encode() for the payload) and one renderer in the addon (BLOCKS[type] in Messages.lua).

The payload is written into every slot addon on every update, so it's kept small: a table row goes as one
string, its cells separated by line breaks (a cell is one line), and the visuals kept are cut to a budget.
"""

import math
import re
import time
from datetime import datetime
from typing import Annotated, Literal, Union

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, StrictStr, ValidationError, model_validator

from wow_mcp import item_text
from wow_mcp.app import guidance, tool, transport
from wow_mcp.tools.messages import delivery_note
from wow_mcp.transport import to_lua

guidance(
    "When an answer is naturally a table or a list of things with values, show it in game with visualize: "
    "comparing items or gear stats, prices and profits per item, entries with several values each (drop rates by "
    "mob, quest rewards, a vendor's items), before and after, the materials for a craft with their prices. Then "
    "keep your text answer to the conclusion and say the details are in the window. A visual is a stack of blocks "
    "in one window: 'table' (columns and rows; the player sorts by clicking a header), 'list' (bullet or numbered "
    "items, each with an optional value on the right) and 'text' (a short paragraph). Give columns and values clear "
    "names with units, and the type that fits: item for items (or {item:<id>} in text), money for gold amounts (in "
    "copper) and delta for gains and losses, percent, number, duration, time. Say where the data is from and how "
    "old it is in the subtitle, e.g. 'Auction house scan, 20 min ago'. Highlight what answers the question (the "
    "best choice, the item they have equipped). Don't make a visual for one or two values: just say them.",
    order=67,
)

KEEP = 5            # visuals the player can flip back through
MAX_BLOCKS = 8
MAX_COLUMNS = 10
MAX_ROWS = 200
MAX_ITEMS = 50      # in a list
MAX_TEXT = 200      # characters of a table cell or list item; the window shows what doesn't fit on hover
MAX_PARAGRAPH = 1000
MAX_SIZE = 40_000   # bytes one visual may take in the payload
MAX_KEPT = 64_000   # bytes all kept visuals may take; older ones go first
MAX_ITEM_ID = 2147483647

ValueType = Literal["text", "number", "percent", "money", "delta", "item", "time", "duration"]
NUMERIC = ("number", "percent", "money", "delta", "time", "duration")
Color = Literal["gold", "green", "red", "blue", "grey"]
Cell = StrictStr | StrictInt | StrictFloat | None
COIN = re.compile(r"^\s*([+-])?\s*(?:(\d+)\s*g)?\s*(?:(\d+)\s*s)?\s*(?:(\d+)\s*c)?\s*$", re.IGNORECASE)
ITEM = re.compile(r"^\s*(?:\{item:(\d+)(?::([^{}|\n]*))?\}|(\d+))\s*$")

VALUE_TYPES = (
    "text: words; may hold {item:<id>} markup. number. percent: in percent, 12.5 shows as 12.5%. money: copper, "
    "12005 shows as 1g 20s 5c with coin icons. delta: a signed change, green when positive and red when negative "
    "(unit: number, money or percent). item: an item ID (or {item:<id>:<name>}), shown as its icon and name in its "
    "quality colour, with its tooltip on hover. time: a unix time in seconds, shown as the local date and time. "
    "duration: seconds, shown like 1h 05m."
)
# What a value of each type should be, for errors.
HOW = {
    "number": "a number",
    "percent": "a number in percent, e.g. 12.5",
    "money": "copper as a number, e.g. 12005 for 1g 20s 5c",
    "delta": "a signed number",
    "time": "a unix time in seconds",
    "duration": "seconds as a number",
    "item": "an item ID, or {item:<id>:<name>}",
}
COLORS = ("gold: the answer or best choice; green: good; red: bad; blue: for information, e.g. what they have now; "
          "grey: less relevant.")

Unit = Annotated[
    Literal["number", "money", "percent"],
    Field(description="For delta values: what changed, a number, money (copper) or a percent."),
]
Decimals = Annotated[
    int | None,
    Field(ge=0, le=4, description="Decimals for number, percent and delta values. Left out: up to 2, as needed."),
]


# ---------------------------------------------------------------------------
# Values, as table cells and list values have them
# ---------------------------------------------------------------------------


def _number(value: object, kind: str) -> float | None:
    """A numeric value, or None if it isn't one. Numbers written as text count too: '12.5', '12.5%' for a percent,
    '1g 20s 5c' for money, an ISO date for a time."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if kind in ("percent", "delta"):
        text = text.removesuffix("%").strip()
    try:
        number = float(text)
        return number if math.isfinite(number) else None
    except ValueError:
        pass
    if kind in ("money", "delta") and (match := COIN.match(text)) and any(match.groups()[1:]):
        sign, gold, silver, copper = match.groups()
        amount = int(gold or 0) * 10000 + int(silver or 0) * 100 + int(copper or 0)
        return float(-amount if sign == "-" else amount)
    if kind == "time":
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return None
    return None


def _item(value: object) -> tuple[int, str | None] | None:
    """An item value's ID and the name given with it, or None if it isn't one."""
    if isinstance(value, int) and not isinstance(value, bool):
        return (value, None) if 0 < value <= MAX_ITEM_ID else None
    if isinstance(value, str) and (match := ITEM.match(value)):
        item_id = int(match.group(1) or match.group(3))
        name = " ".join((match.group(2) or "").split()) or None
        return (item_id, name) if 0 < item_id <= MAX_ITEM_ID else None
    return None


def _text(value: object, limit: int = MAX_TEXT) -> str:
    """Text on one line, as the game shows it."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return item_text.shorten(" ".join(str(value).split()), limit)


def _num(value: float) -> str:
    """A number as short as it goes: 12, 12.5, 1e-05."""
    if value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return format(value, ".10g")


def _wrong(kind: str, unit: str, value: object) -> str | None:
    """What's wrong with a value for its type, if anything."""
    if value is None:
        return None
    if kind == "item":
        if _item(value) is None:
            return f"{value!r} isn't an item: give {HOW['item']}"
    elif kind in NUMERIC and _number(value, kind) is None:
        how = "copper as a number, e.g. -2500 for -25s" if kind == "delta" and unit == "money" else HOW[kind]
        return f"{value!r} isn't {how} (use null for an empty one)"
    return None


def _value(kind: str, unit: str, value: object) -> str:
    """A value as the game reads it: an item as "<id>" or "<id>:<name>", a number in copper or seconds where it
    counts those. Anything else is text, which only totals have in item and number columns ('Total', 'n/a')."""
    if value is None:
        return ""
    if kind == "item" and (item := _item(value)):
        item_id, name = item
        return f"{item_id}:{name}" if name else str(item_id)
    if kind in NUMERIC and (number := _number(value, kind)) is not None:
        if kind in ("money", "time") or (kind == "delta" and unit == "money"):
            number = float(round(number))
        return _num(number)
    return _text(value)


def _align(kind: str, align: str | None) -> str:
    return align or ("right" if kind in NUMERIC else "left")


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------


class _Block(BaseModel):
    model_config = ConfigDict(extra="forbid")

    heading: Annotated[
        str | None,
        Field(max_length=60, description="A small heading above the block, when the visual has several. "
              + item_text.HINT),
    ] = None

    def encode(self) -> dict:
        """The block as the payload carries it: its type and heading, and what its renderer reads."""
        return {"type": self.type, "heading": self.heading, **self.content()}

    def content(self) -> dict:
        raise NotImplementedError

    def describe(self) -> str:
        """The block in a few words, for the tool's answer."""
        raise NotImplementedError


class Column(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[
        str,
        Field(min_length=1, max_length=40, description="The header, with the unit when it helps, e.g. 'Profit', "
              "'Drop %', 'Stamina'. " + item_text.HINT),
    ]
    type: Annotated[ValueType, Field(description=VALUE_TYPES)] = "text"
    align: Annotated[
        Literal["left", "center", "right"] | None,
        Field(description="Left out: numbers, money and times right, text and items left."),
    ] = None
    width: Annotated[
        int | None,
        Field(ge=30, le=400, description="A fixed width in pixels (about 6 per character). Left out: it fits the "
              "content, up to 240."),
    ] = None
    decimals: Decimals = None
    unit: Unit = "number"


class Highlight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    row: Annotated[int, Field(ge=1, description="The row's number in rows, 1 = the first.")]
    label: Annotated[
        str | None,
        Field(max_length=40, description="Why it stands out, shown when the player hovers it, e.g. 'Best value', "
              "'Equipped'."),
    ] = None
    color: Annotated[Color, Field(description=COLORS)] = "gold"


class Sort(BaseModel):
    model_config = ConfigDict(extra="forbid")

    column: Annotated[StrictStr | StrictInt, Field(description="The column's name, or its number (1 = the first).")]
    order: Annotated[
        Literal["asc", "desc"] | None,
        Field(description="Left out: like a click on the header: A to Z for text and items, highest first for "
              "numbers."),
    ] = None


class TableBlock(_Block):
    """A table: columns of a type each, rows of values. The player sorts it by clicking a header (by the values:
    money by copper, items by name), and it scrolls its rows when they're many."""

    type: Literal["table"]
    columns: Annotated[
        list[Column],
        Field(min_length=1, max_length=MAX_COLUMNS, description="The columns, left to right: a name and a type each."),
    ]
    rows: Annotated[
        list[list[Cell]],
        Field(min_length=1, max_length=MAX_ROWS, description="The rows, each a list with one value per column, null "
              "for an empty cell. Text cells may hold {item:<id>} markup; money is copper."),
    ]
    totals: Annotated[
        list[Cell] | None,
        Field(description="A totals row, set apart under the rows: one value per column, e.g. "
              "['Total', null, 152000]. It may hold text in any column."),
    ] = None
    highlight: Annotated[
        list[Highlight],
        Field(max_length=MAX_ROWS, description="Rows to mark: the best choice, the one they have equipped."),
    ] = []
    sort: Annotated[
        Sort | None,
        Field(description="How the rows are sorted when it opens. Left out: in the order given."),
    ] = None

    @model_validator(mode="after")
    def _fits(self) -> "TableBlock":
        problems = []
        width = len(self.columns)
        for n, row in enumerate(self.rows, 1):
            if len(row) != width:
                problems.append(f"row {n} has {len(row)} cells, but there are {width} columns (use null for an "
                                "empty cell)")
                continue
            for column, value in zip(self.columns, row):
                if problem := _wrong(column.type, column.unit, value):
                    problems.append(f"row {n}, column {column.name!r}: {problem}")
        if self.totals is not None and len(self.totals) != width:
            problems.append(f"totals has {len(self.totals)} cells, but there are {width} columns")
        marked = set()
        for h in self.highlight:
            if h.row > len(self.rows):
                problems.append(f"highlight row {h.row} isn't there: there are {len(self.rows)} rows")
            elif h.row in marked:
                problems.append(f"row {h.row} is highlighted twice")
            marked.add(h.row)
        if self.sort is not None and self.sort_column() is None:
            names = ", ".join(repr(c.name) for c in self.columns)
            problems.append(f"sort column {self.sort.column!r} isn't one of the columns ({names}; or 1-{width})")
        if problems:
            more = f"\n(and {len(problems) - 12} more)" if len(problems) > 12 else ""
            raise ValueError("\n".join(problems[:12]) + more)
        return self

    def sort_column(self) -> int | None:
        """The number of the column to sort by, 1 = the first."""
        if self.sort is None:
            return None
        wanted = self.sort.column
        if isinstance(wanted, int):
            return wanted if 1 <= wanted <= len(self.columns) else None
        for n, column in enumerate(self.columns, 1):
            if column.name.strip().lower() == wanted.strip().lower():
                return n
        return None

    def _row(self, cells: list) -> str:
        """A row as one string, its cells separated by line breaks: far smaller in the payload than a list."""
        return "\n".join(_value(c.type, c.unit, v) for c, v in zip(self.columns, cells))

    def content(self) -> dict:
        sent: dict = {
            "columns": [
                {"name": c.name, "type": c.type, "align": _align(c.type, c.align), "width": c.width,
                 "decimals": c.decimals, "unit": c.unit if c.type == "delta" else None}
                for c in self.columns
            ],
            "rows": [self._row(row) for row in self.rows],
        }
        if self.totals is not None:
            sent["totals"] = self._row(self.totals)
        if self.highlight:
            sent["highlight"] = [{"row": h.row, "label": h.label, "color": h.color} for h in self.highlight]
        if (column := self.sort_column()) is not None:
            kind = self.columns[column - 1].type
            sent["sort"] = {"column": column, "dir": self.sort.order or ("asc" if kind in ("text", "item") else "desc")}
        return sent

    def describe(self) -> str:
        rows = f"{len(self.rows)} row{'s' if len(self.rows) != 1 else ''}"
        more = []
        if (column := self.sort_column()) is not None:
            more.append(f"sorted by {self.columns[column - 1].name} ({self.content()['sort']['dir']})")
        if self.highlight:
            more.append(f"{len(self.highlight)} highlighted")
        if self.totals is not None:
            more.append("with totals")
        return f"a table ({rows} x {len(self.columns)} columns{', ' + ', '.join(more) if more else ''})"


class ListItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Annotated[str, Field(min_length=1, max_length=MAX_TEXT, description="The item's line. " + item_text.HINT)]
    value: Annotated[
        Cell, Field(description="Shown on the right, as the list's value_type says. null for none.")
    ] = None
    highlight: Annotated[Color | None, Field(description="Mark this item: " + COLORS)] = None


class ListBlock(_Block):
    """A list: bullet or numbered items, each with an optional value on the right (money, a delta, a percent...),
    e.g. the materials for a craft with their prices, or the steps of a rotation."""

    type: Literal["list"]
    items: Annotated[list[ListItem], Field(min_length=1, max_length=MAX_ITEMS)]
    style: Annotated[Literal["bullet", "numbered"], Field(description="Bullets, or numbers 1, 2, 3...")] = "bullet"
    value_type: Annotated[ValueType, Field(description="The type of the items' values. " + VALUE_TYPES)] = "number"
    unit: Unit = "number"
    decimals: Decimals = None

    @model_validator(mode="after")
    def _fits(self) -> "ListBlock":
        problems = [f"item {n}: {problem}" for n, item in enumerate(self.items, 1)
                    if (problem := _wrong(self.value_type, self.unit, item.value))]
        if problems:
            raise ValueError("\n".join(problems[:12]))
        return self

    def content(self) -> dict:
        return {
            "style": self.style,
            "value": {"type": self.value_type, "unit": self.unit if self.value_type == "delta" else None,
                      "decimals": self.decimals},
            "items": [{"text": _text(i.text), "value": _value(self.value_type, self.unit, i.value) or None,
                       "mark": i.highlight} for i in self.items],
        }

    def describe(self) -> str:
        return f"a {'numbered ' if self.style == 'numbered' else ''}list ({len(self.items)} items)"


class TextBlock(_Block):
    """A short paragraph: a conclusion, a caveat, what to do next."""

    type: Literal["text"]
    text: Annotated[str, Field(min_length=1, max_length=MAX_PARAGRAPH, description="Plain text; line breaks start "
                               "new lines. " + item_text.HINT)]

    def content(self) -> dict:
        return {"text": "\n".join(" ".join(line.split()) for line in self.text.strip().splitlines())}

    def describe(self) -> str:
        return "a text"


# The block types: one model each, told apart by `type`. A new one needs its model here and its renderer
# in the addon (BLOCKS in Messages.lua).
BLOCK_TYPES = (TableBlock, ListBlock, TextBlock)
Block = Annotated[Union[BLOCK_TYPES], Field(discriminator="type")]  # noqa: UP007 (a union of a tuple)


class Visual(BaseModel):
    """A visual: a title and its blocks, top to bottom."""

    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, Field(min_length=1, max_length=80)]
    subtitle: Annotated[str | None, Field(max_length=200)] = None
    blocks: Annotated[list[Block], Field(min_length=1, max_length=MAX_BLOCKS)]

    def encode(self, at: int) -> dict:
        """The visual as the payload carries it, but for its id."""
        return {"title": self.title, "subtitle": self.subtitle, "at": at,
                "blocks": [block.encode() for block in self.blocks]}


def size(value: object) -> int:
    """The bytes it takes in the payload."""
    return len(to_lua(value).encode("utf-8"))


def kept_visuals(state: dict) -> list[dict]:
    """The visuals in the server's state, oldest first."""
    return [v for v in (state.get("visuals") or {}).get("list") or [] if isinstance(v, dict)]


def _problems(error: ValidationError) -> str:
    lines = []
    for e in error.errors():
        where = ".".join(str(part) for part in e["loc"])
        message = e["msg"].removeprefix("Value error, ")
        lines.append(f"{where}: {message}" if where else message)
    return "\n".join(lines)


def _listed(things: list[str]) -> str:
    return things[0] if len(things) == 1 else ", ".join(things[:-1]) + " and " + things[-1]


@tool(
    annotations=ToolAnnotations(
        title="Show a table, list or text in game", read_only_hint=False, destructive_hint=False,
        open_world_hint=False,
    )
)
def visualize(
    title: Annotated[
        str,
        Field(min_length=1, max_length=80, description="What it shows, e.g. 'Leather gloves for you' or 'Profit per "
              "craft'. " + item_text.HINT),
    ],
    blocks: Annotated[
        list[Block],
        Field(min_length=1, max_length=MAX_BLOCKS, description="What to show, top to bottom; each block has a "
              "'type': 'table', 'list' or 'text'. Usually one table or list, maybe with a text for the conclusion."),
    ],
    subtitle: Annotated[
        str | None,
        Field(max_length=200, description="A line under the title: where the data is from and how old it is, e.g. "
              "'Auction house scan, 20 min ago' or 'Wowhead (Forever) drop rates'. " + item_text.HINT),
    ] = None,
) -> str:
    """Show the player a visual in World of Warcraft, in a window of its own: a title and a stack of blocks you
    choose, whatever fits the question. A table (comparing gear stats, auction house profits, drop rates, quest
    rewards: the player can sort it by any column, scroll it and hover items for their tooltip), a list of
    things with values (materials and their prices), a short text.

    It opens when it arrives; the player can flip back through the last 5 visuals, so a new one doesn't lose
    the one before.
    """
    try:
        visual = Visual(title=title, subtitle=subtitle, blocks=blocks)
    except ValidationError as e:
        raise ToolError("Nothing was sent:\n" + _problems(e)) from None
    entry = visual.encode(int(time.time()))
    if (taken := size(entry)) > MAX_SIZE:
        raise ToolError(
            f"Nothing was sent: it takes {taken // 1000} KB, and at most {MAX_SIZE // 1000} KB fit in the game's "
            "updates. Shorten long texts, leave out columns or rows, or split it in two."
        )
    dropped: list[int] = []

    def change(state: dict, seq: int) -> None:
        kept = [*kept_visuals(state)[-(KEEP - 1):], {"id": seq, **entry}]
        while len(kept) > 1 and size(kept) > MAX_KEPT:
            dropped.append(kept.pop(0).get("id"))
        state["visuals"] = {"id": seq, "list": kept}

    visual_id = transport().publish(change)
    result = (
        f"Visual #{visual_id} sent to the game: {title!r}, with {_listed([b.describe() for b in visual.blocks])}. It "
        f"opens in its own window, where the player can sort tables, scroll, and flip back through the last {KEEP}."
    )
    if dropped:
        result += f" Older visuals were dropped to keep the game's updates small: #{', #'.join(map(str, dropped))}."
    return result + delivery_note()


@tool(
    annotations=ToolAnnotations(
        title="Clear in-game visuals", read_only_hint=False, destructive_hint=True, open_world_hint=False
    )
)
def clear_visuals(
    visual_id: Annotated[
        int | None,
        Field(gt=0, description="The visual to remove, by the number visualize gave it. Left out: all of them."),
    ] = None,
) -> str:
    """Remove visuals shown with visualize from the player's game: one, or all of them (the window closes)."""
    kept = kept_visuals(transport().load_state())
    if not kept:
        return "There are no visuals in the game."  # nothing to send: an update would use up a slot
    if visual_id is not None and not any(v.get("id") == visual_id for v in kept):
        listed = ", ".join(f"#{v.get('id')} {v.get('title')!r}" for v in kept)
        raise ToolError(f"There's no visual #{visual_id}. The game has: {listed}.")
    removed: list[dict] = []

    def change(state: dict, seq: int) -> None:
        visuals = kept_visuals(state)
        removed.extend(v for v in visuals if visual_id is None or v.get("id") == visual_id)
        state["visuals"] = {"id": seq, "list": [v for v in visuals if v not in removed]}

    transport().publish(change)
    if visual_id is not None:
        return f"Removed visual #{visual_id} from the game." + delivery_note()
    return f"Cleared {len(removed)} visual{'s' if len(removed) != 1 else ''} from the game." + delivery_note()
