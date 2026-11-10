"""Quest completion and the quest log (request kind QUESTS, Quests.lua)."""

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from wow_mcp import kinds
from wow_mcp.app import as_list, guidance, request, section, tool

guidance(
    "To check quest progress (has the player done a quest, can they get a follow-up, which quests in a "
    "chain are left), look up the quest IDs for this game version and call get_quest_status.",
    order=30,
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
