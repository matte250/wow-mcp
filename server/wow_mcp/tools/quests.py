"""Quest completion and the quest log with objective progress and rewards (request kind QUESTS, Quests.lua)."""

import asyncio
from typing import Annotated, Any, Literal, get_args

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from wow_mcp import kinds
from wow_mcp.app import as_list, guidance, request, section, tool
from wow_mcp.tools.stats import format_money

LOAD_RETRIES, LOAD_WAIT = 2, 1.0  # quest rewards or item names the client is still loading

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
guidance(
    "For levelling advice (how to reach the next level, which quests to turn in), add up the xp of the quests from "
    "get_quest_log and compare the total with xp_to_level from get_character_stats. That xp is what the game will "
    "give this character at their current level. Never estimate quest XP from websites or memory: WoW: Forever "
    "changed quests, and quest XP scales with level (a quest can give less after a level-up). Only quests that are "
    "ready_to_turn_in pay out now. Say plainly when a plan doesn't reach the next level, and list what else would "
    "(more quests, objectives to finish, kills). If a quest's xp is null, say it's unknown instead of guessing.",
    order=36,
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


class RewardItem(BaseModel):
    item_id: int
    name: str | None = Field(description="Null if the game hadn't loaded the item yet; get_item_details has it.")
    count: int


RewardsStatus = Literal["ok", "loading", "hidden", "unknown"]
REWARDS_STATUSES: tuple[RewardsStatus, ...] = get_args(RewardsStatus)


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
    xp: int | None = Field(
        description="XP for turning it in: what the game will give this character at their current level, as the "
        "quest log shows it. Null if unknown (see rewards_status); don't guess it."
    )
    money: str | None = Field(description="Money for turning it in, e.g. '1g 2s 3c'. Null if unknown.")
    money_copper: int | None = Field(description="The same in copper (1g = 100s = 10000c).")
    reward_items: list[RewardItem] | None = Field(description="Items it gives. Null if unknown.")
    reward_choices: list[RewardItem] | None = Field(description="Items to pick one of at turn-in. Null if unknown.")
    rewards_status: RewardsStatus = Field(
        description="ok: the rewards as the game gives them; loading: the game was still loading some (they're null, "
        "or item names are), ask again in a moment; hidden: the game doesn't show this quest's rewards before it's "
        "turned in; unknown: the game didn't give them."
    )


class QuestLog(BaseModel):
    game: str = Field(description="The WoW version being played; quest IDs are for this version.")
    quests: list[LoggedQuest] = Field(description="In quest log order.")
    note: str | None = Field(description="Something the player should do first, e.g. /reload for the rewards.")


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


def _reward_items(value: Any) -> list[RewardItem] | None:
    if value is None:
        return None
    return [RewardItem(item_id=i["id"], name=i.get("name"), count=i.get("count") or 1)
            for i in as_list(value) if isinstance(i, dict) and isinstance(i.get("id"), int)]


def _logged_quest(entry: dict) -> LoggedQuest:
    objectives = entry.get("objectives")
    xp, money = entry.get("xp"), entry.get("money")
    items, choices = _reward_items(entry.get("items")), _reward_items(entry.get("choices"))
    # Quests.lua leaves the status out when it read every reward.
    status = entry.get("rewards")
    if status not in REWARDS_STATUSES:
        status = "unknown" if None in (xp, money, items, choices) else "ok"
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
        xp=xp,
        money=None if money is None else format_money(money),
        money_copper=money,
        reward_items=items,
        reward_choices=choices,
        rewards_status=status,
    )


@tool(
    annotations=ToolAnnotations(title="Player's quest log", read_only_hint=True, open_world_hint=False)
)
async def get_quest_log() -> QuestLog:
    """Get every quest in the player's quest log: title, level, zone (its quest log header), whether it's ready
    to turn in or failed, the progress on each objective, and the rewards: the XP this character gets for turning
    it in (calculated by the game for their current level), money and items. Use it to see what the player is
    working on, what's left to do and what turning quests in will give."""
    load_waits = 0
    while True:
        reply, version = await request(kinds.QUESTS)
        entries = [q for q in as_list(section(reply, "quests").get("log")) if isinstance(q, dict)]
        if entries and not any("level" in q for q in entries):
            raise ToolError(
                "The WowMCP addon in game doesn't send quest objectives yet. Ask the player to type /reload."
            )
        # The addon asked the client for what it reported as loading, so it's likely there a moment later.
        if load_waits < LOAD_RETRIES and any(q.get("rewards") == "loading" for q in entries):
            load_waits += 1
            await asyncio.sleep(LOAD_WAIT)
            continue
        # An addon loaded before rewards were added sends none for any quest.
        old = entries and not any("xp" in q or "rewards" in q for q in entries)
        return QuestLog(
            game=version,
            quests=[_logged_quest(q) for q in entries],
            note="The WowMCP addon in game doesn't send quest rewards yet. Ask the player to type /reload."
            if old else None,
        )
