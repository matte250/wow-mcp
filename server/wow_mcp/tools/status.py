"""Addon build, live channel and slots left (request kind STATUS, Status.lua)."""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from wow_mcp import game, install, kinds
from wow_mcp.app import addons_dir, as_list, guidance, request, tool, transport
from wow_mcp.install import ADDONS_DIR_ENV
from wow_mcp.transport import ADDON, SLOT_COUNT, slot_name

LOW_SLOTS = 20
BUILD_LINE = re.compile(r'ns\.BUILD\s*=\s*"([^"]*)"')

guidance(
    "If a WoW tool fails or behaves oddly (markers or notes don't show up, the game doesn't answer, data looks "
    "stale), or after the addon was updated, call get_status and pass its advice on to the player.",
    order=90,
)


class BlockedAction(BaseModel):
    at: datetime | None
    kind: str = Field(description="ADDON_ACTION_BLOCKED (\"Interface action failed because of an AddOn\"), "
                      "ADDON_ACTION_FORBIDDEN, or the MACRO_ versions.")
    addon: str = Field(description="The addon the game blamed.")
    function: str = Field(description="The protected function that was blocked.")
    stack: str | None = Field(description="The Lua code path that led to the call.")


class Status(BaseModel):
    game: str = Field(description="The WoW version being played, or the one the addon is installed for.")
    ok: bool = Field(description="The game answered and nothing needs doing.")
    advice: list[str] = Field(description="What to do, in order. Pass it on to the player. Empty when all is well.")
    game_answered: bool
    game_problem: str | None = Field(description="Why the game didn't answer or didn't report its status.")
    repo_build: str | None = Field(description="Build ID of the addon code in the server's checkout.")
    installed_build: str | None = Field(description="Build ID of the addon installed in the game's AddOns folder.")
    loaded_build: str | None = Field(description="Build ID the game is running; it changes on /reload.")
    live_updates: bool | None = Field(description="Updates reach the game without a /reload.")
    live_problem: str | None
    slots_left: int | None = Field(description="Live updates left in this UI session; /reload resets them.")
    slot_count: int | None
    last_update_sent: int | None = Field(description="Number of the latest update the server sent.")
    last_update_in_game: int | None = Field(description="Number of the latest update the game has applied.")
    blocked_actions: list[BlockedAction] = Field(
        default_factory=list, description="The last actions the game blocked because of an addon (any addon), newest "
        "first, with the code path. Empty when none were seen.")


@dataclass
class Findings:
    """What needs doing. Installing comes first, then a restart (which includes a reload), then a reload."""

    install: str | None = None
    restart: str | None = None
    reload: str | None = None
    notes: list[str] = field(default_factory=list)

    def advice(self, folder: Path) -> list[str]:
        steps = []
        if self.install:
            then = "then fully quit and restart WoW" if self.restart else "then /reload in game"
            steps.append(f'Run wow-mcp-install (from the server folder: uv run wow-mcp-install "{folder}"): '
                         f"{self.install}; {then}.")
        elif self.restart:
            steps.append(f"Fully quit and restart WoW: {self.restart} (the game only discovers new addon files "
                         "at startup).")
        elif self.reload:
            steps.append(f"/reload in game: {self.reload}.")
        return steps + self.notes


def _repo_build() -> str | None:
    try:
        return install.addon_build()
    except OSError:
        return None


def _installed_build(addon_dir: Path) -> str | None:
    try:
        match = BUILD_LINE.search((addon_dir / "Build.lua").read_text(encoding="utf-8"))
    except OSError:
        return None
    return match.group(1) if match else None


def _check_disk(folder: Path, installed: str | None, repo: str | None, found: Findings) -> None:
    slots = all((folder / slot_name(i) / f"{slot_name(i)}.toc").is_file() for i in range(1, SLOT_COUNT + 1))
    if installed is None or not slots:
        found.install = "addon files are missing from the AddOns folder"
        found.restart = "the installer adds new files"
    elif repo is not None and installed != repo:
        found.install = "the addon code in the server's checkout is newer than the installed copy"


def _check_game(status: dict, installed: str | None, sent: int, found: Findings) -> None:
    loaded = status.get("build")
    if loaded is None:
        found.restart = "the game hasn't loaded the addon's Build.lua"
    elif loaded != installed:
        found.reload = "the installed addon code is newer than what the game has loaded"

    slot_problem, slots_left = status.get("slotProblem"), status.get("slotsLeft")
    if slot_problem == "MISSING":
        found.restart = found.restart or "the game can't find the live-update slot addons"
    elif slot_problem == "DISABLED":
        found.notes.append(f"Enable the WowMCP slot addons ({slot_name(1)}-{slot_name(SLOT_COUNT)}, grouped under "
                           "WowMCP in the AddOns list), then /reload.")
    elif slot_problem not in (None, "EXHAUSTED"):
        found.notes.append(f"The game couldn't load a live-update slot ({slot_problem}): /reload, and if that "
                           "doesn't help, fully quit and restart WoW.")
    if slots_left == 0:
        found.reload = found.reload or "all live-update slots of this session are used, so updates wait for a /reload"
    elif isinstance(slots_left, int) and slots_left < LOW_SLOTS:
        found.notes.append(f"Only {slots_left} live updates left this session; a /reload when convenient resets them.")
    if status.get("live") is False:
        found.notes.append(f"Live updates are off ({status.get('liveProblem') or 'unknown reason'}): fix that and "
                           "/reload. Until then, new data only shows after a /reload.")

    in_game = status.get("seq")
    if not isinstance(in_game, int) or in_game == sent:
        return
    if in_game > sent:
        found.reload = found.reload or "the game has seen newer updates than this server sent, so it ignores new ones"
    elif status.get("pendingLoad"):
        found.notes.append("The latest update is waiting for the player to leave combat.")
    elif slots_left != 0 and status.get("live") is not False:
        found.notes.append(f"The game has update #{in_game} but the server sent #{sent}; if it doesn't show within a "
                           "few seconds, /reload.")


@tool(annotations=ToolAnnotations(title="WowMCP status", read_only_hint=True, open_world_hint=False))
async def get_status() -> Status:
    """Check the connection to the game: whether the addon in game is up to date with the installed copy and
    the server's, whether live updates work and how many are left, and the game version. Returns advice on
    what to fix. Use it when another WoW tool fails or behaves oddly, or after the addon was updated."""
    unknown = dict(game_answered=False, installed_build=None, loaded_build=None, live_updates=None,
                   live_problem=None, slots_left=None, slot_count=None, last_update_sent=None,
                   last_update_in_game=None)
    repo = _repo_build()
    folder = addons_dir()
    if not folder:
        return Status(**unknown, game="unknown", ok=False, repo_build=repo,
                      game_problem=f"{ADDONS_DIR_ENV} is not set.",
                      advice=[f"Set {ADDONS_DIR_ENV} in this MCP server's configuration to the game's "
                              "Interface/AddOns folder."])
    now = game.situation(folder)
    version = (now.mismatch or (now.running[0] if now.running else None) or now.installed).describe()
    addon_dir = folder / ADDON
    if not (addon_dir / f"{ADDON}.toc").is_file():
        found = Findings(install="the addon isn't installed", restart="the installer adds new files")
        return Status(**unknown, game=version, ok=False, repo_build=repo,
                      game_problem=f"{ADDON} is not installed in {folder}.", advice=found.advice(folder))

    found = Findings()
    installed = _installed_build(addon_dir)
    _check_disk(folder, installed, repo, found)
    sent = transport().load_state().get("seq", 0)

    status, problem, answered = None, None, False
    try:
        reply, version = await request(kinds.STATUS)
    except ToolError as e:
        problem = str(e)
        if now.running and not now.mismatch:
            found.notes.append(
                "Ask the player to type /mcp status in game. If it says live updates are off, fix the reason it "
                "gives (e.g. turn on game sound) and /reload. If /mcp isn't a command, the WowMCP addon isn't "
                "loaded: enable it in the AddOns list, or fully restart WoW if it isn't listed."
            )
    else:
        answered = True
        status = reply.get("status")
        if isinstance(status, dict):
            _check_game(status, installed, sent, found)
        else:
            status = None
            if error := (reply.get("errors") or {}).get("status"):
                problem = f"The game couldn't collect its status: {error}"
            else:
                # The addon in game predates Status.lua, or the game hasn't seen that file yet.
                problem = "The addon in game doesn't report its status yet, so it runs older addon code."
                found.reload = found.reload or "the game is running older addon code"
                found.notes.append("If get_status still can't read the addon's status after a /reload, fully quit "
                                   "and restart WoW.")

    advice = found.advice(folder)
    return Status(
        game=version, ok=status is not None and not advice, advice=advice, game_answered=answered,
        game_problem=problem, repo_build=repo, installed_build=installed,
        loaded_build=status.get("build") if status else None,
        live_updates=status.get("live") if status else None,
        live_problem=status.get("liveProblem") if status else None,
        slots_left=status.get("slotsLeft") if status else None,
        slot_count=status.get("slotCount") if status else None,
        last_update_sent=sent, last_update_in_game=status.get("seq") if status else None,
        blocked_actions=_blocked(status.get("blocked")) if status else [],
    )


def _blocked(raw) -> list[BlockedAction]:
    actions = []
    for b in as_list(raw):
        if not isinstance(b, dict):
            continue
        at = b.get("at")
        actions.append(BlockedAction(
            at=datetime.fromtimestamp(at, timezone.utc) if isinstance(at, (int, float)) else None,
            kind=str(b.get("event") or "?"), addon=str(b.get("addon") or "?"), function=str(b.get("func") or "?"),
            stack=b.get("stack") if isinstance(b.get("stack"), str) else None))
    return actions
