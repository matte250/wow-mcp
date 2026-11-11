"""Background service for /ai in game (wow-mcp-bridge).

The addon posts each question the player asks (Chat.lua, through Stream.lua's file opens).
This watches for them, asks an agent that has the wow tools, and shows the answer in the
player's chat as a message (tools/messages.py), marked as a reply so the game shows it in its AI
chat tab. Questions are answered one at a time.

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
from collections.abc import Callable, Collection, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from wow_mcp import item_text, locking, memory, stream
from wow_mcp.install import ADDONS_DIR_ENV
from wow_mcp.transport import ADDON, write_atomic

log = logging.getLogger("wow_mcp.bridge")
LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"  # memory.read_log reads it back

AGENT_ENV = "WOW_MCP_BRIDGE_AGENT"
SENDER = "Claude"
TIMEOUT = 180.0  # seconds an agent may take per question
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
    pass


# (prompt, model, player, effort) -> answer; model, effort None: its own; player: who asks, for its tools;
# raises AgentError
Agent = Callable[[str, str | None, str | None, str | None], str]


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
                    default_model: str | None = None, default_effort: str | None = None) -> int:
    """Show text in game, through the messages feature (tools/messages.py)."""
    # Imported on first use: importing the tools package loads every feature.
    from wow_mcp.tools.messages import publish_message as publish

    return publish(text, style=style, sender=sender, reply=reply, default_model=default_model,
                   default_effort=default_effort)


# Agents -----------------------------------------------------------------------------------


def mcp_config(addons_dir: Path, player: str | None = None) -> dict:
    """An MCP config with this server, run by the bridge's own Python; it tells the memory tools who
    asks (`player`)."""
    env = {ADDONS_DIR_ENV: str(addons_dir), **({memory.PLAYER_ENV: player} if player else {})}
    server = {"type": "stdio", "command": sys.executable, "args": ["-m", "wow_mcp.server"], "env": env}
    return {"mcpServers": {"wow": server}}


def claude_command(model: str | None = None, effort: str | None = None) -> list[str]:
    """Headless Claude Code with only the wow tools and web lookups, apart from the user's own
    settings, MCP servers and sessions."""
    command = [
        "claude", "-p",
        "--output-format", "json",
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
    """The answer in an agent's output; a Claude Code `--output-format json` result is unwrapped."""
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
    """

    def __init__(self, command: list[str], addons_dir: Path, timeout: float = TIMEOUT):
        self.addons_dir = addons_dir
        self._asked = command  # with the placeholders, filled in for each player
        self.command, self.env = self._for(None)
        self.timeout = timeout
        # An empty working folder, so no project's settings or CLAUDE.md apply.
        self._folder = tempfile.TemporaryDirectory(prefix="wow-mcp-bridge-")
        self._process: subprocess.Popen | None = None

    def _for(self, player: str | None) -> tuple[list[str], dict[str, str]]:
        """The command and its environment for a question from this player."""
        config = json.dumps(mcp_config(self.addons_dir, player))
        command = [arg.replace("{system_prompt}", SYSTEM_PROMPT).replace("{mcp_config}", config) for arg in self._asked]
        env = {**os.environ, ADDONS_DIR_ENV: str(self.addons_dir),
               "WOW_MCP_SYSTEM_PROMPT": SYSTEM_PROMPT, "WOW_MCP_MCP_CONFIG": config}
        env.pop(memory.PLAYER_ENV, None)
        return command, {**env, memory.PLAYER_ENV: player} if player else env

    def __call__(self, prompt: str, model: str | None = None, player: str | None = None,
                 effort: str | None = None) -> str:
        command, env = self._for(player) if player else (self.command, self.env)
        if model is not None:
            command = with_model(command, model)
        if effort is not None:
            command = with_effort(command, effort)
        try:
            # Its own process group, so a timeout also stops whatever it started (MCP servers).
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
            out, err = process.communicate(prompt, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            self.cancel()
            raise AgentTimeout(f"it took over {self.timeout:.0f} seconds") from None
        finally:
            self._process = None
        return agent_answer(out, err, process.returncode)

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
                 models: bool = False, model: str | None = None, effort: str | None = None):
        self.out_dir = addons_dir / ADDON / "out"
        self.agent = agent
        self.publish = publish
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
            self.say("Your question arrived garbled. Please ask again.")
            return
        item = Question.from_post(post.value) or Reset.from_post(post.value)
        if isinstance(item, Reset):
            log.info("%s starts a fresh conversation", item.player or "the player")
        elif item:
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
                self.say("I'm still busy with your earlier questions. Please ask again in a minute.")

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
        try:
            # The conversation is the same whichever model answers, however hard it thinks.
            prompt = question.prompt(self.history.recent(question.player), self.history.clock(),
                                     self.notes.about(question.player) if question.player else ())
            answer = self.agent(prompt, model, question.player, effort)
            text = answer or "I have no answer to that."
        except AgentTimeout as e:
            log.error("agent timed out: %s", e)
            text = f"Sorry, I gave up: {e}. Try asking something narrower."
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
        log.info("answer: %s", text)
        # One message (the game prints it one chat line per line), so an answer costs one slot.
        self.say("\n".join(chat_lines(text)), self.signed(model, effort))

    def signed(self, model: str | None, effort: str | None = None) -> str:
        """Who an answer is from: e.g. "Claude Sonnet" when the sonnet model answered it, "Claude Sonnet · high"
        when it also thought at the high level (Claude Code's default level isn't named)."""
        if not self.models:
            return self.sender
        name = MODELS.get(model or self.model or "")
        level = effort or (self.effort if self.effort in EFFORTS else None)
        signed = f"{self.sender} {name}" if name else self.sender
        return f"{signed} · {level}" if level else signed

    def say(self, text: str, sender: str | None = None) -> None:
        try:
            # A reply: the game shows it in the conversation, in its AI chat tab. It also tells the
            # game's picker what answers, and how hard it thinks, when the player picks none.
            default = self.model if self.models and self.model in MODELS else ""
            effort = self.effort if self.models and self.effort in EFFORTS else ""
            self.publish(text, style="chat", sender=sender or self.sender, reply=True, default_model=default,
                         default_effort=effort)
        except Exception:
            log.exception("couldn't show this in game: %s", text)


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
    parser.add_argument("--timeout", type=float, default=TIMEOUT,
                        help="seconds an answer may take (default: %(default)s)")
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
    addons_dir = Path(args.addons_dir).expanduser()
    if not (addons_dir / ADDON / "out").is_dir():
        sys.exit(f"{ADDON} isn't installed in {addons_dir}. Run wow-mcp-install first.")
    lock = claim(addons_dir / ADDON)  # noqa: F841 (held until exit)
    os.environ[ADDONS_DIR_ENV] = str(addons_dir)  # where publish_message sends answers
    asked = split_command(args.agent) if args.agent else claude_command(args.model, args.effort)
    command = resolve_command(asked)
    if not command:
        sys.exit(f"Agent command not found: {args.agent or asked[0]}")
    agent = CommandAgent(command, addons_dir, args.timeout)
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
                    effort=args.effort)
    signal.signal(signal.SIGTERM, lambda *_: bridge.stop.set())
    if args.agent:
        log.info("Answering /ai questions from %s with %s", addons_dir, command[0])
    else:
        log.info("Answering /ai questions from %s with Claude Code: %s, or the model the player picks (%s); thinking: "
                 "%s, or the level the player picks (%s)", addons_dir, args.model or "its default model",
                 ", ".join(MODELS), args.effort or "its default level", ", ".join(EFFORTS))
    try:
        bridge.serve()
    except KeyboardInterrupt:
        pass
    except stream.StreamError as e:
        sys.exit(str(e))
    finally:
        bridge.stop.set()
        agent.cancel()
