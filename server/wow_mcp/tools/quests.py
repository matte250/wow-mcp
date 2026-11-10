"""Quest completion and the quest log with objective progress (request kind QUESTS, Quests.lua)."""

from typing import Annotated

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from wow_mcp import kinds
from wow_mcp.app import as_list, guidance, request, section, tool

guidance(
    "To check quest progress (has the player done a quest, can they get a follow-up, which quests in a "
    "chain are left), look up the quest IDs for this game version and call get_quest_status.",
    order=30,
)
guidance(
    "For what the player is working on (the quests in their log, how far along each objective is, which are "
    "ready to turn in), call get_quest_log. Use it before suggesting what to do next or planning a route for "
    "turn-ins and the objectives left.",
    order=35,
)


class QuestStatus(BaseModel):
    quest_id: int
    completed: bool = Field(description="Turned in on this character.")
    in_quest_log: bool = Field(description="Accepted and not yet turned in.")
    ready_to_turn_in: bool | None = Field(description="Objectives done; only known for quests in the log.")
    title: str | None = Field(description="Only known for quests in the log.")


class QuestStatuses(BaseModel):
    game: str = Field(description="The WoW version being played; quest IDs are for this version.")
    quests: list[QuestStatus]


class QuestObjective(BaseModel):
    text: str = Field(description="As the game shows it, usually with the count, e.g. 'Kobold Vermin slain: 4/10'.")
    fulfilled: int
    required: int
    finished: bool


class LoggedQuest(BaseModel):
    quest_id: int
    title: str
    level: int | None = Field(description="The quest's level, as the quest log shows it.")
    header: str | None = Field(
        description="The quest log header it's under: usually its zone, otherwise a category such as a class "
        "or profession."
    )
    tag: str | None = Field(description="'elite', 'dungeon', 'raid' or 'pvp': likely needs a group or that content.")
    ready_to_turn_in: bool = Field(description="Objectives done; take it to the quest's turn-in NPC.")
    failed: bool = Field(description="Failed (e.g. an escort or timed quest): abandon it and accept it again.")
    objectives: list[QuestObjective] | None = Field(
        description="Empty if the quest only asks to deliver or talk to someone; null if the game didn't give them."
    )


class QuestLog(BaseModel):
    game: str = Field(description="The WoW version being played; quest IDs are for this version.")
    quests: list[LoggedQuest] = Field(description="In quest log order.")


@tool(
    annotations=ToolAnnotations(title="Check quest completion", read_only_hint=True, open_world_hint=False)
)
async def get_quest_status(
    quest_ids: Annotated[
        list[Annotated[int, Field(gt=0)]],
        Field(min_length=1, max_length=500, description="Quest IDs, e.g. from Wowhead for this game version."),
    ],
) -> QuestStatuses:
    """Check which of the given quests the player's character has completed (turned in), and which are in
    their quest log right now (and whether those are ready to turn in)."""
    reply, version = await request(kinds.QUESTS)
    quests = section(reply, "quests")
    completed, total = set(), 0
    for gap in as_list(quests.get("completedGaps")):
        total += gap
        completed.add(total)
    log = {q["id"]: q for q in as_list(quests.get("log")) if isinstance(q, dict)}
    return QuestStatuses(
        game=version,
        quests=[
            QuestStatus(
                quest_id=quest_id,
                completed=quest_id in completed,
                in_quest_log=quest_id in log,
                ready_to_turn_in=log[quest_id].get("ready") if quest_id in log else None,
                title=log[quest_id].get("title") if quest_id in log else None,
            )
            for quest_id in quest_ids
        ],
    )


def _logged_quest(entry: dict) -> LoggedQuest:
    objectives = entry.get("objectives")
    return LoggedQuest(
        quest_id=entry["id"],
        title=entry.get("title") or "",
        level=entry.get("level"),
        header=entry.get("header"),
        tag=entry.get("tag"),
        ready_to_turn_in=bool(entry.get("ready")),
        failed=bool(entry.get("failed")),
        objectives=None if objectives is None else [
            QuestObjective(text=o.get("text") or "", fulfilled=o.get("fulfilled") or 0,
                           required=o.get("required") or 0, finished=bool(o.get("finished")))
            for o in as_list(objectives) if isinstance(o, dict)
        ],
    )


@tool(
    annotations=ToolAnnotations(title="Player's quest log", read_only_hint=True, open_world_hint=False)
)
async def get_quest_log() -> QuestLog:
    """Get every quest in the player's quest log: title, level, zone (its quest log header), whether it's ready
    to turn in or failed, and the progress on each objective. Use it to see what the player is working on and
    what's left to do."""
    reply, version = await request(kinds.QUESTS)
    entries = [q for q in as_list(section(reply, "quests").get("log")) if isinstance(q, dict)]
    if entries and not any("level" in q for q in entries):
        raise ToolError("The WowMCP addon in game doesn't send quest objectives yet. Ask the player to type /reload.")
    return QuestLog(game=version, quests=[_logged_quest(q) for q in entries])
