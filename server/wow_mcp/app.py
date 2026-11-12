"""What the feature modules in wow_mcp/tools/ share: the tool registry, the connection to the
game, and helpers for asking the game for data.

A feature module registers its tools with `@tool(...)` and its advice to agents with
`guidance(...)`; server.py builds the MCP server from them. Nothing else needs editing to add
a feature.
"""

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from wow_mcp import game, stream
from wow_mcp.install import ADDONS_DIR_ENV
from wow_mcp.transport import ADDON, Transport

TOOLS: list[tuple[Callable[..., Any], dict[str, Any]]] = []
GUIDANCE: list[tuple[int, str]] = []


def tool(**options: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a tool function; options are passed to MCPServer.add_tool."""

    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        TOOLS.append((fn, options))
        return fn

    return register


def guidance(text: str, order: int = 50) -> None:
    """Add a paragraph to the instructions agents get; lower `order` comes first."""
    GUIDANCE.append((order, text))


def addons_dir() -> Path | None:
    raw = os.environ.get(ADDONS_DIR_ENV)
    return Path(raw).expanduser() if raw else None


def game_note() -> str:
    """What the agent should assume about the game version."""
    folder = addons_dir()
    if not folder:
        return "The game version is unknown; ask the player if it matters."
    now = game.situation(folder)
    running = now.mismatch or (now.running[0] if now.running else None)
    current = running or now.installed
    note = (
        f"The player {'is playing' if running else 'plays'} {current.describe()}. Look things up for "
        "that version specifically: NPCs, coordinates, zones and quests differ between versions of WoW, "
        "so say so when you have to fall back to information about another version."
    )
    if now.mismatch:
        note += (
            f" The WowMCP addon is only installed for {now.installed.name}, so these tools won't reach "
            "the game until the player switches back."
        )
    return note


def mismatch_error() -> str | None:
    folder = addons_dir()
    if not folder:
        return None
    now = game.situation(folder)
    if now.mismatch:
        return (
            f"The running game is {now.mismatch.name}, but the WowMCP addon is installed for "
            f"{now.installed.name}, so it can't be reached."
        )
    return None


_transport: Transport | None = None


def transport() -> Transport:
    global _transport
    if _transport is None:
        folder = addons_dir()
        if not folder:
            raise ToolError(f"{ADDONS_DIR_ENV} is not set; it must point at the game's Interface/AddOns folder.")
        if not (folder / ADDON / f"{ADDON}.toc").is_file():
            raise ToolError(f"{ADDON} is not installed in {folder}. Run wow-mcp-install first.")
        _transport = Transport(folder)
    return _transport


# The addon takes a request within about 2.5 s (transport.RECEIPT_WAIT) and starts its reply right after;
# replies then stream at a few KB/s.
START_TIMEOUT = 4.0
IDLE_TIMEOUT = 2.0


async def request(kind: int) -> tuple[dict, str]:
    """Ask the game for one kind of data (wow_mcp/kinds.py).

    Returns the reply, { section name: value } as the addon's sections produced it, and a
    description of the game version.
    """
    if problem := mismatch_error():
        raise ToolError(problem)
    now = game.situation(transport().addons_dir)
    if not now.running:
        raise ToolError(f"{now.installed.name} isn't running.")
    for attempt in range(2):
        try:
            with stream.Receiver(transport().addon_dir / "out") as receiver:
                n = await asyncio.to_thread(transport().request, kind, receiver)
                reply = await asyncio.to_thread(receiver.receive, n, START_TIMEOUT, IDLE_TIMEOUT)
            if isinstance(reply, dict):
                return reply, now.installed.describe()
        except stream.Corrupt:
            if attempt:
                raise ToolError("The game's reply arrived damaged twice; try again.") from None
        except stream.Timeout:
            raise ToolError(
                "The game didn't answer. Is the WowMCP addon running with live updates on? "
                "('/mcp status' in game shows it.)"
            ) from None
        except stream.StreamError as e:
            raise ToolError(str(e)) from e
    # Usually an addon loaded before this kind existed: it answers with nothing.
    raise ToolError("The game sent an unexpected reply. If the addon was updated since the player's "
                    "last /reload, ask them to type /reload; get_status says whether it's current.")


def section(reply: dict, name: str) -> Any:
    """One section of a reply, or a ToolError saying why the game didn't send it."""
    if name in reply:
        return reply[name]
    error = (reply.get("errors") or {}).get(name)
    if error:
        raise ToolError(f"The game couldn't collect {name}: {error}")
    raise ToolError(f"The WowMCP addon in game doesn't send {name} yet. Ask the player to type /reload.")


def as_list(value: Any) -> list:
    """A Lua array from a reply: an empty Lua table serializes as {} rather than []."""
    return value if isinstance(value, list) else []
