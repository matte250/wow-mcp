"""The AI's long-term memory: every /ai exchange, kept for good, and its notes about each player.

The bridge appends each exchange it answers to conversations.jsonl, one JSON object per line, and
nothing ever rewrites it: /ai new only starts a fresh short-term conversation (bridge.History). Notes
are lasting facts the AI keeps about a player (notes.json); the bridge puts them in every prompt.
tools/memory.py lets agents search the exchanges and keep the notes, in the bridge or in any other
session. Both files are in ~/.local/share/wow-mcp/ ($XDG_DATA_HOME), apart from the game's folder.

Exchanges from before the archive are only in the bridge's logs: `wow-mcp-memory import-logs` adds
them (import_logs).
"""

import argparse
import contextlib
import json
import logging
import os
import re
from collections import deque
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from wow_mcp import locking
from wow_mcp.transport import write_atomic

log = logging.getLogger("wow_mcp.memory")

ARCHIVE_FILE = "conversations.jsonl"
NOTES_FILE = "notes.json"
LOGS_FOLDER = "bridge-logs"  # where import-logs looks by default
# Who's asking: the bridge sets it for its agent's tools; elsewhere they ask the game.
PLAYER_ENV = "WOW_MCP_PLAYER"
MAX_NOTES = 30  # per player; every prompt carries them all
MAX_NOTES_CHARS = 3000
MAX_NOTE = 300
LOG_SPACING = 60  # seconds between exchanges whose time a log doesn't say


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / "wow-mcp"


# The archive ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """An exchange in the archive. Its id is its line in the file, which never changes."""

    id: int
    at: float  # unix time it was answered
    player: str | None
    question: str
    answer: str
    model: str | None = None
    approx: bool = False  # `at` is a guess: imported from a log without times

    @property
    def key(self) -> tuple[str | None, str, str]:
        return self.player, self.question, self.answer


def record(player: str | None, question: str, answer: str, at: float, model: str | None = None,
           approx: bool = False) -> dict:
    """An exchange as the archive stores it."""
    saved = {"at": at, "player": player, "model": model, "question": question, "answer": answer}
    return {**saved, "approx": True} if approx else saved


def words(query: str | None) -> list[str]:
    """A search's words: an item token like {item:18731:Pattern: Heavy Leather Ball} gives its ID and name."""
    return re.findall(r"\w+", (query or "").lower())


def word(text: str) -> re.Pattern:
    """Where a search word matches: at the start of a word (flip: flips, flipping), an ID only whole."""
    return re.compile(rf"\b{re.escape(text)}" + (r"\b" if text.isdigit() else ""), re.IGNORECASE)


class Archive:
    """Every exchange, in conversations.jsonl; lines are only ever added."""

    def __init__(self, path: Path | None = None):
        self.path = path or data_dir() / ARCHIVE_FILE

    def add(self, player: str | None, question: str, answer: str, at: float, model: str | None = None) -> None:
        self.extend([record(player, question, answer, at, model)])

    def extend(self, records: Sequence[dict]) -> None:
        data = b"".join(json.dumps(r, ensure_ascii=False).encode("utf-8") + b"\n" for r in records)
        if not data:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a+b") as f:
            locking.acquire(f)  # the bridge and an import may add at the same time
            if f.seek(0, os.SEEK_END):
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":  # a write cut short: that line stays broken, the next ones whole
                    data = b"\n" + data
            f.write(data)

    def entries(self) -> list[Entry]:
        """Every exchange, in the file's order; lines that can't be read are left out."""
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return []
        found = []
        # Split on \n only: answers may hold other line breaks (U+2028) that JSON leaves as they are.
        for n, line in enumerate(raw.split(b"\n"), 1):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                found.append(Entry(n, float(r["at"]), r.get("player") or None, str(r["question"]), str(r["answer"]),
                                   r.get("model") or None, bool(r.get("approx"))))
            except (ValueError, TypeError, KeyError, AttributeError):
                log.warning("%s: line %d can't be read, skipping it", self.path, n)
        return found

    def search(self, query: str | None = None, since: float | None = None, until: float | None = None) -> list[Entry]:
        """The exchanges with every word of the query in their question or answer (any case), between
        the times given; newest first."""
        patterns = [word(w) for w in words(query)]
        return sorted(
            (e for e in self.entries()
             if (since is None or e.at >= since) and (until is None or e.at < until)
             and all(p.search(e.question) or p.search(e.answer) for p in patterns)),
            key=lambda e: (e.at, e.id), reverse=True,
        )


# Notes ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Note:
    id: int
    text: str
    at: float  # when it was saved


def _note(saved: dict) -> Note:
    return Note(int(saved["id"]), str(saved["text"]), float(saved["at"]))


class Notes:
    """What the AI knows about each player, in notes.json: per player, at most MAX_NOTES notes and
    MAX_NOTES_CHARS characters, oldest first. Ids are never reused."""

    def __init__(self, path: Path | None = None):
        self.path = path or data_dir() / NOTES_FILE

    def about(self, player: str) -> list[Note]:
        saved = self._load()
        try:
            return [_note(n) for n in saved.get(self._key(saved, player), {}).get("notes", [])]
        except (TypeError, KeyError, ValueError, AttributeError) as e:
            log.warning("can't read %s's notes in %s: %r", player, self.path, e)
            return []

    def add(self, player: str, text: str, at: float, replaces: int | None = None) -> tuple[Note, bool, list[Note]]:
        """Save a note (in place of note `replaces`); it counts as the newest. Returns it, whether
        it was there already, and the oldest notes cut to stay within the limits."""
        text = " ".join(text.split())[:MAX_NOTE]
        if not text:
            raise ValueError("the note is empty")
        with self._change(player) as mine:
            notes = mine["notes"]
            same = [n for n in notes if n["text"].casefold() == text.casefold()]
            if replaces is not None and not any(n["id"] == replaces for n in notes):
                raise LookupError(f"There's no note #{replaces}")
            old = replaces if replaces is not None else (same[0]["id"] if same else None)
            if old is None:
                old = mine["next"]
                mine["next"] += 1
            notes[:] = [n for n in notes if n["id"] != old and n not in same]
            notes.append({"id": old, "text": text, "at": at})
            cut = []
            while len(notes) > MAX_NOTES or sum(len(n["text"]) for n in notes) > MAX_NOTES_CHARS:
                cut.append(_note(notes.pop(0)))
        return Note(old, text, at), bool(same) and replaces is None, cut

    def forget(self, player: str, note_id: int) -> Note | None:
        """Remove a note; None if the player has no such note."""
        if not any(n.id == note_id for n in self.about(player)):
            return None
        with self._change(player) as mine:
            gone = [n for n in mine["notes"] if n["id"] == note_id]
            mine["notes"] = [n for n in mine["notes"] if n["id"] != note_id]
        return _note(gone[0]) if gone else None

    @staticmethod
    def _key(saved: dict, player: str) -> str:
        """The player's name as first saved: names are typed in any case."""
        player = player.strip()
        return next((key for key in saved if key.casefold() == player.casefold()), player)

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
                raise ValueError(f"can't read the notes in {self.path}: {e}") from e
            log.warning("can't read the notes in %s: %s", self.path, e)
            return {}

    @contextlib.contextmanager
    def _change(self, player: str) -> Iterator[dict]:
        """The player's notes ({next, notes}) to change; saved afterwards. One change at a time: the
        bridge's agent and other sessions may change them at once."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_name(self.path.name + ".lock"), "a+b") as lock:
            locking.acquire(lock)
            saved = self._load(changing=True)
            mine = saved.setdefault(self._key(saved, player), {})
            mine.setdefault("notes", [])
            mine.setdefault("next", max((n["id"] for n in mine["notes"]), default=0) + 1)
            yield mine
            write_atomic(self.path, json.dumps(saved, ensure_ascii=False, indent=1).encode("utf-8"))


# Old bridge logs -----------------------------------------------------------------------------

# A line the bridge logged: "INFO Slim asks: ...", since timestamps "2026-11-11 13:27:01,360 INFO ...".
LOG_LINE = re.compile(r"(?:(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)[,.]\d+ )?(DEBUG|INFO|WARNING|ERROR|CRITICAL) (.*)")
ASKS = re.compile(r"(the player|\S+) asks(?: \((\w+)\))?: (.*)")
FAILED = ("agent failed", "agent timed out")  # then the "answer" is an apology
STARTED = "Answering /ai questions"  # a bridge starting, in a log that more runs wrote to
# The bridge's hourly auction refresh says when each new snapshot is from (online_auctions.py), and
# logs it within minutes: in a log without times, what came before happened before about then.
SNAPSHOT = re.compile(r"auction house \d+: new \w+ snapshot from (\d{4}-\d\d-\d\d \d\d:\d\d) UTC\b.*")
# What a log saved by Claude Code's background tasks ends with: not part of the last answer.
TRAILER = re.compile(r"\n\n\[(?:killed|exited with code -?\d+)\]\n?\Z")


def read_log(text: str, last: float) -> list[dict]:
    """The exchanges in a bridge log, in order, as archive records.

    The bridge logs each question as it arrives and each answer when it's done, one at a time, so
    answers go with the questions in the order they were asked. An answer runs on over the lines
    after it until the next logged line.

    Without times on its lines (logs from before LOG_FORMAT had them), each exchange gets a guess,
    marked approx: the time of the next auction snapshot logged after it, else `last` (when the file
    was last written), with the exchanges before that a minute apart.
    """
    exchanges: list[dict] = []
    waiting: deque[dict] = deque()  # questions not answered yet
    answer: dict | None = None  # the one being read
    untimed: list[dict] = []  # answered since the last time the log gave
    for line in TRAILER.sub("\n", text).split("\n"):
        line = line.rstrip("\r")
        m = LOG_LINE.fullmatch(line)
        if not m:
            if answer is not None:
                answer["answer"] += "\n" + line
            continue
        answer = None
        stamp, level, message = m.groups()
        if level == "INFO" and message.startswith(STARTED):  # questions left from a run before go unanswered
            waiting.clear()
        elif level == "INFO" and (asked := ASKS.fullmatch(message)):
            player = None if asked[1] == "the player" else asked[1]
            waiting.append({"player": player, "model": asked[2], "question": asked[3]})
        elif level == "INFO" and message.startswith("answer: ") and waiting:
            question = waiting.popleft()
            if not question.pop("failed", False):
                at = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").timestamp() if stamp else None
                answer = {**question, "answer": message.removeprefix("answer: "), "at": at}
                exchanges.append(answer)
                if at is None:
                    untimed.append(answer)
        elif level == "INFO" and (snapshot := SNAPSHOT.fullmatch(message)) and untimed:
            _guess(untimed, datetime.strptime(snapshot[1], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())
        elif level == "ERROR" and message.startswith(FAILED) and waiting:
            waiting[0]["failed"] = True
    _guess(untimed, last)
    return [record(e["player"], e["question"], e["answer"].strip(), e["at"], e["model"], e.get("approx", False))
            for e in exchanges]


def _guess(untimed: list[dict], until: float) -> None:
    """Times for exchanges that happened before `until`: the last then, each one before a minute earlier."""
    for back, exchange in enumerate(reversed(untimed)):
        exchange["at"], exchange["approx"] = until - back * LOG_SPACING, True
    untimed.clear()


def import_logs(paths: Iterable[Path], archive: Archive) -> tuple[int, int]:
    """Add the exchanges in bridge logs to the archive, except those it has (the same player, question
    and answer), so importing again is safe. Files are read oldest first. Returns how many exchanges
    were added and how many it had."""
    known = {e.key for e in archive.entries()}
    added, had = [], 0
    for path in sorted(paths, key=lambda p: p.stat().st_mtime):
        for exchange in read_log(path.read_text(encoding="utf-8", errors="replace"), path.stat().st_mtime):
            key = (exchange["player"], exchange["question"], exchange["answer"])
            if key in known:
                had += 1
                continue
            known.add(key)
            added.append(exchange)
    archive.extend(added)
    return len(added), had


def log_files(paths: Sequence[str]) -> list[Path]:
    """The files named, with every file in the folders named; by default the data folder's bridge-logs."""
    found = []
    for path in [Path(p).expanduser() for p in paths] or [data_dir() / LOGS_FOLDER]:
        found += sorted(p for p in path.iterdir() if p.is_file()) if path.is_dir() else [path]
    return found


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="wow-mcp-memory",
        description="The /ai conversation archive and the AI's notes, in ~/.local/share/wow-mcp ($XDG_DATA_HOME).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    logs = commands.add_parser(
        "import-logs",
        help="add the exchanges in old wow-mcp-bridge logs to the archive; safe to repeat",
        description="Adds the /ai exchanges in wow-mcp-bridge logs to the archive, skipping those it already has. "
        "Exchanges in logs without times get estimates: from the auction snapshots logged after them, else the "
        "file's modification time.",
    )
    logs.add_argument("paths", nargs="*", help=f"log files, or folders of them (default: {LOGS_FOLDER} in the "
                      "data folder)")
    args = parser.parse_args(argv)
    archive = Archive()
    files = log_files(args.paths)
    if missing := [str(p) for p in files if not p.is_file()]:
        parser.error(f"not found: {', '.join(missing)}")
    added, had = import_logs(files, archive)
    print(f"Read {len(files)} log(s): added {added} exchange(s) to {archive.path}; {had} were there already.")


if __name__ == "__main__":
    main()
