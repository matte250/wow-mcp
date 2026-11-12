"""The player's own lists, kept for good per character (tools/lists.py): what's on them is the player's to decide."""

import asyncio
import json
import time

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from wow_mcp import app, bridge, kinds, memory, server
from wow_mcp.tools import lists, visuals
from wow_mcp.tools import memory as memory_tools
from wow_mcp.tools.lists import delete_list, get_list, get_lists, update_list

BALL = {"item_id": 18731, "label": "Heavy Leather Ball", "note": "buy at Wenna Silkbeard; sells Saturdays",
        "why": "the player asked"}
WOOD = {"item_id": 4470, "label": "Simple Wood"}
CHERRY = {"label": "Rugged Leather from Cherry", "note": "she's on in the evenings"}


@pytest.fixture(autouse=True)
def no_game(monkeypatch):
    """No game here unless a test installs one (the addons fixture): update_list looks for views of a list in it."""
    monkeypatch.delenv("WOW_MCP_ADDONS_DIR", raising=False)
    monkeypatch.setattr(app, "_transport", None)


@pytest.fixture
def slim(monkeypatch):
    monkeypatch.setenv(memory.PLAYER_ENV, "Slim")


def run(coroutine):
    return asyncio.run(coroutine)


def saved():
    return json.loads(lists.Lists().path.read_text(encoding="utf-8"))


def entries(name, player=None):
    return [(e.item_id, e.label, e.note) for e in run(get_list(name, player=player)).entries]


def test_make_add_remove_and_delete(slim):
    assert run(get_lists()).lists == [] and run(get_lists()).note == "Slim has no lists yet."
    result = run(update_list("Good flips", add=[BALL, WOOD], description="Vendor items worth reselling"))
    assert result == ("Made the list 'Good flips' for Slim: added {item:18731:Heavy Leather Ball} and "
                      "{item:4470:Simple Wood}. It has 2 entries.")

    mine = run(get_lists())
    assert mine.player == "Slim" and mine.note is None
    [summary] = mine.lists
    assert (summary.name, summary.description, summary.entries) == ("Good flips", "Vendor items worth reselling", 2)
    assert summary.made == time.strftime("%Y-%m-%d")

    flips = run(get_list("Good flips"))
    assert (flips.player, flips.name, flips.description) == ("Slim", "Good flips", "Vendor items worth reselling")
    ball, wood = flips.entries
    assert (ball.item_id, ball.label, ball.note, ball.why) == (18731, "Heavy Leather Ball", BALL["note"],
                                                               "the player asked")
    assert ball.added.startswith(time.strftime("%Y-%m-%d ")) and (wood.note, wood.why) == (None, None)
    assert "show every entry, also those that aren't worth it right now" in flips.note

    assert run(update_list("Good flips", remove=[4470])) == (
        "Changed Slim's list 'Good flips': removed {item:4470:Simple Wood}. It has 1 entry.")
    # Removing the last entry keeps the list, empty.
    assert run(update_list("Good flips", remove=[18731])).endswith("It has 0 entries.")
    empty = run(get_list("Good flips"))
    assert empty.entries == [] and empty.note.startswith("The list is empty.")
    assert [x.entries for x in run(get_lists()).lists] == [0]

    # The description changes only when given; '' clears it.
    assert run(update_list("Good flips", description="Flips from vendors")) == (
        "Changed Slim's list 'Good flips': updated what it's for. It has 0 entries.")
    assert run(update_list("Good flips")) == "Nothing changed in Slim's list 'Good flips'. It has 0 entries."
    assert run(get_list("Good flips")).description == "Flips from vendors"
    run(update_list("Good flips", description=""))
    assert run(get_list("Good flips")).description is None and "description" not in saved()["Slim"][0]

    run(update_list("Shopping", add=[CHERRY]))
    assert run(delete_list("Good flips")) == "Deleted Slim's list 'Good flips'."
    assert run(delete_list("Shopping")) == ("Deleted Slim's list 'Shopping' with its 1 entry: 'Rugged Leather from "
                                            "Cherry'.")
    with pytest.raises(ToolError, match="Slim has no list 'Good flips'. They have no lists yet."):
        run(get_list("Good flips"))
    with pytest.raises(ToolError, match="Slim has no list 'Good flips'"):
        run(delete_list("Good flips"))
    # Removing from a list that isn't there doesn't make one.
    with pytest.raises(ToolError, match="Slim has no list 'Good flips'"):
        run(update_list("Good flips", remove=[18731]))
    assert saved() == {"Slim": []}


def test_names_in_any_case(slim):
    run(update_list("Good flips", add=[BALL]))
    assert run(update_list("  good   FLIPS ", add=[WOOD])).startswith("Changed Slim's list 'Good flips': added")
    assert [x.name for x in run(get_lists()).lists] == ["Good flips"], "the first spelling stays"
    assert run(get_list("GOOD FLIPS")).name == "Good flips" and len(entries("good flips")) == 2
    run(update_list("Shopping", add=[CHERRY]))
    with pytest.raises(ToolError, match="Slim has no list 'Flips'. Their lists: 'Good flips', 'Shopping'."):
        run(get_list("Flips"))
    assert run(delete_list("good flips")).startswith("Deleted Slim's list 'Good flips' with its 2 entries")
    assert [x.name for x in run(get_lists()).lists] == ["Shopping"]
    with pytest.raises(ToolError, match="name the list in a few words"):
        run(update_list("---", add=[WOOD]))


def test_entries_by_item_and_by_label(slim):
    # An item as the AI writes it, {item:<id>:<name>}, is an item too.
    result = run(update_list("Good flips", add=[CHERRY, {"label": "{item:2320:Coarse Thread}"}, {"item_id": 4470}]))
    assert result == ("Made the list 'Good flips' for Slim: added 'Rugged Leather from Cherry', "
                      "{item:2320:Coarse Thread} and {item:4470}. It has 3 entries.")
    assert entries("Good flips") == [(None, "Rugged Leather from Cherry", "she's on in the evenings"),
                                     (2320, "Coarse Thread", None), (4470, None, None)]
    with pytest.raises(ToolError, match="Nothing was changed:\nadd.1: an entry needs an item_id or a label"):
        run(update_list("Good flips", add=[WOOD, {"note": "something"}]))
    assert len(entries("Good flips")) == 3

    # Already on it: not added twice. With a note, its note is updated; a name or ID it lacked is filled in.
    result = run(update_list("Good flips", add=[{"item_id": 2320, "label": "Coarse Thread"},
                                                {"item_id": 4470, "label": "Simple Wood", "note": "Wenna, 38c"},
                                                {"label": "rugged leather from cherry"}]))
    assert result == ("Changed Slim's list 'Good flips': updated {item:4470:Simple Wood}. Already on it: "
                      "{item:2320:Coarse Thread} and 'Rugged Leather from Cherry'. It has 3 entries.")
    assert entries("Good flips")[2] == (4470, "Simple Wood", "Wenna, 38c")
    # Added by name first, the item later: the same entry, now with its ID.
    run(update_list("Good flips", add=[{"label": "Heavy Leather Ball"}]))
    assert run(update_list("Good flips", add=[BALL])).startswith(
        "Changed Slim's list 'Good flips': updated {item:18731:Heavy Leather Ball}.")
    assert entries("Good flips")[3] == (18731, "Heavy Leather Ball", BALL["note"])
    assert run(get_list("Good flips")).entries[3].why is None, "it keeps when and why it was first added"

    # Removed by item ID (a number, as text, or as the AI writes items) or by label, in any case.
    result = run(update_list("Good flips", remove=["RUGGED leather from cherry", "{item:2320:Coarse Thread}", "4470",
                                                   "Mithril Casing", 999]))
    assert result == ("Changed Slim's list 'Good flips': removed 'Rugged Leather from Cherry', "
                      "{item:2320:Coarse Thread} and {item:4470:Simple Wood}. Not on it: 'Mithril Casing' and "
                      "{item:999}. It has 1 entry.")
    assert run(update_list("Good flips", remove=["heavy leather ball"])).endswith("It has 0 entries.")


def test_entries_stay_until_the_player_removes_them(slim):
    run(update_list("Good flips", add=[BALL, WOOD, CHERRY]))
    kept = entries("Good flips")
    # Nothing else takes an entry off: adding others, changing what it's for, other lists, other characters.
    run(update_list("Good flips", add=[{"item_id": 2320, "label": "Coarse Thread"}], description="Flips"))
    run(update_list("Good flips", add=[BALL, WOOD]))
    run(update_list("Shopping", add=[WOOD]))
    run(update_list("Shopping", remove=[4470]))
    run(delete_list("Shopping"))
    run(update_list("Good flips", add=[CHERRY], player="Brannoc"))
    run(delete_list("Good flips", player="Brannoc"))
    assert entries("Good flips")[:3] == kept and len(entries("Good flips")) == 4

    # A full list refuses more rather than dropping any.
    run(update_list("Big", add=[{"item_id": n} for n in range(1, 51)]))
    for start in (51, 101, 151):
        run(update_list("Big", add=[{"item_id": n} for n in range(start, start + 50)]))
    with pytest.raises(ToolError, match="Nothing was changed: a list holds at most 200 entries"):
        run(update_list("Big", add=[{"item_id": 4470}]))
    assert [e[0] for e in entries("Big")] == list(range(1, 201))
    assert run(update_list("Big", add=[{"item_id": 7, "note": "still fine"}])).startswith("Changed")

    # What can't be read in the file is left alone, and a file that can't be read is never saved over.
    data = saved()
    data["Slim"].append("junk")
    lists.Lists().path.write_text(json.dumps(data), encoding="utf-8")
    run(update_list("Good flips", add=[{"label": "More"}]))
    assert saved()["Slim"][-1] == "junk"
    lists.Lists().path.write_text("{broken", encoding="utf-8")
    assert run(get_lists()).lists == []
    with pytest.raises(ToolError, match="Couldn't change the lists: can't read the lists in"):
        run(update_list("Good flips", add=[WOOD]))
    assert lists.Lists().path.read_text(encoding="utf-8") == "{broken"


def test_per_character(slim, monkeypatch):
    run(update_list("Good flips", add=[BALL]))
    run(update_list("Good flips", add=[WOOD], player="Brannoc"))
    assert entries("Good flips") == [(18731, "Heavy Leather Ball", BALL["note"])]
    assert entries("Good flips", player="Brannoc") == [(4470, "Simple Wood", None)]
    # Names are typed in any case: as first saved.
    run(update_list("Shopping", add=[CHERRY], player="brannoc"))
    assert [x.name for x in run(get_lists(player="BRANNOC")).lists] == ["Good flips", "Shopping"]
    assert list(saved()) == ["Slim", "Brannoc"]

    # Asked in game, the bridge says who; elsewhere the game says who's logged in.
    monkeypatch.setenv(memory.PLAYER_ENV, "Brannoc")
    assert run(get_lists()).player == "Brannoc"
    monkeypatch.delenv(memory.PLAYER_ENV)

    async def request(kind):
        assert kind == kinds.STATE
        return {"character": {"name": "Slim"}}, "World of Warcraft: Forever 1.60.1"

    monkeypatch.setattr(memory_tools, "request", request)
    assert run(get_list("good flips")).player == "Slim"


def test_kept_in_the_data_folder(slim, tmp_path):
    run(update_list("Good flips", add=[BALL]))
    path = tmp_path / "data" / "wow-mcp" / lists.LISTS_FILE
    assert lists.Lists().path == path == memory.data_dir() / "lists.json"
    assert path.is_file() and path.with_name("lists.json.lock").is_file()
    assert saved() == {"Slim": [{"name": "Good flips", "at": pytest.approx(time.time(), abs=60), "entries": [
        {"item": 18731, "label": "Heavy Leather Ball", "note": BALL["note"], "at": pytest.approx(time.time(), abs=60),
         "why": "the player asked"}]}]}


def test_views_of_a_list(addons, slim):
    """A curated view (visualize's list) shows the list: changing or deleting the list says so."""
    text = [{"type": "text", "text": "Profit per item"}]
    visuals.visualize("Good flips", text, name="Flips", refresh=lists.GOOD_FLIPS, list="good FLIPS")
    visuals.visualize("Flip results", text, name="Flip results", refresh="Call get_auction_history: my flips' results")
    result = run(update_list("good flips", add=[BALL]))
    assert result.endswith("The named visual 'Flips' shows this list: update it, or say the next Refresh shows the "
                           "change.")
    assert "named visual" not in run(update_list("Good flips", description="Vendor flips")), "nothing to show anew"
    assert run(delete_list("Good flips")).endswith(
        "The named visual 'Flips' still shows it: tell the player, who may want it removed too (remove_visual).")
    assert "named visual" not in run(update_list("Shopping", add=[CHERRY]))


def test_agents_are_told_whose_lists_they_are():
    instructions = server.create_server().instructions
    assert lists.GUIDANCE in instructions and lists.VIEWS_GUIDANCE in instructions
    assert instructions.index(lists.GUIDANCE) > instructions.index("Give a visual a name"), "after named visuals"
    for said in ("change it only when they ask", "'add X to Good flips'", "suggest additions",
                 "'Suggestions' block", "add them only once the player agrees", "Never remove an entry on your own",
                 "not even one that's bad right now", "show its current state instead", "in their words",
                 "255 characters", "several items in one message", "follow up on them"):
        assert said in lists.GUIDANCE, said
    for said in ("curated view of a list", "list=<the list's name>", "refresh instructions that name the list",
                 "Every refresh then reads the list afresh", "derived data (results, trackers, reports) stay automatic",
                 "if you can't tell which they want, ask", "in the same layout", "'Good flips'",
                 "with list='Good flips'", "Make it with the view, so its layout has it",
                 "profit after the auction house's cut", "the cheapest vendor and where it is", "how many are listed",
                 "get_auction_history", "never dropped", "separate 'Suggestions' block",
                 "suggestions for the player to approve"):
        assert said in lists.VIEWS_GUIDANCE, said
    # The example's refresh instructions fit a named visual's and name the list.
    assert len(lists.GOOD_FLIPS) <= visuals.MAX_REFRESH and "my list 'Good flips'" in lists.GOOD_FLIPS
    visuals.Visual(title="Good flips", blocks=[{"type": "text", "text": "x"}], name="Good flips",
                   refresh=lists.GOOD_FLIPS)


def test_the_tools():
    tools = {fn.__name__: options for fn, options in app.TOOLS}
    for name in ("get_lists", "get_list", "update_list", "delete_list"):
        assert name in tools, name
    assert tools["get_lists"]["annotations"].read_only_hint and tools["get_list"]["annotations"].read_only_hint
    assert tools["update_list"]["annotations"].destructive_hint and tools["delete_list"]["annotations"].destructive_hint
    doc = {fn.__name__: " ".join(fn.__doc__.split()) for fn in (get_lists, get_list, update_list, delete_list)}
    assert "'Good flips'" in doc["get_lists"] and "how many entries" in doc["get_lists"]
    assert "when and why it was added" in doc["get_list"] and "every refresh" in doc["get_list"]
    assert "Only when the player asks" in doc["update_list"] and "agrees to your suggestion" in doc["update_list"]
    assert "Never remove an entry on your own, not even one that's bad right now" in doc["update_list"]
    assert "Removing the last entry keeps the list, empty" in doc["update_list"]
    assert "Only when the player asks" in doc["delete_list"]
    tool = {t.name: t for t in asyncio.run(server.create_server().list_tools())}
    schema = tool["update_list"].input_schema
    assert set(schema["properties"]) == {"name", "add", "remove", "description", "player"}
    assert set(schema["$defs"]["NewEntry"]["properties"]) == {"item_id", "label", "note", "why"}
    assert "in their words" in schema["$defs"]["NewEntry"]["properties"]["note"]["description"]
    for name in ("get_lists", "get_list", "delete_list"):
        assert "player" in tool[name].input_schema["properties"], name
    # The game's status line: no step of their own, so the defaults for reading and changing.
    assert bridge.tool_step("mcp__wow__get_list") == bridge.tool_step("mcp__wow__get_lists") == bridge.LOOKING
    assert bridge.tool_step("mcp__wow__update_list") == bridge.tool_step("mcp__wow__delete_list") == bridge.UPDATING
