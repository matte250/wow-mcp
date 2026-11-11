"""The AI's long-term memory (memory.py, tools/memory.py): the archive of every /ai exchange and
searching it, the notes about each player, what the bridge does with both, and importing old bridge
logs. Everything lives in the test's own data folder (conftest's no_real_memory)."""

import asyncio
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from wow_mcp import app, bridge, game, memory, server, stream
from wow_mcp.bridge import Bridge, CommandAgent
from wow_mcp.tools import memory as memory_tools
from wow_mcp.tools.memory import forget_note, list_notes, remember, search_conversations


def quiet(text, style, sender, reply, default_model):
    pass


def wait_for(condition, seconds=10):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def run(coroutine):
    return asyncio.run(coroutine)


# The archive ---------------------------------------------------------------------------------


def test_every_exchange_is_archived_for_good(addons, tmp_path):
    now = [1_000_000.0]
    long = "First line.\n\n" + "word " * 400 + "end."
    outcomes = ["Karolek, in The Drag.\n", bridge.AgentError("overloaded"), long]

    def agent(prompt, model, player):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def started():  # a bridge, as after a restart
        return Bridge(addons, agent, publish=quiet, history=bridge.History(None, clock=lambda: now[0]), models=True,
                      model="opus")

    started().answer(bridge.Question("where's the trainer?", "Thorgrim", "sonnet"))
    now[0] += 60
    started().answer(bridge.Question("and skinning?", "Thorgrim"))  # failed: not kept
    started().answer(bridge.Question("and skinning?", "Thorgrim"))

    # One line each, whole answers with their line breaks, apart from the game's folder.
    path = tmp_path / "data" / "wow-mcp" / "conversations.jsonl"
    assert [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] == [
        {"at": 1_000_000.0, "player": "Thorgrim", "model": "sonnet", "question": "where's the trainer?",
         "answer": "Karolek, in The Drag."},
        {"at": 1_000_060.0, "player": "Thorgrim", "model": "opus", "question": "and skinning?", "answer": long},
    ]
    assert not list(addons.rglob("conversations.jsonl"))

    # A line cut short (a crash while writing) stays broken, and doesn't break the next ones.
    with open(path, "ab") as f:
        f.write(b'{"at": 1000090.0, "player": "Thor')
    archive = memory.Archive()
    archive.add("Thorgrim", "and mining?", "Geofram, in Ironforge.", 1_000_120.0)
    assert [(e.id, e.question) for e in archive.entries()] == [
        (1, "where's the trainer?"), (2, "and skinning?"), (4, "and mining?"),
    ]


def test_a_new_conversation_keeps_the_archive(addons):
    """/ai new: the next prompt starts afresh, but nothing leaves the archive."""
    prompts, said = [], []

    def agent(prompt, model, player):
        prompts.append(prompt)
        return f"Answer {len(prompts)}."

    b = Bridge(addons, agent, publish=lambda text, style, sender, reply, default_model: said.append(text))
    worker = threading.Thread(target=b._work, daemon=True)
    worker.start()
    try:
        for post_id, value in enumerate([{"question": "where am I?"}, {"reset": True}, {"question": "what now?"}], 1):
            b.accept(stream.Post(1, post_id, end=0, value={**value, "player": "Thorgrim"}))
        wait_for(lambda: len(said) == 2)
    finally:
        b.stop.set()
        worker.join(5)
    assert prompts == ["Thorgrim asks, in game: where am I?", "Thorgrim asks, in game: what now?"]
    assert [(e.question, e.answer) for e in memory.Archive().entries()] == [
        ("where am I?", "Answer 1."), ("what now?", "Answer 2."),
    ]


# Searching -----------------------------------------------------------------------------------


def archive_of(*exchanges, now=None):
    """(hours ago, player, question, answer) each, into the archive; returns now."""
    now = time.time() if now is None else now
    memory.Archive().extend([memory.record(p, q, a, now - hours * 3600) for hours, p, q, a in exchanges])
    return now


def found(**search):
    return [e.question for e in search_conversations(**search).exchanges]


FLIP = "I want you to think outside the box regarding getting gold"
COST = "heavy leather ball cost me 18s. is that what you expected?"
BELT = "Should i sell [Dark Leather Belt] (item 4249) at AH or vendor?"
TOY = "what does a Heavy Leather Ball do?"


def test_search_finds_every_word_in_any_case():
    archive_of(
        (50, "Slim", "where is the closest mining trainer?", "Geofram Bouldertoe, in Ironforge."),
        (26, "Slim", FLIP, ("Best idea is a vendor flip: {item:18731:Pattern: Heavy Leather Ball} costs 18s, the AH "
                            "has it for 63s.")),
        (25, "Slim", COST, "Yes, that is exactly what I expected."),
        (24.5, "Slim", BELT, "I'd vendor them: the AH is flooded with {item:4249:Dark Leather Belt}."),
        (1, "Brannoc", TOY, "It's a toy."),
    )
    # All the words, in the question or the answer (or some in each); newest first.
    assert found(query="Heavy Leather Ball") == [TOY, COST, FLIP]
    assert found(query="BALL expected") == [COST]
    assert found(query="ball mining") == []
    # A word also finds longer ones, never the middle of one.
    assert found(query="vend") == [BELT, FLIP]
    assert found(query="ore") == []
    # Items by name and by ID, in the agent's markup or as the game sends shift-clicked links; IDs only whole.
    assert found(query="18731") == [FLIP]
    assert found(query="{item:18731}") == [FLIP]
    assert found(query="{item:18731:Pattern: Heavy Leather Ball}") == [FLIP]
    assert found(query="1873") == []
    assert found(query="4249") == [BELT]
    assert found(query="dark leather belt") == [BELT]
    # No query: the most recent exchanges.
    assert found() == [TOY, BELT, COST, FLIP, "where is the closest mining trainer?"]

    result = search_conversations(query="ball", limit=1)
    assert result.matches == 3 and result.note == "2 older match(es): offset=1 gives the next ones."
    exchange = result.exchanges[0]
    assert (exchange.player, exchange.question, exchange.answer) == ("Brannoc", TOY, "It's a toy.")
    assert re.fullmatch(r"\w{3} \d{4}-\d\d-\d\d \d\d:\d\d \((59|60) minutes ago\)", exchange.when), exchange.when
    assert re.fullmatch(r"\w{3} \d{4}-\d\d-\d\d \d\d:\d\d", result.now)
    assert search_conversations(query="ball", offset=1, limit=1).exchanges[0].question == COST


def test_search_by_date():
    now = archive_of(
        (50, "Slim", "two days ago", "a"), (30, "Slim", "yesterday-ish", "b"), (2, "Slim", "today-ish", "c"),
    )
    memory.Archive().extend([
        memory.record("Slim", question, "d", datetime(2025, 3, day, hour, minute).timestamp())
        for question, day, hour, minute in [("late", 1, 23, 59), ("early", 2, 0, 1), ("noon", 2, 12, 0),
                                            ("next day", 3, 0, 0)]
    ])
    assert found(days=1) == ["today-ish"]
    assert found(days=2) == ["today-ish", "yesterday-ish"]
    assert found(days=3) == ["today-ish", "yesterday-ish", "two days ago"]
    # A calendar day in local time, also with words.
    assert found(date="2025-03-02") == ["noon", "early"]
    assert found(date="2025-03-02", query="EARLY") == ["early"]
    assert found(date="2025-03-02", days=7) == []
    assert found(date=datetime.fromtimestamp(now - 2 * 3600).strftime("%Y-%m-%d"))[0] == "today-ish"
    nothing = search_conversations(date="2024-01-01")
    assert nothing.note == "Nothing matches. Try fewer or other words, or a longer time."
    with pytest.raises(ToolError, match="'2025-02-30' isn't a date"):
        search_conversations(date="2025-02-30")


def test_search_results_stay_short():
    long = ("Intro. " * 150 + "Then the {item:18731:Pattern: Heavy Leather Ball} sold for 62s to Storm Day. "
            + "More. " * 100)
    archive_of(*[(10 + i, "Slim", f"question {i}", f"answer {i}") for i in range(15)],
               (1, "Slim", "look at the mail now", long))

    # Cut around what was searched for, with items whole, and a way to read all of it.
    result = search_conversations(query="sold storm")
    shown = result.exchanges[0]
    assert shown.answer_length == len(long) and len(shown.answer) <= memory_tools.SHOWN
    assert shown.answer.startswith("…Intro.") and shown.answer.endswith("…")
    assert "{item:18731:Pattern: Heavy Leather Ball} sold for 62s to Storm Day." in shown.answer
    assert result.note == "Answers with answer_length are cut: exchange=<id> gives one whole."
    whole = search_conversations(exchange=shown.id)
    assert whole.matches == 1 and whole.exchanges[0].answer == long and whole.exchanges[0].answer_length is None
    with pytest.raises(ToolError, match="There's no exchange 99"):
        search_conversations(exchange=99)

    # Without a query, a long answer shows its start; short ones are whole.
    recent = search_conversations(limit=3)
    assert recent.exchanges[0].answer.startswith("Intro. Intro.") and recent.exchanges[0].answer.endswith("…")
    assert [e.answer for e in recent.exchanges[1:]] == ["answer 0", "answer 1"]
    assert recent.matches == 16 and "13 older match(es): offset=3 gives the next ones." in recent.note

    # Ten by default, at most thirty.
    assert len(search_conversations().exchanges) == 10
    assert found(limit=5, offset=12) == ["question 11", "question 12", "question 13", "question 14"]
    assert search_conversations(offset=20).note == "Only 16 match; the offset is past them."


def test_an_empty_archive_says_so():
    assert search_conversations(query="ball").model_dump() | {"now": ""} == {
        "now": "", "matches": 0, "exchanges": [], "note": "No earlier conversations have been kept yet.",
    }


def test_excerpts_never_cut_items():
    token = "{item:18731:Pattern: Heavy Leather Ball}"
    for text, looked_for in [("x " * 400 + (token + " ") * 30, "heavy"), ((token + " ") * 30 + "needle " + "y " * 200,
                                                                           "needle")]:
        for size in (100, 200, 333, 600):
            cut = memory_tools.excerpt(text, [looked_for], size)
            assert len(cut) <= size and cut.startswith("…") and cut.endswith("…"), (size, cut)
            assert looked_for in cut.lower(), (size, cut)
            rest = cut.replace(token, "")
            assert "{" not in rest and "}" not in rest, (size, cut)


# Notes ---------------------------------------------------------------------------------------


LEATHER = "Doesn't want to sell their leather; keeps it for leatherworking."
FLIPPING = "Flipping Finespindle patterns: {item:18731:Pattern: Heavy Leather Ball} 18s -> ~63s."


def test_notes(monkeypatch):
    monkeypatch.setenv(memory.PLAYER_ENV, "Slim")
    assert run(remember(LEATHER)) == "Noted for Slim as #1. 1 of 30 notes, 64 of 3,000 characters."
    assert run(remember(FLIPPING)).startswith("Noted for Slim as #2.")
    assert run(remember("Wants flips that aren't scam-like.")).startswith("Noted for Slim as #3.")
    # The same again (spacing and case aside) isn't another note; it counts as the newest, as now worded.
    assert run(remember(" " + LEATHER.lower().replace(" ", "  ", 1))) == (
        "Already noted for Slim as #1; it now counts as the newest. 3 of 30 notes, 182 of 3,000 characters.")
    # An update keeps the id.
    sold = FLIPPING + " The first one sold for 62s."
    assert run(remember(sold, replaces=2)).startswith("Note #2 for Slim updated.")

    notes = run(list_notes())
    assert notes.player == "Slim" and notes.room == "3 of 30 notes, 210 of 3,000 characters"
    assert [(n.id, n.text) for n in notes.notes] == [
        (3, "Wants flips that aren't scam-like."), (1, LEATHER.lower()), (2, sold),
    ]
    assert notes.notes[0].saved == datetime.now().strftime("%Y-%m-%d")

    assert run(forget_note(3)) == "Forgot note #3 for Slim: \"Wants flips that aren't scam-like.\"."
    with pytest.raises(ToolError, match="Slim has no note #3; list_notes shows them."):
        run(forget_note(3))
    with pytest.raises(ToolError, match="Slim has no note #9"):
        run(remember("Something.", replaces=9))
    assert run(remember("Saving up for a mount.")).startswith("Noted for Slim as #4."), "ids aren't reused"
    with pytest.raises(ToolError, match="Couldn't save the note: the note is empty"):
        run(remember("   "))

    # Per character: an alt has its own, and names are matched in any case.
    assert run(list_notes(player="Brannoc")).notes == []
    run(remember("Brannoc is a bank alt.", player="Brannoc"))
    assert [n.text for n in memory.Notes().about("Brannoc")] == ["Brannoc is a bank alt."]
    assert [n.id for n in run(list_notes(player="SLIM")).notes] == [1, 2, 4]
    with pytest.raises(ToolError, match="Nobody has no note #1"):
        run(forget_note(1, player="Nobody"))
    path = memory.Notes().path
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert list(saved) == ["Slim", "Brannoc"] and saved["Slim"]["next"] == 5

    # A file that can't be read is never saved over.
    path.write_text("{broken", encoding="utf-8")
    assert run(list_notes()).notes == []
    with pytest.raises(ToolError, match="Couldn't save the note: can't read the notes in"):
        run(remember("Something new."))
    assert path.read_text(encoding="utf-8") == "{broken"


def test_notes_have_room_for_30_and_3000_characters(monkeypatch):
    monkeypatch.setenv(memory.PLAYER_ENV, "Slim")
    for i in range(1, 31):
        result = run(remember(f"Note {i}."))
    assert "make room" not in result
    result = run(remember("Note 31."))
    assert result == ("Noted for Slim as #31. 30 of 30 notes, 232 of 3,000 characters. To make room, the oldest "
                      "were removed: #1 'Note 1.'. If one still matters, save it again, shorter or merged into another "
                      "note.")
    assert [n.id for n in memory.Notes().about("Slim")] == list(range(2, 32))

    # By length: ten notes of 290 characters fit, an eleventh pushes the oldest out.
    for i in range(10):
        run(remember(f"{i} " + "x" * 288, player="Grunta"))
    assert run(list_notes(player="Grunta")).room == "10 of 30 notes, 2,900 of 3,000 characters"
    result = run(remember("y" * 150, player="Grunta"))
    assert "11 of 30" not in result and "removed: #1 '0 xxx" in result
    assert [n.id for n in memory.Notes().about("Grunta")] == list(range(2, 12))
    result = run(remember("z" * 300, player="Grunta"))
    assert "removed: #2 '1 xxx" in result and "#3" not in result
    assert sum(len(n.text) for n in memory.Notes().about("Grunta")) <= memory.MAX_NOTES_CHARS


def test_notes_come_with_every_prompt(addons):
    notes = memory.Notes()
    notes.add("Thorgrim", LEATHER, datetime(2026, 11, 10, 20, 0).timestamp())
    notes.add("Thorgrim", FLIPPING, datetime(2026, 11, 10, 21, 0).timestamp())
    prompts, players = [], []

    def agent(prompt, model, player):
        prompts.append(prompt)
        players.append(player)
        return "OK."

    b = Bridge(addons, agent, publish=quiet)
    b.answer(bridge.Question("what should I do for gold?", "Thorgrim"))
    b.answer(bridge.Question("and now?", "Thorgrim"))
    b.answer(bridge.Question("hi", "Grunta"))
    known = ("What you know about Thorgrim (your notes from earlier conversations, oldest first):\n"
             f"- #1 (2026-11-10): {LEATHER}\n- #2 (2026-11-10): {FLIPPING}")
    assert prompts[0] == known + "\n\nThorgrim asks, in game: what should I do for gold?"
    assert prompts[1].startswith(known + "\n\nEarlier exchanges with Thorgrim, oldest first.")
    assert prompts[1].endswith("A: OK.\n\nThorgrim asks, in game: and now?")
    assert prompts[2] == "Grunta asks, in game: hi", "only their own"
    assert players == ["Thorgrim", "Thorgrim", "Grunta"]


# Who's asking --------------------------------------------------------------------------------

# Stands in for Claude Code: says which player its MCP server would be told about, through the config
# on its command line, the one in its environment, and its environment itself.
WHO_AGENT = """
import json, os, sys
sys.stdin.read()
told = [json.loads(sys.argv[1]), json.loads(os.environ["WOW_MCP_MCP_CONFIG"])]
print(json.dumps([config["mcpServers"]["wow"]["env"].get("WOW_MCP_PLAYER") for config in told]
                 + [os.environ.get("WOW_MCP_PLAYER")]))
"""


def test_the_bridge_tells_the_tools_who_asks(addons, monkeypatch):
    command = [sys.executable, "-c", WHO_AGENT, "{mcp_config}"]
    agent = CommandAgent(command, addons, timeout=20)
    assert json.loads(agent("Where?", None, "Thorgrim")) == ["Thorgrim"] * 3
    assert json.loads(agent("Where?", None, "Grunta")) == ["Grunta"] * 3
    assert json.loads(agent("Where?")) == [None] * 3
    # Never anyone from the bridge's own environment.
    monkeypatch.setenv(memory.PLAYER_ENV, "Stale")
    assert json.loads(CommandAgent(command, addons, timeout=20)("Where?")) == [None] * 3

    # The tools go by it, unless they're given another character.
    monkeypatch.setenv(memory.PLAYER_ENV, "Thorgrim")
    assert run(remember(LEATHER)).startswith("Noted for Thorgrim as #1.")
    assert run(list_notes()).player == "Thorgrim"
    assert run(list_notes(player="Brannoc")).notes == []


def test_elsewhere_the_tools_ask_the_game_who_plays(live_game, addons, monkeypatch):
    with live_game():
        assert run(remember(LEATHER)).startswith("Noted for Thorgrim as #1.")
        assert [n.text for n in run(list_notes()).notes] == [LEATHER]
    assert [n.text for n in memory.Notes().about("Thorgrim")] == [LEATHER]

    # Without the game they need the name; searching doesn't.
    monkeypatch.setattr(game, "running_folders", lambda proc=None: set())
    with pytest.raises(ToolError, match="isn't running. Pass their name as `player`."):
        run(list_notes())
    assert run(forget_note(1, player="Thorgrim")).startswith("Forgot note #1 for Thorgrim")
    assert search_conversations().note == "No earlier conversations have been kept yet."


# Importing old bridge logs -------------------------------------------------------------------

LOG = """\
INFO Online auction prices: refreshed about every hour
INFO Answering /ai questions from /games/AddOns with Claude Code: sonnet, or the model the player picks (fable, opus)
INFO Slim asks: where is the closest mining trainer?
INFO answer: Geofram Bouldertoe, in Ironforge.
INFO Slim starts a fresh conversation
INFO Slim asks (fable): I want you to think outside the box regarding getting gold
INFO auction house 110: new items snapshot from 2026-11-10 23:13 UTC (174279 auctions, 4234 items)
INFO answer: Best idea is a vendor flip, Slim:

1. Pattern flip:
   - {item:18731:Pattern: Heavy Leather Ball}: costs 18s, AH 63s.

Dead ends: crafting.
INFO Slim asks (fable): how much dps?
ERROR agent failed: the agent exited with status 1: overloaded
INFO answer: Sorry, I couldn't answer that: the agent exited with status 1: overloaded
INFO Slim asks: what now?
ERROR agent failed
Traceback (most recent call last):
  File "bridge.py", line 1, in answer
KeyError: 'broken'
INFO answer: Sorry, I couldn't answer that: 'broken'
INFO Slim asks: first
INFO Slim asks: second
WARNING ignoring the model picked in game, 'gpt': not one of fable, opus, sonnet, haiku
INFO answer: First answer.
INFO answer: Second
answer.
INFO the player asks: who am I?
INFO answer: Nobody knows.
INFO Slim asks: one too late
ERROR agent failed: the agent exited with status 143
"""

GOLD = "I want you to think outside the box regarding getting gold"
FLIP_ANSWER = ("Best idea is a vendor flip, Slim:\n\n1. Pattern flip:\n   - {item:18731:Pattern: Heavy Leather Ball}: "
               "costs 18s, AH 63s.\n\nDead ends: crafting.")
# The bridge started again, writing to the same log: "one too late" above never got its answer.
RESTARTED = "INFO Answering /ai questions from /games/AddOns with claude\n"


def test_old_logs_are_imported_once(tmp_path, capsys):
    logs = tmp_path / "data" / "wow-mcp" / "bridge-logs"  # where it looks by default
    logs.mkdir(parents=True)
    log, older = logs / "b87zbn2al.output", logs / "b0wd06a12.output"
    log.write_text(LOG, encoding="utf-8")
    # Saved by Claude Code's background tasks, which add a line of their own at the end.
    older.write_text("INFO Slim asks: whats up\nINFO answer: Hey Slim!\n\n[killed]\n", encoding="utf-8")
    last = datetime(2026, 11, 10, 23, 56).timestamp()
    os.utime(log, (last, last))
    os.utime(older, (last - 3600, last - 3600))

    memory.main(["import-logs"])
    archive = memory.Archive()
    assert capsys.readouterr().out == f"Read 2 log(s): added 6 exchange(s) to {archive.path}; 0 were there already.\n"
    # Files oldest first. The logs have no times: an exchange before an auction snapshot the bridge logged gets
    # the snapshot's time, the last ones the file's; each one before those a minute earlier.
    snapshot = datetime(2026, 11, 10, 23, 13, tzinfo=timezone.utc).timestamp()
    assert [(e.player, e.model, e.question, e.answer, e.at, e.approx) for e in archive.entries()] == [
        ("Slim", None, "whats up", "Hey Slim!", last - 3600, True),
        ("Slim", None, "where is the closest mining trainer?", "Geofram Bouldertoe, in Ironforge.", snapshot, True),
        ("Slim", "fable", GOLD, FLIP_ANSWER, last - 180, True),
        ("Slim", None, "first", "First answer.", last - 120, True),
        ("Slim", None, "second", "Second\nanswer.", last - 60, True),
        (None, None, "who am I?", "Nobody knows.", last, True),
    ]
    assert json.loads(archive.path.read_text(encoding="utf-8").splitlines()[0]) == {
        "at": last - 3600, "player": "Slim", "model": None, "question": "whats up", "answer": "Hey Slim!",
        "approx": True,
    }

    # Again, by name: nothing new. A log that grew since adds only what's new, whatever its time now.
    memory.main(["import-logs", str(log), str(older)])
    assert capsys.readouterr().out.startswith("Read 2 log(s): added 0 exchange(s)")
    with open(log, "a", encoding="utf-8") as f:
        f.write(RESTARTED + "INFO Slim asks: one more\nINFO answer: Sure.\n")
    os.utime(log, (last + 600, last + 600))
    assert memory.import_logs([log, older], archive) == (1, 6)
    assert [(e.question, e.at) for e in archive.entries()][6:] == [("one more", last + 600)]
    # The logs are left as they were.
    assert log.read_text(encoding="utf-8") == LOG + RESTARTED + "INFO Slim asks: one more\nINFO answer: Sure.\n"
    assert older.stat().st_mtime == last - 3600

    # Found like any other exchange, with the time marked as estimated.
    result = search_conversations(query="Heavy Leather Ball")
    assert [(e.question, e.answer) for e in result.exchanges] == [(GOLD, FLIP_ANSWER)]
    assert "~" in result.exchanges[0].when and "about" in result.exchanges[0].when
    assert result.note == "Times with ~ are estimated: those exchanges came from a log without times."

    with pytest.raises(SystemExit):
        memory.main(["import-logs", str(tmp_path / "missing.output")])
    assert "not found" in capsys.readouterr().err


def test_logs_with_times(tmp_path):
    """The bridge's logs carry times now (LOG_FORMAT); those are taken as they are."""
    formatter = logging.Formatter(bridge.LOG_FORMAT)
    asked = datetime(2026, 11, 12, 18, 30, 5).timestamp()

    def line(message, at, level=logging.INFO):
        record = logging.LogRecord("wow_mcp.bridge", level, __file__, 1, message, None, None)
        record.created, record.msecs = at, 250
        return formatter.format(record) + "\n"

    log = tmp_path / "bridge.log"
    log.write_text(line("Slim asks (sonnet): where is Donni Anthania?", asked)
                   + line("answer: In Elwynn Forest,\nby the farmhouse.", asked + 20)
                   + line("Slim asks: and her cats?", asked + 30)
                   + line("answer: About 40s each.", asked + 45), encoding="utf-8")
    assert log.read_text().startswith("2026-11-12 18:30:05,250 INFO Slim asks (sonnet): ")
    assert memory.import_logs([log], memory.Archive()) == (2, 0)
    assert [(e.model, e.question, e.answer, e.at, e.approx) for e in memory.Archive().entries()] == [
        ("sonnet", "where is Donni Anthania?", "In Elwynn Forest,\nby the farmhouse.", asked + 20, False),
        (None, "and her cats?", "About 40s each.", asked + 45, False),
    ]


# What agents are told ------------------------------------------------------------------------


def test_agents_are_told_to_search_and_take_notes(addons):
    instructions = server.create_server().instructions
    assert memory_tools.GUIDANCE in instructions and memory_tools.NOTES_GUIDANCE in instructions
    assert "Never say you can't remember" in memory_tools.GUIDANCE
    for said in ("'remember…'", "'yesterday'", "search before you answer"):
        assert said in memory_tools.GUIDANCE, said
    for said in ("not chit-chat", "preferences", "ongoing projects", "plans and goals", "corrected you",
                 "update it", "forget it"):
        assert said in memory_tools.NOTES_GUIDANCE, said
    names = {fn.__name__ for fn, _ in app.TOOLS}
    assert {"search_conversations", "remember", "forget_note", "list_notes"} <= names
    assert "earlier conversation" in search_conversations.__doc__ and "yesterday" in search_conversations.__doc__
    assert "Not chit-chat" in remember.__doc__ and "30 notes" in remember.__doc__
    # The bridge's own prompt says so too, and its agent may use them all.
    assert "search_conversations" in bridge.SYSTEM_PROMPT and "Never say you can't remember" in bridge.SYSTEM_PROMPT
    assert "remember" in bridge.SYSTEM_PROMPT
    command = bridge.claude_command()
    assert command[command.index("--allowedTools") + 1].split(",")[0] == "mcp__wow"  # every wow tool
    assert command[command.index("--disallowedTools") + 1] == "mcp__wow__show_message"
