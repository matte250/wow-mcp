"""Online auction house prices from Blizzard's API (battlenet.py): snapshots of the player's auction house, kept as
price history in a SQLite file under ~/.cache/wow-mcp/, and the wow-mcp-auctions command.

Forever's API has no realms. Each region lists 15 auction houses, five each named "Alliance Auction House", "Horde
Auction House" and "Blackwater Auction House", the same under every ruleset. Nothing says which one is the player's,
so it's worked out from what the game saw: get_auction_prices records the in-game prices it gets (tools/auction.py),
and the addon's last full scan as saved on disk (WowMCP.lua in SavedVariables, written at /reload and logout) counts
too. Each candidate (the houses of the player's faction, and the neutral Blackwater ones) is compared item by item
with its snapshot nearest in time: the cheapest price, how many are listed and in how many auctions. The best match
is kept once it's clear; until then there are no online prices, never a guess. BATTLENET_AUCTION_HOUSE=<id> in
battlenet.env overrides it.

A house's snapshot changes about once an hour (its Last-Modified). Every one fetched is kept: per item, the cheapest
buyout per item, how many are listed in how many auctions, the cheapest price points, and for random-suffix gear the
same per suffix (the API's bonus IDs: the in-game suffix IDs are another numbering, so the two are never paired).
Single items and commodities are separate snapshots, as the API has them.
"""

import argparse
import contextlib
import json
import logging
import math
import os
import re
import sqlite3
import statistics
import sys
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wow_mcp import battlenet, game, locking
from wow_mcp.battlenet import BattleNetError
from wow_mcp.install import ADDONS_DIR_ENV
from wow_mcp.transport import ADDON

log = logging.getLogger("wow_mcp.online_auctions")

DB_NAME = "auctions.sqlite3"
KINDS = ("items", "commodities")
HOUR, DAY = 3600, 86400
MAX_AGE = HOUR  # a snapshot this old is checked for a newer one...
RECHECK = 5 * 60  # ...at most this often while Blizzard has none
SETTLE = 120  # seconds after a snapshot is due that the background refresh asks for it
REFRESH_INTERVAL = HOUR  # the background refresh's longest wait
LOCK_WAIT = 120.0  # seconds to wait for another process's refresh
HOUSES_EVERY = DAY  # how often the list of houses is fetched again
RULESETS_EVERY = 7 * DAY
MAX_TIERS = 6  # the cheapest price points kept per item
USUAL_DAYS = 7  # the usual price is the median over this many days
HISTORY_DAYS = 14  # how far back history goes (per day before the last 24 hours)
EVIDENCE_DAYS = 7  # in-game prices this recent are compared with the houses
KEEP_SEEN_DAYS = 30
MIN_EVIDENCE = 20  # in-game prices needed before a house counts as the player's
MIN_SCORE = 0.5
MIN_CONFIDENCE = 0.99
NEUTRAL = "Blackwater"


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "wow-mcp"


def ago(at: float, now: float | None = None) -> str:
    minutes = max(0, int((time.time() if now is None else now) - at)) // 60
    if minutes < 1:
        return "just now"
    if minutes < 120:
        return f"{minutes} minute{'s' if minutes > 1 else ''} ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} hours ago"
    return f"{minutes // 1440} days ago"


def utc(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# Snapshots ---------------------------------------------------------------------------------


@dataclass
class Listing:
    """An item, or one random suffix of it, in a snapshot."""

    price: int | None  # the cheapest buyout per item, in copper; None: only bids
    quantity: int
    auctions: int
    bid_only: int = 0  # how many are listed with a bid but no buyout
    suffixes: int = 0  # the whole item's row: how many random suffixes are listed (0: none)
    tiers: list[tuple[int, int]] = field(default_factory=list)  # the cheapest price points: (price, how many)


def _suffix(item: dict) -> str:
    """A random suffix ("of the Bear") as the API has it: the item's bonus IDs. Context and modifiers vary between
    auctions of plain items too, so they aren't part of it."""
    bonus = item.get("bonus_lists")
    return ":".join(str(b) for b in sorted(b for b in bonus if isinstance(b, int))) if isinstance(bonus, list) else ""


def summarize(auctions: Any, commodities: bool) -> dict[tuple[int, str], Listing]:
    """One of the API's auction lists per (item ID, suffix): suffix "" is the whole item, and random-suffix gear also
    has a row per suffix. Commodities have a unit price; single items a buyout for the auction (or only a bid)."""
    rows: dict[tuple[int, str], Listing] = {}
    points: dict[int, Counter] = defaultdict(Counter)
    suffixes: dict[int, set[str]] = defaultdict(set)
    for auction in auctions if isinstance(auctions, list) else []:
        item = auction.get("item") if isinstance(auction, dict) else None
        quantity = auction.get("quantity") if isinstance(item, dict) else None
        if not isinstance(item, dict) or not isinstance(item.get("id"), int) or not isinstance(quantity, int) \
                or quantity <= 0:
            continue
        total = auction.get("unit_price") if commodities else auction.get("buyout")
        each = None
        if isinstance(total, (int, float)) and total > 0:
            each = int(total) if commodities else math.floor(total / quantity + 0.5)
        item_id, suffix = item["id"], _suffix(item)
        if suffix:
            suffixes[item_id].add(suffix)
        for key in ("", suffix) if suffix else ("",):
            row = rows.get((item_id, key)) or rows.setdefault((item_id, key), Listing(None, 0, 0))
            row.quantity += quantity
            row.auctions += 1
            if each is None:
                row.bid_only += quantity
            elif row.price is None or each < row.price:
                row.price = each
        if each is not None:
            points[item_id][each] += quantity
    for item_id, counts in points.items():
        rows[(item_id, "")].tiers = sorted(counts.items())[:MAX_TIERS]
    for item_id, found in suffixes.items():
        rows[(item_id, "")].suffixes = len(found)
    return rows


@dataclass(frozen=True)
class Snapshot:
    id: int
    region: str
    house: int
    kind: str  # "items" or "commodities"
    at: int  # its Last-Modified: when Blizzard took it
    fetched: int
    ruleset: int
    auctions: int
    items: int
    bytes: int


@dataclass(frozen=True)
class House:
    id: int
    name: str

    @property
    def label(self) -> str:
        return f"{self.name} {self.id}"


@dataclass
class Seen:
    """An item's price as the game saw it: the evidence for which house is the player's."""

    item: int
    at: int
    price: int | None
    quantity: int
    auctions: int | None
    source: str  # "full scan" or "search"
    faction: str | None = None


@dataclass
class Match:
    house: int | None  # the best match; None if there was nothing to compare
    clear: bool  # sure enough to use
    confidence: float | None  # how sure it beats the runner-up (0.5: a coin toss, 1: certain)
    items: int  # in-game prices compared
    scores: dict[int, float]  # per house, how alike its prices were (1: the same)
    note: str
    at: int
    region: str = ""


SCHEMA = """
CREATE TABLE IF NOT EXISTS houses (
    region TEXT NOT NULL, id INTEGER NOT NULL, name TEXT NOT NULL, seen INTEGER NOT NULL,
    PRIMARY KEY (region, id));
CREATE TABLE IF NOT EXISTS rulesets (
    region TEXT NOT NULL, id INTEGER NOT NULL, name TEXT NOT NULL, seen INTEGER NOT NULL,
    PRIMARY KEY (region, id));
-- One per house, kind (items or commodities) and Last-Modified.
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    region TEXT NOT NULL, house INTEGER NOT NULL, kind TEXT NOT NULL,
    at INTEGER NOT NULL, fetched INTEGER NOT NULL, ruleset INTEGER NOT NULL,
    auctions INTEGER NOT NULL, items INTEGER NOT NULL, bytes INTEGER NOT NULL,
    UNIQUE (region, house, kind, at));
-- Per item (suffix '') and per random suffix of it (the API's bonus IDs, e.g. '12730').
CREATE TABLE IF NOT EXISTS prices (
    snapshot INTEGER NOT NULL REFERENCES snapshots (id) ON DELETE CASCADE,
    item INTEGER NOT NULL, suffix TEXT NOT NULL,
    price INTEGER, quantity INTEGER NOT NULL, auctions INTEGER NOT NULL, bid_only INTEGER NOT NULL,
    suffixes INTEGER NOT NULL, tiers TEXT,
    PRIMARY KEY (snapshot, item, suffix)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS prices_by_item ON prices (item, suffix, snapshot);
-- When each house's snapshots were last asked for: a 304 makes no snapshot.
CREATE TABLE IF NOT EXISTS checks (
    region TEXT NOT NULL, house INTEGER NOT NULL, kind TEXT NOT NULL, at INTEGER NOT NULL,
    PRIMARY KEY (region, house, kind));
-- Prices the game saw, the evidence for which house is the player's.
CREATE TABLE IF NOT EXISTS seen (
    realm TEXT NOT NULL, at INTEGER NOT NULL, item INTEGER NOT NULL, source TEXT NOT NULL,
    price INTEGER, quantity INTEGER NOT NULL, auctions INTEGER, faction TEXT,
    PRIMARY KEY (realm, at, item, source)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS matches (
    realm TEXT PRIMARY KEY, region TEXT NOT NULL, house INTEGER, clear INTEGER NOT NULL, confidence REAL,
    items INTEGER NOT NULL, scores TEXT NOT NULL, note TEXT NOT NULL, at INTEGER NOT NULL);
-- The player's last realm, faction and region, and which saved full scan was read last.
CREATE TABLE IF NOT EXISTS facts (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _tiers(text: str | None) -> list[tuple[int, int]]:
    tiers = []
    for part in (text or "").split(","):
        price, _, count = part.partition(":")
        if price.isdigit() and count.isdigit():
            tiers.append((int(price), int(count)))
    return tiers


def _listing(row: sqlite3.Row) -> Listing:
    return Listing(row["price"], row["quantity"], row["auctions"], row["bid_only"], row["suffixes"],
                   _tiers(row["tiers"]))


class Store:
    """The SQLite file: houses, snapshots and their prices, the in-game evidence, and each realm's house."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else cache_dir() / DB_NAME
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=60, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def fact(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM facts WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_facts(self, **values: str | None) -> None:
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO facts VALUES (?, ?)",
                                [(k, v) for k, v in values.items() if v is not None])

    def houses(self, region: str) -> tuple[list[House], int | None]:
        rows = self.db.execute("SELECT id, name, seen FROM houses WHERE region = ? ORDER BY id", (region,)).fetchall()
        return [House(r["id"], r["name"]) for r in rows], min((r["seen"] for r in rows), default=None)

    def set_houses(self, region: str, houses: list[dict], now: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM houses WHERE region = ?", (region,))
            self.db.executemany("INSERT INTO houses VALUES (?, ?, ?, ?)",
                                [(region, h["id"], h["name"], now) for h in houses])

    def rulesets(self, region: str) -> tuple[list[dict], int | None]:
        rows = self.db.execute("SELECT id, name, seen FROM rulesets WHERE region = ? ORDER BY id", (region,)).fetchall()
        return [{"id": r["id"], "name": r["name"]} for r in rows], min((r["seen"] for r in rows), default=None)

    def set_rulesets(self, region: str, rulesets: list[dict], now: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM rulesets WHERE region = ?", (region,))
            self.db.executemany("INSERT INTO rulesets VALUES (?, ?, ?, ?)",
                                [(region, r["id"], r["name"], now) for r in rulesets])

    def checked(self, region: str, house: int, kind: str) -> int | None:
        row = self.db.execute("SELECT at FROM checks WHERE region = ? AND house = ? AND kind = ?",
                              (region, house, kind)).fetchone()
        return row[0] if row else None

    def mark_checked(self, region: str, house: int, kind: str, at: int) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO checks VALUES (?, ?, ?, ?)", (region, house, kind, at))

    def _snapshot(self, where: str, args: tuple) -> Snapshot | None:
        row = self.db.execute(f"SELECT * FROM snapshots WHERE {where}", args).fetchone()
        return Snapshot(**dict(row)) if row else None

    def latest(self, region: str, house: int, kind: str) -> Snapshot | None:
        return self._snapshot("region = ? AND house = ? AND kind = ? ORDER BY at DESC LIMIT 1", (region, house, kind))

    def nearest(self, region: str, house: int, kind: str, at: int) -> Snapshot | None:
        before = self._snapshot("region = ? AND house = ? AND kind = ? AND at <= ? ORDER BY at DESC LIMIT 1",
                                (region, house, kind, at))
        after = self._snapshot("region = ? AND house = ? AND kind = ? AND at > ? ORDER BY at LIMIT 1",
                               (region, house, kind, at))
        return min((s for s in (before, after) if s), key=lambda s: abs(s.at - at), default=None)

    def add_snapshot(self, region: str, house: int, kind: str, ruleset: int, at: int, fetched: int,
                     rows: dict[tuple[int, str], Listing], size: int) -> tuple[Snapshot, bool]:
        """Keeps a snapshot unless it's there already; whether it was new."""
        auctions = sum(listing.auctions for (_, suffix), listing in rows.items() if not suffix)
        items = sum(1 for _, suffix in rows if not suffix)
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO snapshots (region, house, kind, at, fetched, ruleset, auctions, items, bytes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (region, house, kind, at, fetched, ruleset, auctions, items, size))
            new = cursor.rowcount == 1
            if new:
                self.db.executemany(
                    "INSERT INTO prices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ((cursor.lastrowid, item, suffix, x.price, x.quantity, x.auctions, x.bid_only, x.suffixes,
                      ",".join(f"{p}:{n}" for p, n in x.tiers) or None) for (item, suffix), x in rows.items()))
        snapshot = self._snapshot("region = ? AND house = ? AND kind = ? AND at = ?", (region, house, kind, at))
        assert snapshot
        return snapshot, new

    def snapshots(self, region: str, house: int) -> list[Snapshot]:
        rows = self.db.execute("SELECT * FROM snapshots WHERE region = ? AND house = ? ORDER BY at",
                               (region, house)).fetchall()
        return [Snapshot(**dict(r)) for r in rows]

    def listings(self, snapshot: int, items: Iterable[int] | None = None) -> dict[int, Listing]:
        """The whole items' rows of a snapshot: all of them, or these items'."""
        if items is None:
            rows = self.db.execute("SELECT * FROM prices WHERE snapshot = ? AND suffix = ''", (snapshot,)).fetchall()
        else:
            items = list(items)
            rows = self.db.execute(
                f"SELECT * FROM prices WHERE snapshot = ? AND suffix = '' AND item IN ({','.join('?' * len(items))})",
                (snapshot, *items)).fetchall() if items else []
        return {r["item"]: _listing(r) for r in rows}

    def suffixes(self, snapshot: int, item: int) -> dict[str, Listing]:
        rows = self.db.execute("SELECT * FROM prices WHERE snapshot = ? AND item = ? AND suffix != ''",
                               (snapshot, item)).fetchall()
        return {r["suffix"]: _listing(r) for r in rows}

    def last_listed(self, region: str, house: int, item: int) -> tuple[int, Listing] | None:
        row = self.db.execute(
            "SELECT s.at, p.* FROM prices p JOIN snapshots s ON s.id = p.snapshot WHERE p.item = ? AND p.suffix = '' "
            "AND s.region = ? AND s.house = ? ORDER BY s.at DESC LIMIT 1", (item, region, house)).fetchone()
        return (row["at"], _listing(row)) if row else None

    def series(self, region: str, house: int, item: int, since: int) -> list[tuple[int, Listing | None]]:
        """The item in every snapshot of its kind since then, oldest first; None where none were listed."""
        kind = self.db.execute(
            "SELECT s.kind FROM prices p JOIN snapshots s ON s.id = p.snapshot WHERE p.item = ? AND p.suffix = '' "
            "AND s.region = ? AND s.house = ? ORDER BY s.at DESC LIMIT 1", (item, region, house)).fetchone()
        if not kind:
            return []
        rows = self.db.execute(
            "SELECT s.at, p.* FROM snapshots s LEFT JOIN prices p ON p.snapshot = s.id AND p.item = ? "
            "AND p.suffix = '' WHERE s.region = ? AND s.house = ? AND s.kind = ? AND s.at >= ? ORDER BY s.at",
            (item, region, house, kind[0], since)).fetchall()
        return [(r["at"], _listing(r) if r["quantity"] is not None else None) for r in rows]

    def record(self, realm: str, seen: Iterable[Seen]) -> None:
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO seen VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                [(realm, s.at, s.item, s.source, s.price, s.quantity, s.auctions, s.faction)
                                 for s in seen])
            # Only recent evidence is compared; the snapshots are what's kept.
            self.db.execute("DELETE FROM seen WHERE at < ?", (int(time.time()) - KEEP_SEEN_DAYS * DAY,))

    def evidence(self, realm: str, since: int) -> list[Seen]:
        rows = self.db.execute("SELECT * FROM seen WHERE realm = ? AND at >= ? ORDER BY at, item, source",
                               (realm, since)).fetchall()
        return [Seen(r["item"], r["at"], r["price"], r["quantity"], r["auctions"], r["source"], r["faction"])
                for r in rows]

    def count_seen(self) -> int:
        return self.db.execute("SELECT count(*) FROM seen").fetchone()[0]

    def match(self, realm: str) -> Match | None:
        row = self.db.execute("SELECT * FROM matches WHERE realm = ?", (realm,)).fetchone()
        if not row:
            return None
        scores = {int(k): v for k, v in json.loads(row["scores"]).items()}
        return Match(row["house"], bool(row["clear"]), row["confidence"], row["items"], scores, row["note"],
                     row["at"], row["region"])

    def save_match(self, realm: str, region: str, match: Match) -> None:
        match.region = region
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO matches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (realm, region, match.house, int(match.clear), match.confidence, match.items,
                             json.dumps(match.scores), match.note, match.at))


# Matching the player's house -----------------------------------------------------------------


def _closeness(a: float | None, b: float | None) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return min(a, b) / max(a, b)


def similarity(seen: Seen, listing: Listing | None) -> float:
    """How alike an item looked in game and in a house's snapshot, from 0 to 1 (the same): its cheapest price, how
    many were listed and in how many auctions. The game leaves the player's own auctions out; Blizzard doesn't."""
    listing = listing or Listing(None, 0, 0)
    parts = [_closeness(seen.price, listing.price), _closeness(seen.quantity, listing.quantity)]
    if seen.auctions is not None:
        parts.append(_closeness(seen.auctions, listing.auctions))
    return sum(parts) / len(parts)


def confidence(best: list[float], other: list[float]) -> float:
    """How sure it is that the best house really fits the in-game prices better than the other: a paired test of the
    per-item differences (0.5: a coin toss, 1: certain)."""
    diffs = [a - b for a, b in zip(best, other, strict=True)]
    if not diffs:
        return 0.5
    mean = statistics.fmean(diffs)
    spread = statistics.stdev(diffs) if len(diffs) > 1 else 0.0
    if spread == 0:
        return 1.0 if mean > 0 else 0.5
    z = mean / (spread / math.sqrt(len(diffs)))
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _sure(confidence: float | None) -> str:
    # A test over thousands of prices rounds to 100%, which overstates it.
    if confidence is None:
        return "not sure"
    return "over 99.99% sure" if confidence >= 0.9999 else f"{confidence:.2%} sure"


def judge(scored: dict[int, list[float]], houses: dict[int, House], now: int, gap: int = 0) -> Match:
    """The best match among the houses, from each one's similarity per in-game price, and whether it's clear."""
    items = len(next(iter(scored.values()), []))
    if not scored or not items:
        return Match(None, False, None, items, {}, "There was nothing to compare the in-game prices with.", now)
    scores = {h: round(statistics.fmean(s), 3) for h, s in scored.items()}
    ranked = sorted(scores, key=lambda h: (-scores[h], h))
    best, runner = ranked[0], ranked[1] if len(ranked) > 1 else None
    sure = round(confidence(scored[best], scored[runner] if runner else [0.0] * items), 4)
    clear = items >= MIN_EVIDENCE and scores[best] >= MIN_SCORE and sure >= MIN_CONFIDENCE
    listed = ", ".join(f"{houses[h].label} {scores[h]:.2f}" for h in ranked)
    when = f", with snapshots up to {gap // 60} minutes from when the game saw them" if gap >= 60 else ""
    if clear:
        note = (f"{houses[best].label} matches the {items} in-game prices best ({_sure(sure)}{when}; "
                f"how alike, 1 being the same: {listed}).")
    elif items < MIN_EVIDENCE:
        note = (f"Only {items} in-game price(s) to compare the auction houses with, and at least {MIN_EVIDENCE} are "
                f"needed (so far: {listed}).")
    elif scores[best] < MIN_SCORE:
        note = (f"No auction house matches the in-game prices well ({listed}); they may be from another auction "
                "house than these.")
    elif runner is None:
        note = f"{houses[best].label} matches the in-game prices, but not surely enough ({_sure(sure)}; {listed})."
    else:
        note = (f"{houses[best].label} and {houses[runner].label} match the in-game prices about equally well "
                f"({sure:.0%} sure; {listed}).")
    return Match(best, clear, sure, items, scores, note, now)


# The addon's saved full scan ------------------------------------------------------------------


class LuaError(ValueError):
    pass


LUA_TOKEN = re.compile(r"""
    (?P<space>\s+|--[^\n]*)
    | (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
    | (?P<num>-?(?:0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?))
    | (?P<name>[A-Za-z_]\w*)
    | (?P<sym>[{}\[\]=,;])
""", re.VERBOSE | re.DOTALL)
LUA_ESCAPE = re.compile(r"\\(\d{1,3}|.)", re.DOTALL)
LUA_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "f": "\f", "v": "\v", "\n": "\n"}


def _lua_string(text: str) -> str:
    return LUA_ESCAPE.sub(lambda m: chr(int(m[1])) if m[1].isdigit() else LUA_ESCAPES.get(m[1], m[1]), text[1:-1])


def _lua_tokens(text: str) -> list[tuple[str, str]]:
    tokens, pos = [], 0
    while pos < len(text):
        m = LUA_TOKEN.match(text, pos)
        if not m:
            raise LuaError(f"unexpected {text[pos:pos + 10]!r} at {pos}")
        if m.lastgroup != "space":
            tokens.append((m.lastgroup, m[0]))
        pos = m.end()
    tokens.append(("end", ""))
    return tokens


def parse_saved_variables(text: str) -> dict[str, Any]:
    """A SavedVariables file (`Name = <Lua value>` lines, as the game writes them) as Python values: tables whose
    keys are 1..n become lists, other tables dicts."""
    tokens, i, found = _lua_tokens(text), 0, {}

    def value(i: int) -> tuple[Any, int]:
        kind, word = tokens[i]
        if kind == "str":
            return _lua_string(word), i + 1
        if kind == "num":
            return (int(word, 0) if re.fullmatch(r"-?(0[xX][0-9a-fA-F]+|\d+)", word) else float(word)), i + 1
        if kind == "name" and word in ("true", "false", "nil"):
            return {"true": True, "false": False, "nil": None}[word], i + 1
        if word == "{":
            return table(i + 1)
        raise LuaError(f"unexpected {word!r}")

    def expect(i: int, word: str) -> int:
        if tokens[i][1] != word:
            raise LuaError(f"expected {word!r}, got {tokens[i][1]!r}")
        return i + 1

    def table(i: int) -> tuple[Any, int]:
        out: dict[Any, Any] = {}
        n = 0
        while tokens[i][1] != "}":
            if tokens[i][0] == "end":
                raise LuaError("unfinished table")
            if tokens[i][1] == "[":
                key, i = value(i + 1)
                i = expect(expect(i, "]"), "=")
            elif tokens[i][0] == "name" and tokens[i + 1][1] == "=":
                key, i = tokens[i][1], i + 2
            else:
                n += 1
                key = n
            out[key], i = value(i)
            if tokens[i][1] in (",", ";"):
                i += 1
        if out and all(isinstance(k, int) for k in out) and sorted(out) == list(range(1, len(out) + 1)):
            return [out[k] for k in range(1, len(out) + 1)], i + 1
        return out, i + 1

    while tokens[i][0] != "end":
        if tokens[i][0] != "name":
            raise LuaError(f"expected a name, got {tokens[i][1]!r}")
        name = tokens[i][1]
        found[name], i = value(expect(i + 1, "="))
    return found


def saved_files(addons_dir: Path) -> list[Path]:
    """The addon's SavedVariables files of every account, newest first."""
    try:
        flavour = addons_dir.resolve().parents[1]  # <flavour>/Interface/AddOns
        files = list(flavour.glob(f"WTF/Account/*/SavedVariables/{ADDON}.lua"))
        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)
    except (OSError, IndexError):
        return []


def saved_scan(addons_dir: Path, realm: str | None = None) -> tuple[str, list[Seen]] | None:
    """The addon's latest full scan of a realm's auction house (the newest one of any realm if None) as last saved:
    every item as that scan saw it, an item a scan before saw as not listed then."""
    best: tuple[int, str, list[Seen]] | None = None
    for path in saved_files(addons_dir):
        try:
            saved = parse_saved_variables(path.read_text(encoding="utf-8", errors="replace")).get("WowMCPDB")
        except (OSError, LuaError, RecursionError) as e:
            log.warning("can't read %s: %s", path.name, e)
            continue
        realms = saved.get("auction") if isinstance(saved, dict) else None
        for name, data in (realms or {}).items() if isinstance(realms, dict) else ():
            if (realm and name != realm) or not isinstance(data, dict):
                continue
            scans, current = data.get("scans"), data.get("current")
            scan = scans[current - 1] if isinstance(scans, list) and isinstance(current, int) \
                and 0 < current <= len(scans) else None
            if not isinstance(scan, dict) or not isinstance(scan.get("at"), int):
                continue
            at, faction, seen = scan["at"], scan.get("faction"), []
            for item, entry in (data.get("scan") or {}).items() if isinstance(data.get("scan"), dict) else ():
                parts = entry.split(",") if isinstance(item, int) and isinstance(entry, str) else []
                if len(parts) != 4 or not all(p.isdigit() for p in parts[1:]):
                    continue
                price, listed, auctions, k = parts[0], int(parts[1]), int(parts[2]), int(parts[3])
                if k == current:
                    seen.append(Seen(item, at, int(price) if price.isdigit() else None, listed, auctions,
                                     "full scan", faction))
                else:  # gone by this scan
                    seen.append(Seen(item, at, None, 0, 0, "full scan", faction))
            if seen and (best is None or at > best[0]):
                best = (at, name, seen)
    return (best[1], best[2]) if best else None


# The online prices -----------------------------------------------------------------------------


def ruleset_for(realm: str | None, rulesets: list[dict]) -> int:
    """The ruleset of a realm's type, by its name (e.g. "... Realm 8 PvP"). Every ruleset lists the same houses with
    the same auctions, so this only matters if Blizzard ever splits them."""
    name = (realm or "").lower()
    if re.search(r"\b(rp|roleplay)[ -]?pvp\b", name):
        wanted = "roleplay pvp"
    elif re.search(r"\b(rp|roleplay)\b", name):
        wanted = "roleplay"
    elif re.search(r"\bpvp\b", name):
        wanted = "pvp"
    else:
        wanted = "normal"
    for ruleset in rulesets:
        if ruleset["name"].lower() == wanted:
            return ruleset["id"]
    if not rulesets:
        raise BattleNetError("Blizzard's API lists no rulesets for this region.")
    return rulesets[0]["id"]


def find_region(config: battlenet.Config, addons_dir: Path | None, store: Store | None = None) -> str:
    if config.region:
        return config.region
    if addons_dir:
        with contextlib.suppress(OSError, IndexError):
            region = (game.situation(addons_dir).installed.region or "").lower()
            if region in battlenet.REGIONS:
                return region
    stored = store.fact("region") if store else None
    if stored:
        return stored
    raise BattleNetError(f"The player's region isn't known: set {battlenet.REGION_KEY}=eu (or us, kr, tw) in "
                         f"{battlenet.shown(config.path or battlenet.config_path())}.")


def make_client(config: battlenet.Config, region: str) -> battlenet.Client:
    return battlenet.Client(config, region)


def _addons_dir() -> Path | None:
    raw = os.environ.get(ADDONS_DIR_ENV)
    return Path(raw).expanduser() if raw else None


@dataclass
class Fetch:
    house: int
    kind: str
    status: str  # "new", "unchanged" (a 304) or "fresh" (not due)
    snapshot: Snapshot | None
    seconds: float = 0.0
    downloaded: int = 0


@dataclass
class Choice:
    house: House | None
    how: str | None = None  # e.g. "matched to the in-game prices (99.95% sure)"
    match: Match | None = None
    note: str | None = None  # why there's none


class Online:
    """The online prices for one region: the store, plus a client made when the network is needed."""

    def __init__(self, store: Store, config: battlenet.Config, region: str, addons_dir: Path | None = None,
                 now: float | None = None):
        self.store, self.config, self.region, self.addons_dir = store, config, region, addons_dir
        self.now = int(time.time() if now is None else now)
        self._client: battlenet.Client | None = None

    @property
    def client(self) -> battlenet.Client:
        if self._client is None:
            self._client = make_client(self.config, self.region)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def rulesets(self) -> list[dict]:
        stored, seen = self.store.rulesets(self.region)
        if stored and seen is not None and self.now - seen < RULESETS_EVERY:
            return stored
        found = self.client.rulesets()
        self.store.set_rulesets(self.region, found, self.now)
        return found

    def ruleset(self, realm: str | None) -> int:
        return ruleset_for(realm, self.rulesets())

    def houses(self, realm: str | None = None) -> list[House]:
        stored, seen = self.store.houses(self.region)
        if stored and seen is not None and self.now - seen < HOUSES_EVERY:
            return stored
        found = self.client.auction_houses(self.ruleset(realm))
        self.store.set_houses(self.region, found, self.now)
        return [House(h["id"], h["name"]) for h in sorted(found, key=lambda h: h["id"])]

    @contextlib.contextmanager
    def _lock(self) -> Iterator[None]:
        """One refresh at a time across processes (MCP servers, the bridge), so a snapshot is fetched once."""
        with open(self.store.path.with_suffix(".lock"), "a") as lock:
            deadline = time.monotonic() + LOCK_WAIT
            while True:
                if locking.acquire(lock, blocking=False):
                    break
                if time.monotonic() > deadline:
                    raise BattleNetError("Another wow-mcp process is still fetching auction data.")
                time.sleep(0.2)
            try:
                yield
            finally:
                locking.release(lock)

    def due(self, house: int, kind: str) -> bool:
        checked = self.store.checked(self.region, house, kind)
        if checked is not None and self.now - checked < RECHECK:
            return False
        latest = self.store.latest(self.region, house, kind)
        return latest is None or self.now - latest.at >= MAX_AGE

    def refresh(self, houses: Iterable[int], realm: str | None = None, force: bool = False) -> list[Fetch]:
        """Fetch the houses' snapshots that may have changed (conditional GETs) and keep the new ones."""
        houses = list(dict.fromkeys(houses))
        if not houses:
            return []
        if not force and not any(self.due(h, k) for h in houses for k in KINDS):
            return [Fetch(h, k, "fresh", self.store.latest(self.region, h, k)) for h in houses for k in KINDS]
        ruleset = self.ruleset(realm)
        done = []
        with self._lock():
            for house in houses:
                for kind in KINDS:
                    latest = self.store.latest(self.region, house, kind)
                    if not force and not self.due(house, kind):  # done meanwhile by another process
                        done.append(Fetch(house, kind, "fresh", latest))
                        continue
                    started = time.monotonic()
                    self.store.mark_checked(self.region, house, kind, self.now)  # a failure waits RECHECK too
                    get = self.client.commodities if kind == "commodities" else self.client.auctions
                    response = get(ruleset, house, since=latest.at if latest else None)
                    if response is None:
                        done.append(Fetch(house, kind, "unchanged", latest, time.monotonic() - started))
                        continue
                    rows = summarize(response.data.get("auctions") if isinstance(response.data, dict) else None,
                                     kind == "commodities")
                    at = int(response.last_modified or self.now)
                    snapshot, new = self.store.add_snapshot(self.region, house, kind, ruleset, at, self.now, rows,
                                                            response.size)
                    done.append(Fetch(house, kind, "new" if new else "unchanged", snapshot,
                                      time.monotonic() - started, response.downloaded))
                    if new:
                        log.info("auction house %d: new %s snapshot from %s (%d auctions, %d items)", house, kind,
                                 utc(at), snapshot.auctions, snapshot.items)
        return done

    def candidates(self, realm: str | None, faction: str | None) -> list[House]:
        """The houses the player could use: their faction's and the neutral ones."""
        houses = self.houses(realm)
        if faction in ("Alliance", "Horde"):
            return [h for h in houses if h.name.startswith((faction, NEUTRAL))]
        return houses

    def match(self, realm: str, faction: str | None) -> Match:
        """Works out which house the in-game prices came from, and keeps the result for the realm."""
        if self.addons_dir:
            import_saved_scan(self.store, self.addons_dir, realm)
        evidence = self.store.evidence(realm, self.now - EVIDENCE_DAYS * DAY)
        faction = faction or next((s.faction for s in reversed(evidence) if s.faction), None)
        if len(evidence) < MIN_EVIDENCE:
            match = Match(None, False, None, len(evidence), {}, (
                f"Only {len(evidence)} in-game price(s) so far to compare the auction houses with, and at least "
                f"{MIN_EVIDENCE} are needed: get_auction_prices records the prices the game has (more after the "
                "player visits the auction house)."), self.now)
        else:
            candidates = self.candidates(realm, faction)
            self.refresh([h.id for h in candidates], realm)
            scored, gap = self.compare([h for h in candidates if self.has_listings(h.id)], evidence)
            match = judge(scored, {h.id: h for h in candidates}, self.now, gap)
        self.store.save_match(realm, self.region, match)
        return match

    def has_listings(self, house: int) -> bool:
        return any((s := self.store.latest(self.region, house, kind)) and s.items for kind in KINDS)

    def compare(self, houses: list[House], evidence: list[Seen]) -> tuple[dict[int, list[float]], int]:
        """Per house, how alike each in-game price is to the house's snapshot nearest in time to it; and the
        biggest gap in time between the two."""
        scored: dict[int, list[float]] = {h.id: [] for h in houses}
        loaded: dict[int, dict[int, Listing]] = {}
        groups: dict[int, list[Seen]] = defaultdict(list)
        for seen in evidence:
            groups[seen.at].append(seen)
        gap = 0
        for at, group in sorted(groups.items()):
            for house in houses:
                listed: dict[int, Listing] = {}
                for kind in KINDS:
                    snapshot = self.store.nearest(self.region, house.id, kind, at)
                    if snapshot:
                        if snapshot.id not in loaded:
                            loaded[snapshot.id] = self.store.listings(snapshot.id)
                        listed.update(loaded[snapshot.id])
                        gap = max(gap, abs(snapshot.at - at))
                scored[house.id] += [similarity(seen, listed.get(seen.item)) for seen in group]
        return scored, gap

    def choose(self, realm: str | None, faction: str | None) -> Choice:
        """The player's auction house: set in battlenet.env, or matched to the in-game prices once that's clear."""
        if self.config.auction_house:
            wanted = self.config.auction_house
            try:
                house = next((h for h in self.houses(realm) if h.id == wanted), None)
            except BattleNetError:
                house = None
            how = f"set in {battlenet.shown(self.config.path or battlenet.config_path())} ({battlenet.HOUSE_KEY})"
            return Choice(house or House(wanted, "Auction House"), how)
        if not realm:
            return Choice(None, note=(
                "Which auction house is the player's isn't known yet: it's worked out from the prices the game has, "
                "so ask get_auction_prices while the game runs."))
        match = self.store.match(realm)
        if not (match and match.clear and match.region == self.region):
            match = self.match(realm, faction)
        if not match.clear or match.house is None:
            hint = (f"set {battlenet.HOUSE_KEY}=<id> in {battlenet.shown(self.config.path or battlenet.config_path())} "
                    "if you know which it is (wow-mcp-auctions houses lists them)")
            return Choice(None, match=match, note=f"No online prices: which auction house is the player's isn't clear. "
                                                  f"{match.note} Or {hint}.")
        house = next((h for h in self.houses(realm) if h.id == match.house), House(match.house, "Auction House"))
        return Choice(house, f"matched to the in-game prices ({_sure(match.confidence)})", match)


def import_saved_scan(store: Store, addons_dir: Path, realm: str) -> int:
    """The addon's saved full scan of the realm as evidence, once per version of the file; how many items."""
    files = saved_files(addons_dir)
    stamp = ";".join(f"{p}:{p.stat().st_mtime_ns}" for p in files)
    if not files or store.fact(f"saved scan {realm}") == stamp:
        return 0
    found = saved_scan(addons_dir, realm)
    if found:
        store.record(realm, found[1])
    store.set_facts(**{f"saved scan {realm}": stamp})
    return len(found[1]) if found else 0


# For the tools --------------------------------------------------------------------------------


@dataclass
class Point:
    at: int
    span: str  # "snapshot", or "day": the median of that day's snapshots
    price: int | None  # the cheapest buyout per item then (median of the day's)
    quantity: int
    snapshots: int = 1


@dataclass
class Usual:
    price: int | None  # the median of the cheapest prices over USUAL_DAYS
    quantity: int  # the median of how many were listed
    snapshots: int
    since: int  # the oldest snapshot it's from


@dataclass
class ItemPrice:
    item: int
    at: int  # the snapshot it's from
    listing: Listing | None  # None: none listed then
    commodity: bool | None = None
    last: tuple[int, Listing] | None = None  # none listed now: when some last were
    usual: Usual | None = None
    history: list[Point] = field(default_factory=list)


@dataclass
class Prices:
    enabled: bool = False  # there are credentials
    house: House | None = None
    how: str | None = None
    at: int | None = None  # the newest snapshot
    since: int | None = None  # the oldest one (how far history goes)
    items: dict[int, ItemPrice] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _median(values: list[int]) -> int | None:
    return math.floor(statistics.median(values) + 0.5) if values else None


def history(series: list[tuple[int, Listing | None]], now: int) -> tuple[Usual | None, list[Point]]:
    """The usual price (median over USUAL_DAYS) and the history: every snapshot of the last 24 hours, and a point per
    day before that."""
    def prices(listings: list[Listing | None]) -> int | None:
        return _median([x.price for x in listings if x and x.price])

    def listed(listings: list[Listing | None]) -> int:
        return _median([x.quantity if x else 0 for x in listings]) or 0

    recent = [(at, x) for at, x in series if at >= now - USUAL_DAYS * DAY]
    usual = None
    if recent:
        listings = [x for _, x in recent]
        usual = Usual(prices(listings), listed(listings), len(recent), recent[0][0])
    points, days = [], defaultdict(list)
    for at, listing in series:
        if at >= now - DAY:
            points.append(Point(at, "snapshot", listing.price if listing else None, listing.quantity if listing else 0))
        else:
            days[at - at % DAY].append(listing)
    daily = [Point(day, "day", prices(ls), listed(ls), len(ls)) for day, ls in sorted(days.items())]
    return usual, daily + points


def _config() -> tuple[battlenet.Config | None, str | None]:
    try:
        config = battlenet.load_config()
    except BattleNetError as e:
        return None, f"Online prices are off: {e}"
    if not config.has_credentials:
        return None, f"Online prices are off: to have them, {config.setup_hint()}."
    return config, None


def lookup(items: Iterable[int], realm: str | None = None, faction: str | None = None, seen: Iterable[Seen] = (),
           with_history: bool = False, addons_dir: Path | None = None, now: float | None = None) -> Prices:
    """Online prices of these items on the player's auction house, fetching a newer snapshot first if one is due.
    `seen` are the prices the game just gave (the evidence for which house is the player's). Problems become notes:
    online prices never stop the in-game ones."""
    config, problem = _config()
    if not config:
        return Prices(notes=[problem] if problem else [])
    addons_dir = addons_dir or _addons_dir()
    seen = list(seen)
    with Store() as store:
        if realm:
            store.set_facts(realm=realm, faction=faction)
            store.record(realm, seen)
        realm, faction = realm or store.fact("realm"), faction or store.fact("faction")
        try:
            region = find_region(config, addons_dir, store)
        except BattleNetError as e:
            return Prices(True, notes=[f"Online prices are off: {e}"])
        store.set_facts(region=region)
        online = Online(store, config, region, addons_dir, now)
        result = Prices(True)
        try:
            choice = online.choose(realm, faction)
            if choice.house:
                online.refresh([choice.house.id], realm)
        except BattleNetError as e:
            # Whatever is stored still serves: a house chosen before, and its snapshots.
            choice = _stored_choice(store, config, region, realm)
            result.notes.append(f"Couldn't reach Blizzard's API for newer prices: {e}")
        finally:
            online.close()
        if not choice.house:
            if choice.note:
                result.notes.append(choice.note)
            return result
        result.house, result.how = choice.house, choice.how
        return _read(store, region, choice.house, list(dict.fromkeys(items)), with_history, online.now, result)


def _stored_choice(store: Store, config: battlenet.Config, region: str, realm: str | None) -> Choice:
    names = {h.id: h for h in store.houses(region)[0]}
    if config.auction_house:
        return Choice(names.get(config.auction_house) or House(config.auction_house, "Auction House"),
                      f"set in battlenet.env ({battlenet.HOUSE_KEY})")
    match = store.match(realm) if realm else None
    if match and match.clear and match.house and match.region == region:
        return Choice(names.get(match.house) or House(match.house, "Auction House"),
                      f"matched to the in-game prices ({_sure(match.confidence)})", match)
    return Choice(None)


def _read(store: Store, region: str, house: House, items: list[int], with_history: bool, now: int,
          result: Prices) -> Prices:
    latest = [s for kind in KINDS if (s := store.latest(region, house.id, kind))]
    if not latest:
        result.notes.append(f"No snapshot of {house.label} yet.")
        return result
    snapshots = store.snapshots(region, house.id)
    result.at, result.since = max(s.at for s in latest), snapshots[0].at
    found: dict[int, tuple[Snapshot, Listing]] = {}
    for snapshot in latest:
        for item, listing in store.listings(snapshot.id, items).items():
            found[item] = (snapshot, listing)
    for item in items:
        if item in found:
            snapshot, listing = found[item]
            price = ItemPrice(item, snapshot.at, listing, snapshot.kind == "commodities")
        else:
            price = ItemPrice(item, min(s.at for s in latest), None, last=store.last_listed(region, house.id, item))
        if with_history:
            price.usual, price.history = history(store.series(region, house.id, item, now - HISTORY_DAYS * DAY), now)
        result.items[item] = price
    return result


def warm_up(addons_dir: Path | None = None) -> None:
    """While the game is being asked: fetch a newer snapshot of the player's house if one is due. Quiet: lookup
    reports the problems."""
    try:
        lookup([], addons_dir=addons_dir)
    except Exception:  # noqa: BLE001 (never in the tool's way)
        log.debug("warming up the online prices failed", exc_info=True)


# The background refresh (wow-mcp-bridge) ------------------------------------------------------


def enabled() -> bool:
    config, _ = _config()
    return config is not None


def refresh_once(addons_dir: Path | None = None, now: float | None = None) -> float:
    """Fetch what's due: the player's house, or while it isn't known the houses it could be (so there are snapshots
    to compare with). Seconds until the next snapshot is due."""
    config, problem = _config()
    if not config:
        log.info("%s", problem)
        return REFRESH_INTERVAL
    with Store() as store:
        region = find_region(config, addons_dir, store)
        realm, faction = store.fact("realm"), store.fact("faction")
        online = Online(store, config, region, addons_dir, now)
        try:
            choice = online.choose(realm, faction) if realm or config.auction_house else Choice(None)
            if choice.house:
                houses = [choice.house.id]
            elif realm and faction:
                houses = [h.id for h in online.candidates(realm, faction)]
                log.info("online auction prices: %s", choice.note or "the player's house isn't known yet")
            else:
                log.info("online auction prices: waiting for get_auction_prices to say the player's realm")
                return REFRESH_INTERVAL
            online.refresh(houses, realm)
            due = [s.at + MAX_AGE + SETTLE for h in houses for k in KINDS if (s := store.latest(region, h, k))]
        finally:
            online.close()
    wait = min(due, default=online.now + REFRESH_INTERVAL) - online.now
    return float(min(REFRESH_INTERVAL, max(RECHECK, wait)))


def refresh_loop(addons_dir: Path | None, stop: threading.Event,
                 refresh: Callable[[Path | None], float] = refresh_once) -> None:
    """The bridge's background refresh: each new snapshot of the player's house soon after Blizzard makes it (about
    hourly), so price history builds up even when nobody asks. Runs until `stop` is set."""
    while not stop.is_set():
        wait = float(RECHECK)
        try:
            wait = refresh(addons_dir)
        except BattleNetError as e:
            log.warning("online auction prices: %s", e)
        except Exception:  # noqa: BLE001 (it mustn't stop the bridge)
            log.exception("online auction prices: refreshing failed")
        stop.wait(wait)


# wow-mcp-auctions -------------------------------------------------------------------------------


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _player(store: Store, addons_dir: Path | None) -> tuple[str | None, str | None]:
    realm, faction = store.fact("realm"), store.fact("faction")
    if not realm and addons_dir:
        found = saved_scan(addons_dir)
        if found:
            realm, faction = found[0], next((s.faction for s in found[1] if s.faction), None)
    return realm, faction


def _show_house(store: Store, region: str, house: House, now: int) -> list[str]:
    lines = []
    for kind in KINDS:
        s = store.latest(region, house.id, kind)
        if s:
            lines.append(f"  {kind}: {utc(s.at)} ({ago(s.at, now)}): {s.auctions:,} auctions, {s.items:,} items, "
                         f"{_size(s.bytes)} of JSON")
        else:
            lines.append(f"  {kind}: no snapshot yet")
    snapshots = store.snapshots(region, house.id)
    if snapshots:
        stamps = sorted({s.at for s in snapshots})
        lines.append(f"  history: {len(stamps)} snapshot time(s) from {utc(stamps[0])} to {utc(stamps[-1])} "
                     f"({(stamps[-1] - stamps[0]) / DAY:.1f} days)")
    return lines


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="wow-mcp-auctions",
        description="Online auction house prices from Blizzard's API: which house is the player's, its snapshots and "
        "price history. Credentials go in ~/.config/wow-mcp/battlenet.env.",
    )
    parser.add_argument("command", nargs="?", default="status", choices=("status", "refresh", "houses", "match"),
                        help="status (default): the house, its latest snapshots and history; refresh: fetch newer "
                        "snapshots; houses: the region's auction houses; match: work out the player's house again")
    parser.add_argument("--addons-dir", default=os.environ.get(ADDONS_DIR_ENV),
                        help=f"the game's Interface/AddOns folder, for the region and the saved full scan "
                        f"(default: ${ADDONS_DIR_ENV})")
    parser.add_argument("--house", type=int, action="append", metavar="ID",
                        help="refresh: this house instead of the player's (repeatable)")
    parser.add_argument("--force", action="store_true", help="refresh: ask even if the snapshots aren't due")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    addons_dir = Path(args.addons_dir).expanduser() if args.addons_dir else None
    try:
        sys.exit(_run(args, addons_dir))
    except BattleNetError as e:
        sys.exit(str(e))


def _run(args: argparse.Namespace, addons_dir: Path | None) -> int:
    config = battlenet.load_config()
    path = battlenet.shown(config.path or battlenet.config_path())
    print(f"Credentials: {'set' if config.has_credentials else 'missing'} ({path})")
    with Store() as store:
        region = find_region(config, addons_dir, store)
        realm, faction = _player(store, addons_dir)
        now = int(time.time())
        print(f"Region: {region}; realm: {realm or 'not known yet'}{f' ({faction})' if faction else ''}")
        online = Online(store, config, region, addons_dir, now)
        try:
            return {"status": _status, "refresh": _refresh, "houses": _houses, "match": _match}[args.command](
                args, store, online, realm, faction)
        finally:
            online.close()


def _status(args, store: Store, online: Online, realm, faction) -> int:
    rulesets, _ = store.rulesets(online.region)
    if rulesets:
        names = ", ".join(f"{r['id']} {r['name']}" for r in rulesets)
        print(f"Ruleset: {ruleset_for(realm, rulesets)} (of {names}; they all list the same houses)")
    choice = _stored_choice(store, online.config, online.region, realm)
    match = store.match(realm) if realm else None
    if choice.house:
        print(f"Auction house: {choice.house.label}, {choice.how}")
        for line in _show_house(store, online.region, choice.house, online.now):
            print(line)
    else:
        print("Auction house: not known yet" + (f". {match.note}" if match else ""))
    if match:
        print(f"Match ({utc(match.at)}): {match.note}")
    size = sum(p.stat().st_size for p in store.path.parent.glob(store.path.name + "*"))
    print(f"Cache: {battlenet.shown(store.path)}, {_size(size)}; {store.count_seen():,} in-game prices kept as "
          "evidence")
    return 0


def _refresh(args, store: Store, online: Online, realm, faction) -> int:
    online.config.require()
    if args.house:
        houses = args.house
    else:
        choice = online.choose(realm, faction)
        houses = [choice.house.id] if choice.house else [h.id for h in online.candidates(realm, faction)]
        if not choice.house:
            print(choice.note or "The player's house isn't known: fetching the houses it could be.")
    started = time.monotonic()
    for fetch in online.refresh(houses, realm, force=args.force):
        s = fetch.snapshot
        what = f"{fetch.house} {fetch.kind}"
        if fetch.status == "new" and s:
            print(f"{what}: new snapshot from {utc(s.at)}: {s.auctions:,} auctions, {s.items:,} items; "
                  f"{_size(s.bytes)} of JSON, {_size(fetch.downloaded)} downloaded, {fetch.seconds:.1f} s")
        elif s:
            state = "no newer snapshot yet (304)" if fetch.status == "unchanged" else "not due yet"
            took = f", {fetch.seconds:.1f} s" if fetch.seconds else ""
            print(f"{what}: {state}; the latest is from {utc(s.at)} ({ago(s.at, online.now)}){took}")
        else:
            print(f"{what}: nothing")
    print(f"Done in {time.monotonic() - started:.1f} s.")
    return 0


def _houses(args, store: Store, online: Online, realm, faction) -> int:
    online.config.require()
    rulesets = online.rulesets()
    print("Rulesets: " + ", ".join(f"{r['id']} {r['name']}" for r in rulesets) +
          " (they all list the same houses with the same auctions)")
    choice = _stored_choice(store, online.config, online.region, realm)
    for house in online.houses(realm):
        mark = f"  <- the player's, {choice.how}" if choice.house and choice.house.id == house.id else ""
        latest = [s for kind in KINDS if (s := store.latest(online.region, house.id, kind))]
        known = (f": {sum(s.auctions for s in latest):,} auctions as of {utc(max(s.at for s in latest))}"
                 if latest else "")
        print(f"{house.id}  {house.name}{known}{mark}")
    return 0


def _match(args, store: Store, online: Online, realm, faction) -> int:
    online.config.require()
    if not realm:
        print("The player's realm isn't known: call get_auction_prices once while the game runs, or pass "
              "--addons-dir so the saved full scan can be read.")
        return 1
    match = online.match(realm, faction)
    print(match.note)
    if match.clear and match.house is not None:
        print(f"The player's auction house: {match.house}.")
        return 0
    return 1
