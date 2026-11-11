"""Background service for /ai in game (wow-mcp-bridge).

The addon posts each question the player asks (Chat.lua, through Stream.lua's file opens).
This watches for them, asks an agent that has the wow tools, and shows the answer in the
player's chat as a message (tools/messages.py), marked as a reply so the game shows it in its AI
chat tab. Questions are answered one at a time.

An answer may take as long as it needs while the agent visibly works. The game hears that each
question arrived (an ack, right away) and, now and then, what the agent is doing (Progress), for
the AI tab's status line; it can tell from them that the bridge is up. Claude Code streams its
events (stream-json), and every one of them is a sign of life: it's stopped only after --timeout
seconds without any, or when it reaches --max-time, a guard against runaways. Other agents
(--agent) print only their answer, so --max-time is all they have.

Claude Code's events also say how many tokens it writes (Tokens), so each note to the game carries
the count so far and the pace, which the game runs on between notes, and the answer the final count.

Each agent starts from zero, so the player's recent exchanges go into the prompt, which makes
follow-up questions work. They're kept in a file (History), so a restart keeps the conversation;
/ai new starts a fresh one. Every exchange also goes into the archive for good, which the agent
searches for older ones, and its notes about the player come with every prompt (memory.py).

The default agent is headless Claude Code (`claude -p`) with this MCP server and web lookups,
run apart from the user's own Claude Code settings. Any other command works too (--agent): it
gets the prompt on stdin and prints the answer.

The player picks which Claude model answers in game (Chat.lua), and each question says which.
Only the models in MODELS reach Claude Code's command line (--model); without a pick, the
bridge's own --model answers. Answers are signed with the model, e.g. "Claude Sonnet".
How hard it thinks is picked the same way: only the levels in EFFORTS reach --effort; without a
pick, the bridge's own --effort, else Claude Code's default. A level shows in the signature too,
e.g. "Claude Sonnet · high". Claude Code (2.1.322, checked live) takes every level with every
model in MODELS, and ignores a level it doesn't know with a warning, so no pick fails an answer.

Being long-lived, it also fetches each new online snapshot of the player's auction house (about
hourly) when Blizzard API credentials are set up, so price history builds up even when nobody asks
(online_auctions.py; --no-auction-refresh turns it off).
"""

import argparse
import contextlib
import json
import logging
import os
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from collections import Counter
from collections.abc import Callable, Collection, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from wow_mcp import item_text, locking, memory, stream
from wow_mcp.install import ADDONS_DIR_ENV
from wow_mcp.transport import ADDON, write_atomic

log = logging.getLogger("wow_mcp.bridge")
LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"  # memory.read_log reads it back

AGENT_ENV = "WOW_MCP_BRIDGE_AGENT"
SENDER = "Claude"
# Seconds Claude Code may go without any sign of life before it's stopped (--timeout). Measured live (2.1.322, Fable at
# max and xhigh): thinking streams an event about every 1.3 s (at most 3.3 s apart), so the silences are tool calls,
# about 5 s for a web search or fetch. The slowest tools wait on the network for up to a minute (an auction snapshot).
SILENCE = 120.0
MAX_TIME = 30 * 60.0  # seconds any answer may take, however busy (--max-time): only a guard against runaways
PROGRESS_EVERY = 40.0  # seconds at least between progress updates to the game: each costs a live-update slot
SETTLE = 5.0  # seconds a step must last before the game is told about it, so short ones don't spend slots
TICK = 1.0  # seconds between looks at a running agent
CHARS_PER_TOKEN = 4  # text and tool calls still streaming are counted by their length, at about this many a token
RATE_WINDOW = 10.0  # seconds of writing the pace sent to the game is measured over
# Seconds without a new token after which the agent isn't writing (a tool runs), so its pace is 0. Measured live
# (2.1.322): thinking streams an estimate at least every 3.3 s.
PAUSE = 5.0
MAX_WAITING = 5  # questions queued behind the one being answered
MAX_QUESTION = 500
LINE_WIDTH = 250  # characters per chat message
MAX_LINES = 6  # chat lines per answer
HISTORY = 8  # earlier exchanges in each prompt
HISTORY_MINUTES = 60  # ... from at most this long ago
MAX_KEPT_ANSWER = 1000  # characters of an answer kept for later prompts
HISTORY_FILE = "bridge-history.json"  # in the addon's folder, next to server-state.json
# The models the player can pick in game (MODELS in Chat.lua), by Claude Code's alias for each, with
# the name answers are signed with. Nothing else from the game reaches the command line.
MODELS = {"fable": "Fable", "opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku"}
# How hard it thinks, as the player can pick it in game (EFFORTS in Chat.lua): Claude Code's --effort levels.
EFFORTS = ("low", "medium", "high", "xhigh", "max")

SYSTEM_PROMPT = """\
The player typed this question into World of Warcraft's chat (with /ai, or in its AI chat tab) \
while playing. Your final reply is shown to them in the game's chat, so:
- Reply in plain text, in at most three short sentences (about 400 characters) unless they ask \
for more. No Markdown, tables, headings or web links: the chat window shows them literally.
- When the answer is naturally a table or a list of things with values (comparing items or gear, \
prices and profits, drop rates, materials), show it with visualize, in a window of its own, and \
keep your reply to the conclusion and "Details in the window."
- Write each item you name as {item:<id>}, or {item:<id>:<name>}, with its item ID for their game \
version, e.g. {item:2318:Light Leather}: the chat shows it as an item link with its icon, which \
they can click. Their bags and bank (get_inventory), recipes, vendors, quest rewards and \
get_item_details give item IDs. Items you have no ID for stay plain text.
- Use the wow tools to look at their game (location, character, quests, bags, ...) when the \
answer depends on it, instead of asking.
- When the answer involves places, mark them with show_on_map (nearest_first for "closest" \
questions) and say they're marked on the map. Use the other tools that act in game too when \
that's what they ask for.
- Look things up on the web (e.g. Wowhead) when you're not sure, for their game version.
- Never suggest buying something to resell, or listing it on the auction house, before \
get_item_details (binds, can_be_auctioned) confirms it can be auctioned in their game.
- Don't call show_message: your reply is shown in game for you.
- Your earlier exchanges with them may come before the question, as context. Use them only when \
the question follows up on them (e.g. "and the next one?"); otherwise ignore them. The game may \
have changed since, so look again when that matters.
- Those are only the last few. search_conversations finds anything from your earlier conversations, \
also on earlier days: when they refer to one ("remember…", "like we said", "yesterday") or seem to \
continue a project, search it before answering. Never say you can't remember or see an earlier \
conversation without having searched.
- What you know about them (your notes) may come first. Save lasting facts and decisions with \
remember, and update or forget notes that are wrong.
- Don't ask them questions back: make a sensible assumption and say what you assumed. They can \
correct you with another /ai."""


class AgentError(Exception):
    """The agent couldn't answer; the message is shown to the player."""


class AgentTimeout(AgentError):
    """It was stopped at the time limit (--max-time); the message says so: "after the 30-minute limit"."""


class AgentSilent(AgentTimeout):
    """It was stopped for showing no sign of life (--timeout): "no response from the model for 2 minutes"."""


# (prompt, model, player, effort, progress) -> answer; model, effort None: its own; player: who asks, for its tools;
# progress: what it does meanwhile, for the game (Progress); raises AgentError
Agent = Callable[[str, str | None, str | None, str | None, "Progress"], str]


def _player(post: dict) -> str | None:
    player = post.get("player")
    return player if isinstance(player, str) and player else None


def _picked(post: dict, key: str, allowed: Collection[str], what: str) -> str | None:
    """What the player picked in game, if it's one of `allowed`; anything else is left out."""
    value = post.get(key)
    if value is None or (isinstance(value, str) and value in allowed):
        return value
    log.warning("ignoring the %s picked in game, %.40r: not one of %s", what, value, ", ".join(allowed))
    return None


def _model(post: dict) -> str | None:
    return _picked(post, "model", MODELS, "model")


def _effort(post: dict) -> str | None:
    return _picked(post, "effort", EFFORTS, "thinking level")


@dataclass(frozen=True)
class Exchange:
    question: str
    answer: str
    at: float  # unix time it was answered


def _ago(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes} min ago" if minutes > 0 else "just now"


@dataclass(frozen=True)
class Question:
    text: str
    player: str | None = None
    model: str | None = None  # one of MODELS, picked in game; None: the bridge's own
    effort: str | None = None  # one of EFFORTS, likewise
    post: tuple[int, int] | None = None  # the post it came in, (session, id): the game knows its replies by it

    @classmethod
    def from_post(cls, value: object) -> "Question | None":
        """The question in a post from Chat.lua ({question, player, model, effort}), if it is one."""
        if not isinstance(value, dict) or not isinstance(value.get("question"), str):
            return None
        text = " ".join(value["question"].split())[:MAX_QUESTION]
        return cls(text, _player(value), _model(value), _effort(value)) if text else None

    def prompt(self, earlier: Sequence[Exchange] = (), now: float | None = None,
               notes: Sequence[memory.Note] = ()) -> str:
        """The agent's prompt: the question, after the agent's notes about the player and their earlier
        exchanges (oldest first)."""
        parts = []
        if notes:
            lines = [(f"What you know about {self.player or 'the player'} (your notes from earlier conversations, "
                      "oldest first):")]
            lines += [f"- #{n.id} ({time.strftime('%Y-%m-%d', time.localtime(n.at))}): {n.text}" for n in notes]
            parts.append("\n".join(lines))
        if earlier:
            now = time.time() if now is None else now
            lines = [(f"Earlier exchanges with {self.player or 'the player'}, oldest first. Context only: use them if "
                      "the question below follows up on them, otherwise ignore them.")]
            for exchange in earlier:
                lines += [f"Q ({_ago(now - exchange.at)}): {exchange.question}", f"A: {exchange.answer}"]
            parts.append("\n".join(lines))
        return "\n\n".join([*parts, f"{self.player or 'The player'} asks, in game: {self.text}"])


@dataclass(frozen=True)
class Reset:
    """/ai new: the player starts a fresh conversation."""

    player: str | None = None

    @classmethod
    def from_post(cls, value: object) -> "Reset | None":
        """The reset in a post from Chat.lua ({reset = true, player}), if it is one."""
        if not isinstance(value, dict) or value.get("reset") is not True:
            return None
        return cls(_player(value))


class History:
    """Each player's recent exchanges, for their next prompts. Kept in a file (if `path`), so a
    restart keeps the conversation; only what a prompt can still use is kept."""

    def __init__(self, path: Path | None, size: int = HISTORY, minutes: float = HISTORY_MINUTES,
                 clock: Callable[[], float] = time.time):
        self.path = path
        self.size = size
        self.window = minutes * 60
        self.clock = clock
        self._players: dict[str, list[Exchange]] = {}  # by player name ("" if unknown)
        self._lock = threading.Lock()
        if path and path.exists():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                self._players = {
                    str(player): [Exchange(str(e["question"]), str(e["answer"]), float(e["at"])) for e in exchanges]
                    for player, exchanges in saved.items()
                }
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as e:
                log.warning("starting without earlier exchanges: can't read %s: %s", path, e)

    def recent(self, player: str | None) -> list[Exchange]:
        """The player's exchanges for their next prompt, oldest first."""
        with self._lock:
            return self._recent(player or "")

    def _recent(self, key: str) -> list[Exchange]:
        if self.size <= 0:
            return []
        since = self.clock() - self.window
        return [e for e in self._players.get(key, []) if e.at >= since][-self.size :]

    def add(self, player: str | None, question: str, answer: str) -> None:
        if self.size <= 0 or self.window <= 0:
            return
        answer = " ".join(answer.split())
        if len(answer) > MAX_KEPT_ANSWER:
            answer = answer[: MAX_KEPT_ANSWER - 1].rstrip() + "…"
        with self._lock:
            key = player or ""
            self._players[key] = [*self._recent(key), Exchange(question, answer, self.clock())][-self.size :]
            self._save()

    def clear(self, player: str | None) -> None:
        with self._lock:
            if self._players.pop(player or "", None) is not None:
                self._save()

    def _save(self) -> None:
        self._players = {key: recent for key in self._players if (recent := self._recent(key))}
        if not self.path:
            return
        saved = {key: [asdict(e) for e in exchanges] for key, exchanges in self._players.items()}
        try:
            write_atomic(self.path, json.dumps(saved, ensure_ascii=False, indent=1).encode("utf-8"))
        except OSError as e:
            log.warning("couldn't save the conversation: %s", e)


# Answer formatting ------------------------------------------------------------------------


def plain_text(text: str) -> str:
    """Markdown the agent used anyway, as plain text the chat window can show."""
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("```") or ("-" in line and set(line) <= set("|:- ")):  # code fences, rules
            continue
        if line.startswith("|"):  # a table row
            line = ", ".join(cell.strip() for cell in line.strip("|").split("|") if cell.strip())
        line = re.sub(r"^#{1,6}\s+", "", line)
        line = re.sub(r"^[*+]\s+", "- ", line)
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"\[([^\]]+)\]\([^)\s]*\)", r"\1", text)  # links: just the text
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)
    text = re.sub(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])", r"\1", text)
    text = re.sub(r"`+([^`]+)`+", r"\1", text)
    return re.sub("[\U00010000-\U0010ffff\U0000fe0f\U0000200d]", "", text)  # emoji: the game's fonts lack them


def _pack(pieces: list[str], width: int) -> list[str]:
    lines: list[str] = []
    for piece in pieces:
        if lines and len(lines[-1]) + 1 + len(piece) <= width:
            lines[-1] += " " + piece
        else:
            lines.append(piece)
    return lines


# While an answer is split into lines, each item ({item:...}) stands in as one word of the same length,
# so no split falls inside one.
ITEM_START, ITEM_REST = "\ue000", "\ue001"


def _hide_items(text: str) -> tuple[str, list[str]]:
    items = item_text.ITEM.findall(text)
    return item_text.ITEM.sub(lambda m: ITEM_START + ITEM_REST * (len(m[0]) - 1), text), items


def _show_items(lines: list[str], items: list[str]) -> list[str]:
    found = iter(items)

    def back(match: re.Match) -> str:
        item = next(found)
        return item if len(match[0]) == len(item) else ""  # cut short at the end: left out

    return [re.sub(f"{ITEM_START}{ITEM_REST}*", back, line) for line in lines]


def chat_lines(text: str, width: int = LINE_WIDTH, limit: int = MAX_LINES) -> list[str]:
    """An answer as at most `limit` chat messages of at most `width` characters: plain text, a
    message per paragraph or list item, long ones split between sentences, and cut short with
    "…" if it's still too long. Items ({item:...}) are never split; one longer than a line gets
    a line of its own."""
    text, items = _hide_items(plain_text(text))
    whole = any(len(item) > width for item in items)
    lines = []
    for paragraph in text.splitlines():
        sentences = re.split(r"(?<=[.!?])\s+", " ".join(paragraph.split()))
        pieces = [piece for s in sentences if s for piece in textwrap.wrap(s, width, break_long_words=not whole)]
        lines += _pack(pieces, width)
    if len(lines) > limit:
        lines = _pack(lines, width)
    if len(lines) > limit:
        lines = lines[:limit]
        last = lines[-1]
        if len(last) > width - 2:
            last = last[: width - 2].rsplit(" ", 1)[0]
        lines[-1] = last + " …"
    return _show_items(lines, items)


def publish_message(text: str, style: str = "chat", sender: str = SENDER, reply: bool = False,
                    default_model: str | None = None, default_effort: str | None = None,
                    question: tuple[int, int] | None = None, took: float | None = None,
                    tokens: int | None = None) -> int:
    """Show text in game, through the messages feature (tools/messages.py)."""
    # Imported on first use: importing the tools package loads every feature.
    from wow_mcp.tools.messages import publish_message as publish

    return publish(text, style=style, sender=sender, reply=reply, default_model=default_model,
                   default_effort=default_effort, question=question, took=took, tokens=tokens)


def publish_status(status: str, text: str = "", question: tuple[int, int] | None = None,
                   quiet: float | None = None, tokens: int | None = None, rate: float | None = None) -> int:
    """Tell the game how a question is coming along, for the AI tab's status line (tools/messages.py)."""
    from wow_mcp.tools.messages import publish_status as publish

    return publish(status, text, question=question, quiet=quiet, tokens=tokens, rate=rate)


def _span(seconds: float, before_noun: bool = False) -> str:
    """A time as said: "2 minutes", "90 seconds"; before a noun, "30-minute"."""
    count, unit = (seconds / 60, "minute") if seconds >= 60 and seconds % 60 == 0 else (seconds, "second")
    return f"{count:g}-{unit}" if before_noun else f"{count:g} {unit}{'' if count == 1 else 's'}"


def _clock(seconds: float) -> str:
    """A time taken, as a clock shows it: "4:07"."""
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


# Progress ---------------------------------------------------------------------------------

THINKING, WRITING = "working it out", "writing"  # under "Thinking…" in game, so not "thinking" again
WOW_TOOLS = "mcp__wow__"  # how Claude Code names the wow tools
# What the game's status line says the agent is doing while it calls each tool. The rest of the wow tools read the
# game (get_..., list_...) or change what it shows.
STEPS = {
    "get_auction_prices": "checking auction prices",
    "watch_auction_prices": "checking auction prices",
    "find_upgrades": "looking for upgrades",
    "get_item_details": "looking up items",
    "get_inventory": "looking in your bags",
    "get_player_location": "checking where you are",
    "get_character_stats": "looking at your character",
    "get_quest_log": "reading your quest log",
    "get_quest_status": "checking your quests",
    "get_professions": "checking your professions",
    "get_known_recipes": "checking your recipes",
    "get_vendor_items": "checking vendors",
    "get_flight_paths": "checking flight paths",
    "get_reputations": "checking reputations",
    "search_conversations": "remembering earlier conversations",
    "find_npc": "looking up NPCs",
    "find_object": "looking up objects",
    "find_quest": "looking up quests",
    "show_on_map": "marking the map",
    "show_route": "marking a route",
    "highlight_areas": "marking areas",
    "show_plan": "making a plan",
    "visualize": "making a table or list",
    "WebSearch": "searching the web",
    "WebFetch": "reading a web page",
}


def tool_step(name: str) -> str:
    """What the status line says while the agent calls a tool, by Claude Code's name for it."""
    short = name.removeprefix(WOW_TOOLS)
    if short in STEPS:
        return STEPS[short]
    if name.startswith(WOW_TOOLS):
        return "looking at your game" if short.startswith(("get_", "list_", "search_")) else "updating your game"
    return "using its tools"


def event_step(event: object) -> tuple[str | None, str | None]:
    """What one of Claude Code's stream-json events says the agent does now: (step, tool), each None if it doesn't.
    Content starting says it (--include-partial-messages): thinking, a tool call, text."""
    if not isinstance(event, dict) or event.get("type") != "stream_event":
        return None, None
    inner = event.get("event")
    starts = isinstance(inner, dict) and inner.get("type") == "content_block_start"
    block = inner.get("content_block") if starts else None
    kind = block.get("type") if isinstance(block, dict) else None
    if kind in ("tool_use", "server_tool_use", "mcp_tool_use"):
        name = str(block.get("name") or "")
        return tool_step(name), name.removeprefix(WOW_TOOLS) or None
    if kind in ("thinking", "redacted_thinking"):
        return THINKING, None
    if kind == "text":
        return WRITING, None
    return None, None


def _output_tokens(usage: object) -> int | None:
    count = usage.get("output_tokens") if isinstance(usage, dict) else None
    return count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None


DELTA_TEXT = {"thinking_delta": "thinking", "text_delta": "text", "input_json_delta": "partial_json"}  # by delta type


def _delta_tokens(delta: object) -> float:
    """About how many tokens a piece of a streaming message is (a content_block_delta's delta)."""
    if not isinstance(delta, dict):
        return 0.0
    # Thinking usually streams without its text, with an estimate instead.
    estimate = delta.get("estimated_tokens") if delta.get("type") == "thinking_delta" else None
    if isinstance(estimate, (int, float)) and not isinstance(estimate, bool):
        return max(0.0, float(estimate))
    text = delta.get(DELTA_TEXT.get(str(delta.get("type")), ""))
    return len(text) / CHARS_PER_TOKEN if isinstance(text, str) else 0.0


class Tokens:
    """How many tokens the agent has written so far, for the game's status line: everything it outputs (thinking, text
    and tool calls) over all its turns, from Claude Code's stream-json events.

    A finished message's count is exact (its message_delta's usage), and the run's total once the result is in (its
    usage). A message still streaming is estimated: its thinking by the estimates that stream with it, its text and
    tool calls by their length. Claude Code says each thinking estimate twice (the thinking_delta's estimated_tokens,
    then a system thinking_tokens event); only the first counts.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.started = clock()
        self.final: int | None = None  # the result's count
        self._messages: dict[str, list] = {}  # by message id: [estimate, exact count once it's in]
        self._current = ""  # the message streaming, for events that don't say theirs
        # (when, tokens estimated by then), over the last RATE_WINDOW: the pace, which exact counts coming in don't move.
        self._written: list[tuple[float, float]] = [(self.started, 0.0)]

    def see(self, event: object) -> None:
        """One of Claude Code's stream-json events; most say nothing about tokens."""
        if not isinstance(event, dict):
            return
        if event.get("type") == "result":
            self.final = _output_tokens(event.get("usage"))
            return
        inner = event.get("event") if event.get("type") == "stream_event" else None
        if not isinstance(inner, dict):
            return
        kind, named = inner.get("type"), event.get("api_message_id")
        if kind == "message_start":
            message = inner.get("message")
            named = message.get("id") if isinstance(message, dict) and message.get("id") else named
            self._current = str(named or f"#{len(self._messages)}")
            self._messages.setdefault(self._current, [0.0, None])
            return
        message = self._messages.setdefault(str(named or self._current), [0.0, None])
        if kind == "message_delta" and (count := _output_tokens(inner.get("usage"))) is not None:
            message[1] = count
        elif kind == "content_block_delta" and (tokens := _delta_tokens(inner.get("delta"))):
            message[0] += tokens
            now = self.clock()
            self._written.append((now, self._written[-1][1] + tokens))
            while len(self._written) > 2 and self._written[1][0] <= now - RATE_WINDOW:
                self._written.pop(0)

    @property
    def total(self) -> int:
        if self.final is not None:
            return self.final
        return sum(exact if exact is not None else round(estimate) for estimate, exact in self._messages.values())

    def rate(self) -> float:
        """Tokens a second it has written lately (over RATE_WINDOW); 0 once it has written none for PAUSE seconds."""
        now = self.clock()
        last, written = self._written[-1]
        if now - last > PAUSE:
            return 0.0
        before = next((w for t, w in reversed(self._written) if t <= now - RATE_WINDOW), self._written[0][1])
        span = min(RATE_WINDOW, now - self.started)
        return (written - before) / span if span > 0 else 0.0


class Progress:
    """Tells the game how an answer is coming along, for the AI tab's status line: the step the agent is on (e.g.
    "checking auction prices"), as the agent says (CommandAgent, from Claude Code's events).

    Each update costs one of the game's live-update slots (300 per UI session), so a step goes out only once it has
    lasted SETTLE seconds, and at most every `every` seconds. While the agent keeps busy, it goes out again unchanged
    after `quiet` seconds, so the game can tell a long think from a bridge that's gone. `send(step)` publishes one;
    with `tokens` (the agent streams Claude Code's events), `send(step, tokens=..., rate=...)`, the count so far and
    the pace, which the game runs on until the next one.
    """

    def __init__(self, send: Callable[..., object], quiet: float = SILENCE, every: float = PROGRESS_EVERY,
                 clock: Callable[[], float] = time.monotonic, tokens: Tokens | None = None):
        self.send = send
        self.quiet = quiet
        self.every = every
        self.clock = clock
        self.started = self.sent = clock()  # when the game last heard about it: the ack, or the answer before
        self.alive = self.started  # the agent's last sign of life
        self.step: str | None = None  # what it's doing, since `since`
        self.since = self.started
        self.shown: str | None = None  # what the game was told
        self.tools: Counter[str] = Counter()  # the tools it called, in order
        self.tokens = tokens  # what it has written so far

    def read(self, event: object) -> None:
        """One of Claude Code's stream-json events (None for other output): a sign of life, which may say what the agent
        does now and what it has written."""
        if self.tokens is not None:
            self.tokens.see(event)
        self.saw(*event_step(event))

    def saw(self, step: str | None = None, tool: str | None = None) -> None:
        """A sign of life from the agent; step: what it's doing now, if it says; tool: one it calls."""
        now = self.clock()
        self.alive = now
        if step and step != self.step:
            self.step, self.since = step, now
        if tool:
            self.tools[tool] += 1
        self.tick()

    def tick(self) -> None:
        """Sends the step if that's due. The agent calls this about every second while it runs."""
        now = self.clock()
        if now - self.sent < self.every:
            return
        changed = self.step not in (None, self.shown) and now - self.since >= SETTLE
        busy = self.alive > self.sent and now - self.sent >= self.quiet
        if changed or busy:
            self.sent, self.shown = now, self.step
            if self.tokens is None:
                self.send(self.step or "")
            else:
                self.send(self.step or "", tokens=self.tokens.total, rate=round(self.tokens.rate(), 1))

    def used(self) -> str:
        """The tools it called, for the log: "get_auction_prices ×3, WebSearch"."""
        return ", ".join(f"{tool} ×{n}" if n > 1 else tool for tool, n in self.tools.items()) or "none"


# Agents -----------------------------------------------------------------------------------


def mcp_config(addons_dir: Path, player: str | None = None) -> dict:
    """An MCP config with this server, run by the bridge's own Python; it tells the memory tools who
    asks (`player`)."""
    env = {ADDONS_DIR_ENV: str(addons_dir), **({memory.PLAYER_ENV: player} if player else {})}
    server = {"type": "stdio", "command": sys.executable, "args": ["-m", "wow_mcp.server"], "env": env}
    return {"mcpServers": {"wow": server}}


def claude_command(model: str | None = None, effort: str | None = None) -> list[str]:
    """Headless Claude Code with only the wow tools and web lookups, apart from the user's own
    settings, MCP servers and sessions. It streams its events as they happen, down to each piece of
    thinking (stream-json with partial messages), so the bridge can watch it work."""
    command = [
        "claude", "-p",
        "--output-format", "stream-json", "--verbose", "--include-partial-messages",
        "--mcp-config", "{mcp_config}", "--strict-mcp-config",
        "--setting-sources", "project",  # the agent runs in an empty folder, so: none
        "--tools", "WebSearch,WebFetch",
        "--allowedTools", "mcp__wow,WebSearch,WebFetch",
        "--disallowedTools", "mcp__wow__show_message",  # the bridge shows the reply itself
        "--permission-mode", "dontAsk",
        "--no-session-persistence",
        "--append-system-prompt", "{system_prompt}",
    ]
    if model:
        command += ["--model", model]
    if effort:
        command += ["--effort", effort]
    return command


WINDOWS = sys.platform == "win32"
# What npm's launcher script for a native program (claude.cmd) runs: "%dp0%\...\claude.exe" %*
NPM_SHIM = re.compile(r'"%dp0%\\([^"\r\n]+\.exe)"\s+%\*', re.IGNORECASE)
LAUNCHERS = (".cmd", ".bat")  # run by cmd.exe


def split_command(text: str, windows: bool = WINDOWS) -> list[str]:
    """An agent command line (--agent) as its words."""
    if not windows:
        return shlex.split(text)
    # Windows paths have backslashes, which POSIX rules would eat. Without those rules the
    # quotes around a word stay on, so they come off here: "C:\Program Files\x\agent.exe".
    words = shlex.split(text, posix=False)
    return [word[1:-1] if len(word) >= 2 and word[0] == word[-1] and word[0] in "\"'" else word for word in words]


def resolve_command(command: list[str]) -> list[str] | None:
    """The command if its program is on PATH, or None if it isn't there.

    On Windows the program is replaced by where it was found. It's often installed as a .cmd
    launcher there (npm's claude.cmd), and arguments with line breaks or quotes, like the system
    prompt, don't survive cmd.exe. So a launcher that only starts a program runs as that program
    instead (see launcher_problem for the ones that don't).
    """
    program = shutil.which(command[0]) if command else None
    if not program:
        return None
    if not WINDOWS:
        return command
    if program.lower().endswith(LAUNCHERS):
        with contextlib.suppress(OSError):
            if m := NPM_SHIM.search(Path(program).read_text(encoding="utf-8", errors="replace")):
                target = Path(program).parent / m.group(1)
                if target.is_file():
                    program = str(target)
    return [program, *command[1:]]


def launcher_problem(command: list[str], windows: bool = WINDOWS) -> str | None:
    """Why a command, as it will be run, can't be: its program is still a .cmd or .bat launcher
    and an argument has a line break or a double quote, which cmd.exe would cut off or garble."""
    if not windows or not command or not command[0].lower().endswith(LAUNCHERS):
        return None
    if not any(c in arg for arg in command[1:] for c in '\r\n"'):
        return None
    return (f"{command[0]} is a batch launcher, and Windows can't pass it arguments with line breaks or double "
            "quotes (the system prompt, the MCP config). Name the program it starts as the agent instead (--agent), "
            "or leave {system_prompt} and {mcp_config} out of the command: the agent also gets them in its "
            "environment, as WOW_MCP_SYSTEM_PROMPT and WOW_MCP_MCP_CONFIG.")


def _option(command: list[str], option: str) -> str | None:
    """The value an option has in a command, if it's there."""
    i = command.index(option) if option in command else -1
    return command[i + 1] if 0 <= i < len(command) - 1 else None


def _with_option(command: list[str], option: str, value: str) -> list[str]:
    if option in command:
        i = command.index(option)
        command = command[:i] + command[i + 2 :]
    return command + [option, value]


def with_model(command: list[str], model: str) -> list[str]:
    """Claude Code's command with --model set to one of MODELS, in place of its own."""
    if model not in MODELS:  # never anything else from the game on the command line
        raise ValueError(f"not a model the player can pick: {model!r}")
    return _with_option(command, "--model", model)


def with_effort(command: list[str], effort: str) -> list[str]:
    """Claude Code's command with --effort set to one of EFFORTS, in place of its own."""
    if effort not in EFFORTS:  # likewise
        raise ValueError(f"not a thinking level the player can pick: {effort!r}")
    return _with_option(command, "--effort", effort)


def agent_answer(out: str, err: str, returncode: int) -> str:
    """The answer in an agent's output; a Claude Code result (`--output-format json`, or the last event of
    stream-json) is unwrapped."""
    text = out.strip()
    with contextlib.suppress(ValueError):
        result = json.loads(text)
        if isinstance(result, dict) and result.get("type") == "result":
            if result.get("is_error") or returncode:
                raise AgentError(str(result.get("result") or result.get("subtype") or "the agent failed"))
            return str(result.get("result") or "")
    if returncode:
        detail = (err.strip() or text).splitlines()
        raise AgentError(f"the agent exited with status {returncode}" + (f": {detail[-1][:200]}" if detail else ""))
    return text


class CommandAgent:
    """Runs an agent command per question: the prompt on stdin, the answer on stdout.

    In the command, {system_prompt} and {mcp_config} (JSON) are filled in; both are also in its
    environment, as WOW_MCP_SYSTEM_PROMPT and WOW_MCP_MCP_CONFIG. A model (one of MODELS) is
    given to Claude Code's command as --model, a thinking level (one of EFFORTS) as --effort. Who
    asks goes to the wow tools, in the MCP config and the environment (memory.PLAYER_ENV), so
    their memory is that player's.

    A command that streams Claude Code's events (`--output-format stream-json`) is watched as it
    runs: each event is a sign of life, and tells `progress` the step it's on and what it has
    written. It's stopped after `silence` seconds without any. Any command is stopped after `max_time`.
    """

    def __init__(self, command: list[str], addons_dir: Path, silence: float = SILENCE, max_time: float = MAX_TIME):
        self.addons_dir = addons_dir
        self._asked = command  # with the placeholders, filled in for each player
        self.command, self.env = self._for(None)
        self.silence = silence
        self.max_time = max_time
        self.streams = _option(command, "--output-format") == "stream-json"
        # An empty working folder, so no project's settings or CLAUDE.md apply.
        self._folder = tempfile.TemporaryDirectory(prefix="wow-mcp-bridge-")
        self._process: subprocess.Popen | None = None

    @property
    def quiet(self) -> float:
        """The longest it goes without telling `progress` anything while all is well: an agent that doesn't
        stream says nothing until it's done."""
        return self.silence if self.streams else self.max_time

    def _for(self, player: str | None) -> tuple[list[str], dict[str, str]]:
        """The command and its environment for a question from this player."""
        config = json.dumps(mcp_config(self.addons_dir, player))
        command = [arg.replace("{system_prompt}", SYSTEM_PROMPT).replace("{mcp_config}", config) for arg in self._asked]
        env = {**os.environ, ADDONS_DIR_ENV: str(self.addons_dir),
               "WOW_MCP_SYSTEM_PROMPT": SYSTEM_PROMPT, "WOW_MCP_MCP_CONFIG": config}
        env.pop(memory.PLAYER_ENV, None)
        return command, {**env, memory.PLAYER_ENV: player} if player else env

    def __call__(self, prompt: str, model: str | None = None, player: str | None = None,
                 effort: str | None = None, progress: Progress | None = None) -> str:
        command, env = self._for(player) if player else (self.command, self.env)
        if model is not None:
            command = with_model(command, model)
        if effort is not None:
            command = with_effort(command, effort)
        try:
            # Its own process group, so stopping it also stops whatever it started (MCP servers).
            # On Windows its output is read as UTF-8, not in the system's code page.
            platform = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP, "encoding": "utf-8", "errors": "replace"}
                        if WINDOWS else {"start_new_session": True})
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=env, cwd=self._folder.name, **platform,
            )
        except OSError as e:
            raise AgentError(f"couldn't start {self.command[0]}: {e.strerror}") from e
        self._process = process
        try:
            if self.streams:
                out, err = self._watch(process, prompt, progress or Progress(lambda step: None))
            else:
                try:
                    out, err = process.communicate(prompt, timeout=self.max_time)
                except subprocess.TimeoutExpired:
                    self.cancel()
                    raise AgentTimeout(f"after the {_span(self.max_time, before_noun=True)} limit") from None
        finally:
            self._process = None
        return agent_answer(out, err, process.returncode)

    def _watch(self, process: subprocess.Popen, prompt: str, progress: Progress) -> tuple[str, str]:
        """Runs a streaming agent to its end: its result event (or "") and what it said on stderr. Any output is a
        sign of life, and the events say what it's doing (progress). Stopped after `silence` seconds without any
        output, and at `max_time`."""
        output: queue.Queue[tuple[str, str | None]] = queue.Queue()

        def read(pipe, name: str) -> None:
            with contextlib.suppress(OSError, ValueError):  # closed under it when it's stopped
                for line in pipe:
                    output.put((name, line))
            output.put((name, None))

        def write() -> None:
            with contextlib.suppress(OSError, ValueError):
                process.stdin.write(prompt)
                process.stdin.close()

        for target, args in ((write, ()), (read, (process.stdout, "out")), (read, (process.stderr, "err"))):
            threading.Thread(target=target, args=args, name="wow-mcp-bridge-agent-io", daemon=True).start()
        started = alive = time.monotonic()
        result, errors, reading = "", [], 2
        while reading:
            with contextlib.suppress(queue.Empty):
                name, line = output.get(timeout=TICK)
                alive = time.monotonic()
                if line is None:
                    reading -= 1
                elif name == "err":
                    errors.append(line)
                    progress.saw()
                else:
                    event = None
                    with contextlib.suppress(ValueError):
                        event = json.loads(line)
                    if isinstance(event, dict) and event.get("type") == "result":
                        result = line
                    progress.read(event)
            now = time.monotonic()
            if now - alive > self.silence:
                self.cancel()
                raise AgentSilent(f"no response from the model for {_span(self.silence)}")
            if now - started > self.max_time:
                self.cancel()
                raise AgentTimeout(f"after the {_span(self.max_time, before_noun=True)} limit")
            progress.tick()
        process.wait()
        return result, "".join(errors)

    def cancel(self) -> None:
        """Stop the agent that's running, if any, and everything it started."""
        process = self._process
        if process is None:
            return
        if WINDOWS:
            # The process and everything under it; Windows has no polite way to ask a tree to stop.
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True, check=False)
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(5)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)  # anything still left in its group
        process.wait()
        for pipe in (process.stdin, process.stdout, process.stderr):
            with contextlib.suppress(OSError):
                pipe.close()


# The service ------------------------------------------------------------------------------


class Bridge:
    """Receives questions from the game and answers them one at a time."""

    def __init__(self, addons_dir: Path, agent: Agent, publish: Callable[..., int] = publish_message,
                 sender: str = SENDER, history: History | None = None,
                 background: Sequence[Callable[[threading.Event], None]] = (),
                 models: bool = False, model: str | None = None, effort: str | None = None,
                 notify: Callable[..., int] = publish_status, quiet: float = SILENCE,
                 clock: Callable[[], float] = time.monotonic, tokens: bool = False):
        self.out_dir = addons_dir / ADDON / "out"
        self.agent = agent
        self.publish = publish
        self.notify = notify  # tells the game how a question is coming along (publish_status)
        # The longest the agent goes without a sign of life while all is well (CommandAgent.quiet): the game is
        # told, and progress is sent at least that often.
        self.quiet = quiet
        self.tokens = tokens  # the agent streams Claude Code's events, so the game hears how many tokens it writes
        self.clock = clock  # for timing answers
        self.sender = sender
        self.models = models  # the agent is Claude Code, so the player can pick its model and thinking level
        self.model = model  # the one it answers with otherwise (--model), if known
        self.effort = effort  # how hard it thinks otherwise (--effort); None: Claude Code's default
        self.history = History(addons_dir / ADDON / HISTORY_FILE) if history is None else history
        # Long-term memory, apart from the game's folder: every exchange, and the agent's notes.
        self.archive = memory.Archive()
        self.notes = memory.Notes()
        self.background = background  # jobs that run until `stop` (the online auction refresh)
        # Resets wait their turn too: a question asked before /ai new belongs to the old conversation.
        self.questions: queue.Queue[Question | Reset] = queue.Queue(MAX_WAITING)
        self.ready = threading.Event()  # watching for questions
        self.stop = threading.Event()

    def serve(self) -> None:
        """Receive and answer questions until `stop` is set."""
        threading.Thread(target=self._work, name="wow-mcp-bridge-agent", daemon=True).start()
        for job in self.background:
            threading.Thread(target=job, args=(self.stop,), name="wow-mcp-bridge-background", daemon=True).start()
        with stream.Receiver(self.out_dir) as receiver:
            self.ready.set()
            while not self.stop.is_set():
                try:
                    posts = receiver.receive_posts(0.5)
                except stream.Corrupt as e:  # lost file opens; whatever was in flight is gone
                    log.warning("%s", e)
                    receiver.symbols.clear()
                    continue
                for post in posts:
                    self.accept(post)

    def accept(self, post: stream.Post) -> None:
        if post.error:
            log.warning("%s", post.error)
            self.say("Your question arrived garbled. Please ask again.", question=post.key)
            return
        item = Question.from_post(post.value) or Reset.from_post(post.value)
        if isinstance(item, Reset):
            log.info("%s starts a fresh conversation", item.player or "the player")
        elif item:
            item = replace(item, post=post.key)
            # memory.read_log reads this line and the answer's back (import-logs): keep their wording.
            log.info("%s asks%s: %s", item.player or "the player", f" ({item.model})" if item.model else "", item.text)
            if item.effort:  # a line of its own, so the one above keeps its wording
                log.info("thinking picked in game: %s", item.effort)
        else:
            log.warning("ignoring a post that isn't a question: %r", post.value)
            return
        try:
            self.questions.put_nowait(item)
        except queue.Full:
            if isinstance(item, Reset):  # the game already said it's done
                self.history.clear(item.player)
            else:
                self.say("I'm still busy with your earlier questions. Please ask again in a minute.",
                         question=item.post)
            return
        if isinstance(item, Question):  # right away, so the game knows the bridge is up
            self.tell(item, "ack", tokens=0 if self.tokens else None)

    def _work(self) -> None:
        while not self.stop.is_set():
            try:
                item = self.questions.get(timeout=0.5)
            except queue.Empty:
                continue
            if isinstance(item, Reset):
                self.history.clear(item.player)
            else:
                self.answer(item)

    def answer(self, question: Question) -> None:
        model, effort = question.model, question.effort
        if model and not self.models:
            log.info("ignoring the model picked in game (%s): --agent answers with its own", model)
            model = None
        if effort and not self.models:
            log.info("ignoring the thinking level picked in game (%s): --agent thinks its own way", effort)
            effort = None
        answer = ""  # stays empty if the agent fails: failures don't become part of the conversation
        progress = Progress(lambda step, **counts: self.tell(question, "progress", step, **counts), self.quiet,
                            clock=self.clock, tokens=Tokens(self.clock) if self.tokens else None)
        try:
            # The conversation is the same whichever model answers, however hard it thinks.
            prompt = question.prompt(self.history.recent(question.player), self.history.clock(),
                                     self.notes.about(question.player) if question.player else ())
            answer = self.agent(prompt, model, question.player, effort, progress)
            text = answer or "I have no answer to that."
        except AgentSilent as e:  # memory.FAILED knows these log lines: what follows isn't an answer
            log.error("agent timed out: %s", e)
            text = f"Sorry, I stopped: {e}."
        except AgentTimeout as e:
            log.error("agent timed out: %s", e)
            text = f"Sorry, I stopped {e}. Try asking something narrower."
        except AgentError as e:
            log.error("agent failed: %s", e)
            text = f"Sorry, I couldn't answer that: {e}"
        except Exception as e:  # a broken agent mustn't stop the bridge
            log.exception("agent failed")
            text = f"Sorry, I couldn't answer that: {e}"
        if self.stop.is_set():
            return
        if answer:
            self.history.add(question.player, question.text, answer)
            try:
                self.archive.add(question.player, question.text, answer.strip(), self.history.clock(),
                                 (model or self.model) if self.models else None)
            except OSError as e:
                log.warning("couldn't archive this exchange: %s", e)
        took = self.clock() - progress.started
        written = progress.tokens.total if progress.tokens else None  # the result's exact count, if it came
        log.info("took %s; tools used: %s%s", _clock(took), progress.used(),
                 f"; tokens written: {written}" if written is not None else "")
        log.info("answer: %s", text)
        # One message (the game prints it one chat line per line), so an answer costs one slot.
        self.say("\n".join(chat_lines(text)), self.signed(model, effort), question.post, took, written)

    def signed(self, model: str | None, effort: str | None = None) -> str:
        """Who an answer is from: e.g. "Claude Sonnet" when the sonnet model answered it, "Claude Sonnet · high"
        when it also thought at the high level (Claude Code's default level isn't named)."""
        if not self.models:
            return self.sender
        name = MODELS.get(model or self.model or "")
        level = effort or (self.effort if self.effort in EFFORTS else None)
        signed = f"{self.sender} {name}" if name else self.sender
        return f"{signed} · {level}" if level else signed

    def say(self, text: str, sender: str | None = None, question: tuple[int, int] | None = None,
            took: float | None = None, tokens: int | None = None) -> None:
        """A reply, to the question that came in post `question` if it's known; an answer says how long it took and,
        if they were counted, how many tokens the agent wrote."""
        try:
            # A reply: the game shows it in the conversation, in its AI chat tab. It also tells the
            # game's picker what answers, and how hard it thinks, when the player picks none.
            default = self.model if self.models and self.model in MODELS else ""
            effort = self.effort if self.models and self.effort in EFFORTS else ""
            extra = {key: value for key, value in (("took", took), ("tokens", tokens)) if value is not None}
            self.publish(text, style="chat", sender=sender or self.sender, reply=True, default_model=default,
                         default_effort=effort, question=question, **extra)
        except Exception:
            log.exception("couldn't show this in game: %s", text)

    def tell(self, question: Question, status: str, text: str = "", tokens: int | None = None,
             rate: float = 0.0) -> None:
        """A note to the game about a question it waits for, for its status line (never shown as a message):
        "ack" when it arrives, "progress" with the step the agent is on. Each says how long the bridge may then
        stay quiet while all is well and, if they're counted, the tokens written so far and how fast (the ack's: 0)."""
        counts = {} if tokens is None else {"tokens": tokens, "rate": rate}
        try:
            self.notify(status, text, question=question.post, quiet=self.quiet, **counts)
        except Exception:
            log.exception("couldn't tell the game how its question is coming along (%s %s)", status, text)


def claim(addon_dir: Path):
    """Make this the only bridge answering for this game; two would each answer every question.
    Returns the lock, which is held as long as the returned file stays open."""
    lock = open(addon_dir / ".bridge.lock", "a+")
    if not locking.acquire(lock, blocking=False):
        lock.seek(0)
        pid = lock.read().strip()
        lock.close()
        raise SystemExit(f"Another wow-mcp-bridge is already answering for this game{f' (pid {pid})' if pid else ''}.")
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()))
    lock.flush()
    return lock


def auction_refresh(addons_dir: Path, off: bool = False) -> list[Callable[[threading.Event], None]]:
    """The hourly online auction refresh (online_auctions.py) as a background job, when there are credentials."""
    from wow_mcp import online_auctions

    if off:
        log.info("Online auction prices: not refreshed in the background (--no-auction-refresh)")
        return []
    if not online_auctions.enabled():
        log.info("Online auction prices: off, no Blizzard API credentials (~/.config/wow-mcp/battlenet.env)")
        return []
    log.info("Online auction prices: refreshed about every hour")
    return [lambda stop: online_auctions.refresh_loop(addons_dir, stop)]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Answers the questions asked in World of Warcraft with /ai, using an agent with the wow tools."
    )
    parser.add_argument("--addons-dir", default=os.environ.get(ADDONS_DIR_ENV),
                        help=f"the game's Interface/AddOns folder (default: ${ADDONS_DIR_ENV})")
    parser.add_argument("--agent", default=os.environ.get(AGENT_ENV),
                        help="agent command instead of Claude Code; it gets the prompt on stdin and prints the answer, "
                        "and {system_prompt} and {mcp_config} (JSON) in it are filled in "
                        f"(default: ${AGENT_ENV})")
    parser.add_argument("--model", help="model for the Claude Code agent when the player hasn't picked one in game, "
                        "e.g. sonnet")
    parser.add_argument("--effort", choices=EFFORTS,
                        help="how hard the Claude Code agent thinks when the player hasn't picked a level in game "
                        "(default: Claude Code's own)")
    parser.add_argument("--timeout", type=float, default=SILENCE, metavar="SECONDS",
                        help="stop an answer when Claude Code has shown no sign of life (thinking, tool calls, "
                        "writing) for this long; while it works, it may take as long as it needs "
                        "(default: %(default)s)")
    parser.add_argument("--max-time", type=float, default=MAX_TIME, metavar="SECONDS",
                        help="stop any answer that takes longer than this, however busy; the only limit for --agent "
                        "(default: %(default)s)")
    parser.add_argument("--history", type=int, default=HISTORY, metavar="N",
                        help="earlier exchanges with the player to include in each prompt, so follow-up questions "
                        "work; 0 for none (default: %(default)s)")
    parser.add_argument("--history-minutes", type=float, default=HISTORY_MINUTES, metavar="MINUTES",
                        help="only include exchanges from the last this many minutes (default: %(default)s)")
    parser.add_argument("--no-auction-refresh", action="store_true",
                        help="don't fetch online auction prices every hour (on when ~/.config/wow-mcp/battlenet.env "
                        "has Blizzard API credentials), which builds up their price history")
    args = parser.parse_args()
    if not args.addons_dir:
        parser.error(f"pass --addons-dir or set {ADDONS_DIR_ENV}")
    if args.agent and args.model:
        parser.error("--model only applies to the default agent")
    if args.agent and args.effort:
        parser.error("--effort only applies to the default agent")
    if args.history < 0 or args.history_minutes < 0:
        parser.error("--history and --history-minutes can't be negative")
    if args.timeout <= 0 or args.max_time <= 0:
        parser.error("--timeout and --max-time must be more than 0")
    addons_dir = Path(args.addons_dir).expanduser()
    if not (addons_dir / ADDON / "out").is_dir():
        sys.exit(f"{ADDON} isn't installed in {addons_dir}. Run wow-mcp-install first.")
    lock = claim(addons_dir / ADDON)  # noqa: F841 (held until exit)
    os.environ[ADDONS_DIR_ENV] = str(addons_dir)  # where publish_message sends answers
    asked = split_command(args.agent) if args.agent else claude_command(args.model, args.effort)
    command = resolve_command(asked)
    if not command:
        sys.exit(f"Agent command not found: {args.agent or asked[0]}")
    agent = CommandAgent(command, addons_dir, args.timeout, args.max_time)
    if problem := launcher_problem(agent.command):
        sys.exit(problem)
    from wow_mcp.tools import messages

    if not hasattr(messages, "publish_message"):
        sys.exit("Can't show answers in game: tools/messages.py has no publish_message.")

    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    logging.getLogger("httpx2").setLevel(logging.WARNING)  # a line per request otherwise
    history = History(addons_dir / ADDON / HISTORY_FILE, args.history, args.history_minutes)
    background = auction_refresh(addons_dir, args.no_auction_refresh)
    bridge = Bridge(addons_dir, agent, history=history, background=background, models=not args.agent, model=args.model,
                    effort=args.effort, quiet=agent.quiet, tokens=agent.streams)
    signal.signal(signal.SIGTERM, lambda *_: bridge.stop.set())
    if args.agent:
        log.info("Answering /ai questions from %s with %s", addons_dir, command[0])
    else:
        log.info("Answering /ai questions from %s with Claude Code: %s, or the model the player picks (%s); thinking: "
                 "%s, or the level the player picks (%s)", addons_dir, args.model or "its default model",
                 ", ".join(MODELS), args.effort or "its default level", ", ".join(EFFORTS))
    if agent.streams:
        log.info("An answer is stopped after %s without a sign of life, and at the %s limit", _span(args.timeout),
                 _span(args.max_time, before_noun=True))
    else:
        log.info("An answer is stopped at the %s limit (the agent's output isn't watched as it runs)",
                 _span(args.max_time, before_noun=True))
    try:
        bridge.serve()
    except KeyboardInterrupt:
        pass
    except stream.StreamError as e:
        sys.exit(str(e))
    finally:
        bridge.stop.set()
        agent.cancel()
