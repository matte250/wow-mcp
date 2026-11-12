"""/ai in game (Chat.lua) and the bridge that answers it (bridge.py). The agent is always a fake
here: a Python function, or a small Python script standing in for the agent command."""

import asyncio
import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path

import pytest

from wow_mcp import bridge, item_text, memory, stream
from wow_mcp.app import transport
from wow_mcp.bridge import (
    MAX_WAITING, SYSTEM_PROMPT, AgentError, AgentSilent, AgentTimeout, Bridge, CommandAgent, Progress, Tokens,
    chat_lines, claude_command,
)
from wow_mcp.tools import messages, player


def publish_per_contract(text, style="chat", sender="Claude", reply=False, default_model=None, default_effort=None,
                         question=None, took=None, tokens=None):
    """Stands in for tools/messages.py: the `messages` payload as docs/PLAN.md (W7) defines it."""

    def change(state, seq):
        message = {"id": seq, "text": text, "style": style, "from": sender, **({"reply": True} if reply else {}),
                   **({"defaultModel": default_model} if reply and default_model is not None else {}),
                   **({"defaultEffort": default_effort} if reply and default_effort is not None else {}),
                   **({"session": question[0], "post": question[1]} if reply and question else {}),
                   **({"took": round(took)} if reply and took is not None else {}),
                   **({"tokens": tokens} if reply and tokens is not None else {})}
        state["messages"] = [*state.get("messages", []), message][-10:]

    return transport().publish(change)


def question(post_id, text, session=1):
    return stream.Post(session, post_id, end=0, value={"question": text, "player": "Thorgrim"})


def reset(post_id, player="Thorgrim", session=1):
    return stream.Post(session, post_id, end=0, value={"reset": True, "player": player})


def earlier(*exchanges, player="Thorgrim"):
    """The part of a prompt with the earlier exchanges: (question, minutes ago, answer) each."""
    lines = [(f"Earlier exchanges with {player}, oldest first. Context only: use them if the question below "
              "follows up on them, otherwise ignore them.")]
    for q, minutes, a in exchanges:
        lines += [f"Q ({f'{minutes} min ago' if minutes else 'just now'}): {q}", f"A: {a}"]
    return "\n".join(lines) + "\n\n"


def type_in(typed, *lines):
    """Lines for the live game to type into its chat box (see GAME)."""
    typed.with_suffix(".tmp").write_text("".join(line + "\n" for line in lines))
    typed.with_suffix(".tmp").rename(typed)


def wait_for(condition, seconds=10):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def gone(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] in "ZX"  # exited
    except FileNotFoundError:
        return True


@pytest.fixture
def running():
    """Runs a bridge until the test ends."""
    started = []

    def start(b):
        thread = threading.Thread(target=b.serve, daemon=True)
        thread.start()
        started.append((b, thread))
        assert b.ready.wait(5)
        return b

    yield start
    for b, thread in started:
        b.stop.set()
        thread.join(5)


# Lua for live_game: logs the chat and the messages the game receives, and types the lines the
# test writes into TYPED. The conversation shows in the main chat window (/mcp aitab off); the AI
# tab has its own tests (test_ai_tab.py).
GAME = """
WowMCPDB = { aiTab = { mode = "off" } }
local function Log(line)
    local f = assert(io.open("LOG", "a"))
    f:write(line, "\\n")
    f:close()
end
local addMessage = DEFAULT_CHAT_FRAME.AddMessage
DEFAULT_CHAT_FRAME.AddMessage = function(self, msg)
    Log("chat: " .. msg)
    return addMessage(self, msg)
end
local loadAddon = client.LoadAddon
function client.LoadAddon()
    local ns = loadAddon()
    local receive = WowMCP_Receive
    WowMCP_Receive = function(payload)
        for _, m in ipairs(payload.messages or {}) do
            Log(string.format("message %d %s: %s", m.id, m.from, m.text))
        end
        return receive(payload)
    end
    local status = ns.AIChat.Status -- the bridge's notes, from the bell
    ns.AIChat.Status = function(m)
        Log(string.format("status %s %s: %s", m.status, tostring(m.question), tostring(m.step)))
        return status(m)
    end
    return ns
end
C_Timer.NewTicker(0.5, function()
    local f = io.open("TYPED")
    if not f then return end
    local lines = f:read("a")
    f:close()
    os.remove("TYPED")
    for line in lines:gmatch("[^\\n]+") do client.Type(line) end
end)
"""


def test_ai_in_game_end_to_end(live_game, addons, tmp_path, monkeypatch, running):
    """The player types /ai and /ask; the bridge hears both, the agent answers them in turn
    (using the wow tools meanwhile), and the answers reach the game as messages."""
    log, typed = tmp_path / "game.log", tmp_path / "typed.txt"
    monkeypatch.setattr(messages, "publish_message", publish_per_contract, raising=False)
    prompts, busy = [], []

    def agent(prompt, model, asker, effort, progress):
        busy.append(prompt)
        assert len(busy) == 1, "one question at a time"
        prompts.append(prompt)
        if "what now" in prompt:
            answer = "Go and see Gornek.\n\n- He's in the den.\n- Then come back."
        else:
            location = asyncio.run(player.get_player_location())
            answer = f"**You're in {location.zone}**, at {location.x:.0f}, {location.y:.0f}."
        busy.remove(prompt)
        return answer

    with live_game(GAME.replace("LOG", log.as_posix()).replace("TYPED", typed.as_posix()), seconds=25):
        running(Bridge(addons, agent))
        type_in(typed, "/ai where am I?", "/ask what now?")
        wait_for(lambda: log.exists() and "Then come back." in log.read_text(), seconds=15)

    # The second question comes with the first exchange, for context.
    assert prompts == [
        "Thorgrim asks, in game: where am I?",
        earlier(("where am I?", 0, "**You're in Durotar**, at 50, 50.")) + "Thorgrim asks, in game: what now?",
    ]
    lines = log.read_text(encoding="utf-8").splitlines()  # the game writes UTF-8 (the "…"), whatever the locale
    assert "chat: |cff33ff99WowMCP|r Asking… where am I?" in lines
    assert "chat: |cff33ff99WowMCP|r Asking… what now?" in lines
    received = [line.split(" ", 2)[2] for line in dict.fromkeys(lines) if line.startswith("message ")]
    assert received == ["Claude: You're in Durotar, at 50, 50.", "Claude: Go and see Gornek."]
    # Each question is acknowledged as it arrives, by the post it came in (over the bell, no slot): a note for the AI
    # tab's status line, never a chat line (and no warning that the bridge is down).
    acks = [line.split(":")[0].split()[2] for line in lines if line.startswith("status ack ")]
    assert acks == ["1", "2"]
    assert [line for line in dict.fromkeys(lines) if line.startswith("chat: ") and "Claude" in line] == [
        "chat: |cff33ff99[Claude]|r You're in Durotar, at 50, 50.", "chat: |cff33ff99[Claude]|r Go and see Gornek."]
    assert not any("wow-mcp-bridge" in line for line in lines)
    # One message per answer (one live-update slot), naming its question; the game prints it line by line.
    state = json.loads((addons / "WowMCP" / "server-state.json").read_text())
    assert [(m["text"], m["style"], m["reply"], m["post"]) for m in state["messages"] if not m.get("status")] == [
        ("You're in Durotar, at 50, 50.", "chat", True, 1),
        ("Go and see Gornek.\n- He's in the den.\n- Then come back.", "chat", True, 2),
    ]


def test_new_conversation_in_game_end_to_end(live_game, addons, tmp_path, monkeypatch, running):
    """/ai new in game clears the conversation; /ai new quests near me? is just a follow-up."""
    log, typed = tmp_path / "game.log", tmp_path / "typed.txt"
    monkeypatch.setattr(messages, "publish_message", publish_per_contract, raising=False)
    prompts = []

    def agent(prompt, model, asker, effort, progress):
        prompts.append(prompt)
        return f"Answer {len(prompts)}."

    def answered(n):
        return lambda: log.exists() and f"Claude: Answer {n}." in log.read_text()

    with live_game(GAME.replace("LOG", log.as_posix()).replace("TYPED", typed.as_posix()), seconds=30):
        running(Bridge(addons, agent))
        type_in(typed, "/ai where am I?")
        wait_for(answered(1), seconds=15)
        type_in(typed, "/ai new quests near me?")
        wait_for(answered(2), seconds=15)
        type_in(typed, "/ai new", "/ai what now?")
        wait_for(answered(3), seconds=15)

    assert prompts == [
        "Thorgrim asks, in game: where am I?",
        earlier(("where am I?", 0, "Answer 1.")) + "Thorgrim asks, in game: new quests near me?",
        "Thorgrim asks, in game: what now?",
    ]
    assert "chat: |cff33ff99WowMCP|r Starting a fresh conversation." in log.read_text().splitlines()
    # The reset itself sends nothing back, unlike a question (its ack).
    state = json.loads((addons / "WowMCP" / "server-state.json").read_text())
    assert [m["text"] for m in state["messages"]] == ["Answer 1.", "Answer 2.", "Answer 3."]
    acks = [line.split(":")[0].split()[2] for line in log.read_text().splitlines() if line.startswith("status ack ")]
    assert acks == ["1", "2", "4"]
    saved = json.loads((addons / "WowMCP" / "bridge-history.json").read_text())
    assert [(e["question"], e["answer"]) for e in saved["Thorgrim"]] == [("what now?", "Answer 3.")]


def test_follow_ups_see_the_conversation(addons):
    now = [1_000_000.0]
    prompts = []

    def agent(prompt, model, asker, effort, progress):
        prompts.append(prompt)
        return f"Answer {len(prompts)}:\n\n- **Karolek**   is in The Drag."

    history = bridge.History(addons / "WowMCP" / "bridge-history.json", clock=lambda: now[0])
    b = Bridge(addons, agent, publish=lambda text, style, sender, reply, **_: None, history=history)
    b.answer(bridge.Question("where can I learn leatherworking?", "Thorgrim"))
    now[0] += 150
    b.answer(bridge.Question("and skinning?", "Thorgrim"))
    b.answer(bridge.Question("where am I?", "Grunta"))  # another player has their own conversation
    b.answer(bridge.Question("thanks!", "Thorgrim"))
    assert prompts == [
        "Thorgrim asks, in game: where can I learn leatherworking?",
        earlier(("where can I learn leatherworking?", 2, "Answer 1: - **Karolek** is in The Drag."))
        + "Thorgrim asks, in game: and skinning?",
        "Grunta asks, in game: where am I?",
        earlier(
            ("where can I learn leatherworking?", 2, "Answer 1: - **Karolek** is in The Drag."),
            ("and skinning?", 0, "Answer 2: - **Karolek** is in The Drag."),
        ) + "Thorgrim asks, in game: thanks!",
    ]
    assert "earlier exchanges" in SYSTEM_PROMPT and "otherwise ignore them" in SYSTEM_PROMPT


def test_history_keeps_only_the_recent_part(tmp_path):
    start = 1_000_000.0
    now = [start]
    path = tmp_path / "bridge-history.json"
    history = bridge.History(path, clock=lambda: now[0])
    for i in range(12):  # one a minute
        history.add("Thorgrim", f"q{i}", f"a{i}")
        now[0] += 60
    # The last 8 (the default), oldest first; the file keeps no more than that.
    assert [e.question for e in history.recent("Thorgrim")] == [f"q{i}" for i in range(4, 12)]
    assert len(json.loads(path.read_text())["Thorgrim"]) == 8
    # Only those from the last 60 minutes (the default).
    now[0] = start + 7 * 60 + 3600 + 1  # q7 was answered just over an hour ago
    assert [e.question for e in history.recent("Thorgrim")] == ["q8", "q9", "q10", "q11"]
    now[0] = start + 12 * 60 + 3600
    assert history.recent("Thorgrim") == []
    # Saving drops what's too old to use, for every player.
    history.add("Grunta", "q", "a")
    assert json.loads(path.read_text()) == {"Grunta": [{"question": "q", "answer": "a", "at": now[0]}]}

    # Long answers are kept on one line, cut short.
    history.add("Grunta", "q2", "Line one.\n\n" + "word " * 1000)
    kept = history.recent("Grunta")[-1].answer
    assert len(kept) == bridge.MAX_KEPT_ANSWER and kept.startswith("Line one. word word") and kept.endswith(" word…")

    # Other sizes and windows; 0 turns it off.
    small = bridge.History(None, size=2, minutes=5, clock=lambda: now[0])
    for q in ("q1", "q2", "q3"):
        small.add(None, q, "a")
    assert [e.question for e in small.recent(None)] == ["q2", "q3"]
    now[0] += 5 * 60 + 1
    assert small.recent(None) == []
    off = bridge.History(tmp_path / "off.json", size=0)
    off.add("Thorgrim", "q", "a")
    assert off.recent("Thorgrim") == [] and not (tmp_path / "off.json").exists()


def test_failed_answers_are_not_remembered(addons):
    prompts, said = [], []
    outcomes = [AgentError("not logged in"), AgentTimeout("after the 30-minute limit"), KeyError("broken"), "",
                "It's in Orgrimmar.", "Go west."]

    def agent(prompt, model, asker, effort, progress):
        prompts.append(prompt)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    b = Bridge(addons, agent, publish=lambda text, style, sender, reply, **_: said.append(text))
    for q in ("q1", "q2", "q3", "q4", "where is it?", "and then?"):
        b.answer(bridge.Question(q, "Thorgrim"))
    assert said[3] == "I have no answer to that."
    assert prompts[:5] == [f"Thorgrim asks, in game: {q}" for q in ("q1", "q2", "q3", "q4", "where is it?")]
    assert prompts[5] == earlier(("where is it?", 0, "It's in Orgrimmar.")) + "Thorgrim asks, in game: and then?"

    # Nor is an answer the player never sees because the bridge is stopping.
    b.agent = lambda prompt, model, asker, effort, progress: "Too late."
    b.stop.set()
    b.answer(bridge.Question("one more?", "Thorgrim"))
    assert [e.answer for e in b.history.recent("Thorgrim")] == ["It's in Orgrimmar.", "Go west."]


def test_history_survives_a_restart(addons):
    prompts = []

    def agent(prompt, model, asker, effort, progress):
        prompts.append(prompt)
        return "Karolek, in The Drag."

    quiet = lambda text, style, sender, reply, **_: None  # noqa: E731
    Bridge(addons, agent, publish=quiet).answer(bridge.Question("where's the leatherworking trainer?", "Thorgrim"))
    Bridge(addons, agent, publish=quiet).answer(bridge.Question("and skinning?", "Thorgrim"))
    assert prompts[1] == (
        earlier(("where's the leatherworking trainer?", 0, "Karolek, in The Drag.")) + "Thorgrim asks, in game: and skinning?"
    )

    # A damaged file means starting afresh, not failing.
    path = addons / "WowMCP" / "bridge-history.json"
    for damaged in ("{not json", '{"Thorgrim": [{"question": "q"}]}', "[1, 2]"):
        path.write_text(damaged)
        assert Bridge(addons, agent, publish=quiet).history.recent("Thorgrim") == []


def test_new_conversation(addons, running):
    """A reset waits its turn behind the questions asked before it."""
    prompts, started, release = [], threading.Event(), threading.Event()

    def agent(prompt, model, asker, effort, progress):
        prompts.append(prompt)
        started.set()
        release.wait(5)
        return "An answer."

    said = []
    b = running(Bridge(addons, agent, publish=lambda text, style, sender, reply, **_: said.append(text)))
    b.history.add("Grunta", "where am I?", "Durotar.")
    b.accept(question(1, "where am I?"))
    assert started.wait(5)
    b.accept(question(2, "what now?"))
    b.accept(reset(3))
    b.accept(question(4, "where's the bank?"))
    release.set()
    wait_for(lambda: len(said) == 3)
    assert prompts == [
        "Thorgrim asks, in game: where am I?",
        earlier(("where am I?", 0, "An answer.")) + "Thorgrim asks, in game: what now?",
        "Thorgrim asks, in game: where's the bank?",
    ]
    assert [e.question for e in b.history.recent("Thorgrim")] == ["where's the bank?"]
    assert [e.question for e in b.history.recent("Grunta")] == ["where am I?"]  # only their own

    # With the queue full, it still happens, right away.
    idle = Bridge(addons, agent, history=bridge.History(None))  # not serving: questions just wait
    idle.history.add("Thorgrim", "q", "a")
    for i in range(MAX_WAITING):
        idle.accept(question(i, f"q{i}"))
    idle.accept(reset(99))
    assert idle.history.recent("Thorgrim") == []


def test_chat_command(run_client):
    # In the main chat window (/mcp aitab off), as before the AI tab; test_ai_tab.py has the tab.
    run_client("""
        WowMCPDB = { aiTab = { mode = "off" } }
        local ns = client.LoadAddon()
        local posted = {}
        ns.Stream.Post = function(value) posted[#posted + 1] = value end
        local function Said(text)
            for _, line in ipairs(client.chat) do
                if line:find(text, 1, true) then return true end
            end
        end
        local function Count(text)
            local n = 0
            for _, line in ipairs(client.chat) do
                if line:find(text, 1, true) then n = n + 1 end
            end
            return n
        end

        client.Type("/ai")
        assert(Said("/ai <question>") and Said("/ai new - start a fresh conversation") and #posted == 0, "usage")

        -- Links become text the agent can read; the echo keeps them clickable.
        local link = "|cff1eff00|Hitem:2589::::::::60:::::::::|h[Linen Cloth]|h|r"
        client.Type("/ask who buys " .. link .. "?  ")
        assert(posted[1].question == "who buys [Linen Cloth] (item 2589)?" and posted[1].player == "Thorgrim")
        assert(Said("Asking… who buys " .. link .. "?"))

        -- Nothing at all from the bridge for 15 seconds: it may not be running. Said once.
        client.Wait(28)
        assert(not Said("wow-mcp-bridge"))
        client.Wait(4)
        assert(Said("No answer from wow-mcp-bridge. Is it running?"))
        client.Wait(100)
        assert(Count("wow-mcp-bridge") == 1 and not Said("Thinking"))

        -- Any update in the meantime (the bridge's notes, an answer, markers) means it is. Then nothing for longer
        -- than the bridge may stay quiet while it works (120 s, and a margin): it seems to have stopped.
        wipe(client.chat)
        client.Type("/ai where am I?")
        ns.run.seq = ns.run.seq + 1
        client.Wait(200)
        assert(not Said("wow-mcp-bridge") and not Said("stopped"))
        client.Wait(170)
        assert(Said("The AI seems to have stopped responding."))

        -- Answers (here, from a bridge that doesn't say which question): nothing is due any more.
        ns.AIChat.Answer("First.", "chat", "Claude")
        ns.AIChat.Answer("Second.", "chat", "Claude")
        assert(Said("[Claude]|r Second."))

        -- Without live updates, the answer can't show until /reload.
        ns.run.liveProblem = "game sound is turned off"
        client.Type("/ai one more")
        assert(#posted == 3 and Said("only show after /reload"))

        -- Long questions are cut at a character boundary.
        client.Type("/ai " .. string.rep("é", 300))
        assert(posted[4].question == string.rep("é", 250))

        -- /ai new, in any case, starts a fresh conversation: a reset post, confirmed right away.
        -- Nothing comes back for it, so no hint about the bridge and no warning about live updates.
        wipe(client.chat)
        client.Type("/ai new")
        assert(posted[5].reset == true and posted[5].player == "Thorgrim" and posted[5].question == nil)
        assert(Said("Starting a fresh conversation.") and not Said("Asking") and not Said("/reload"))
        ns.run.liveProblem = nil
        client.Type("/ask  NeW ")
        assert(#posted == 6 and posted[6].reset == true)
        client.Wait(400)
        assert(not Said("wow-mcp-bridge") and not Said("stopped"))

        -- A question that only starts with "new" is a question.
        client.Type("/ai new quests near me?")
        assert(posted[7].question == "new quests near me?" and posted[7].reset == nil)
        assert(Said("Asking… new quests near me?"))
    """)


def test_questions_wait_their_turn(addons, running):
    said, busy, started, release = [], [], threading.Event(), threading.Event()

    def agent(prompt, model, asker, effort, progress):
        busy.append(prompt)
        assert len(busy) == 1, "one question at a time"
        started.set()
        release.wait(5)
        busy.remove(prompt)
        return "Answer: " + prompt.rsplit(": ", 1)[1]

    b = running(Bridge(addons, agent, publish=lambda text, style, sender, reply, **_: said.append(text)))
    b.accept(question(1, "q1"))
    assert started.wait(5)
    for i in range(2, MAX_WAITING + 3):  # one too many
        b.accept(question(i, f"q{i}"))
    assert said == ["I'm still busy with your earlier questions. Please ask again in a minute."]
    release.set()
    wait_for(lambda: len(said) == MAX_WAITING + 2)
    assert said[1:] == [f"Answer: q{i}" for i in range(1, MAX_WAITING + 2)]


def test_problems_are_reported_in_game(addons, tmp_path):
    said = []

    def agent(prompt, model, asker, effort, progress):
        raise AgentError("the agent exited with status 1: not logged in")

    b = Bridge(addons, agent, publish=lambda text, style, sender, reply, **_: said.append((sender, text)))
    b.answer(bridge.Question("where am I?"))
    b.accept(stream.Post(1, 1, end=0, error="post 1 arrived damaged"))
    b.accept(stream.Post(1, 2, end=0, value={"not": "a question"}))
    b.agent = lambda prompt, model, asker, effort, progress: {}["broken"]
    b.answer(bridge.Question("where am I?"))
    assert said == [
        ("Claude", "Sorry, I couldn't answer that: the agent exited with status 1: not logged in"),
        ("Claude", "Your question arrived garbled. Please ask again."),
        ("Claude", "Sorry, I couldn't answer that: 'broken'"),
    ]

    # A stuck agent is stopped at the time limit, together with what it started (e.g. its MCP servers). This
    # one doesn't stream (--agent), so the time limit is all it has.
    child = tmp_path / "child.pid"
    stuck = (
        "import subprocess, time\n"
        f"open({str(child)!r}, 'w').write(str(subprocess.Popen(['sleep', '60']).pid))\n"
        "time.sleep(60)\n"
    )
    b.agent = CommandAgent([sys.executable, "-c", stuck], addons, silence=0.1, max_time=1)
    started = time.monotonic()
    b.answer(bridge.Question("where am I?"))
    assert 1 <= time.monotonic() - started < 8
    assert said[-1] == ("Claude", "Sorry, I stopped after the 1-second limit. Try asking something narrower.")
    wait_for(lambda: gone(int(child.read_text())), seconds=5)


FAKE_AGENT = """
import json, os, sys
prompt = sys.stdin.read()
config = json.loads(sys.argv[2])["mcpServers"]["wow"]
assert config["args"] == ["-m", "wow_mcp.server"] and config["command"] == sys.executable
assert os.environ["WOW_MCP_SYSTEM_PROMPT"] == sys.argv[1]
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": prompt + " in " + config["env"]["WOW_MCP_ADDONS_DIR"] + ", " + sys.argv[1][:30]}))
"""


def test_agent_command(addons):
    agent = CommandAgent([sys.executable, "-c", FAKE_AGENT, "{system_prompt}", "{mcp_config}"], addons, max_time=20)
    assert agent("Where?") == f"Where? in {addons}, {SYSTEM_PROMPT[:30]}"

    def run(code):
        return CommandAgent([sys.executable, "-c", code], addons, max_time=20)("Where?")

    assert run("print('  just text  ')") == "just text"
    with pytest.raises(AgentError, match="status 1: not logged in"):
        run("import sys; print('partial'); sys.exit('not logged in')")
    with pytest.raises(AgentError, match="^API Error: overloaded$"):
        run("import json; print(json.dumps({'type': 'result', 'is_error': True, 'result': 'API Error: overloaded'}))")
    with pytest.raises(AgentError, match="couldn't start"):
        CommandAgent(["/nonexistent/agent"], addons)("Where?")


def test_default_agent_is_claude_code_with_the_wow_tools(addons):
    """Checked, never run."""
    command = CommandAgent(claude_command("sonnet"), addons).command
    option = lambda name: command[command.index(name) + 1]  # noqa: E731
    assert command[:2] == ["claude", "-p"]
    assert json.loads(option("--mcp-config")) == bridge.mcp_config(addons)
    assert "--strict-mcp-config" in command
    assert option("--allowedTools") == "mcp__wow,WebSearch,WebFetch"
    assert option("--disallowedTools") == "mcp__wow__show_message"
    assert option("--append-system-prompt") == SYSTEM_PROMPT
    # Every event as it happens, down to each piece of thinking, so it's watched while it works.
    assert option("--output-format") == "stream-json" and {"--verbose", "--include-partial-messages"} <= set(command)
    agent = CommandAgent(claude_command(), addons, silence=90, max_time=600)
    assert agent.streams and agent.quiet == 90
    assert option("--model") == "sonnet"
    assert "--model" not in claude_command()
    assert "--effort" not in command and claude_command("sonnet", "high")[-4:] == ["--model", "sonnet", "--effort", "high"]
    assert claude_command(effort="max")[-2:] == ["--effort", "max"] and "--model" not in claude_command(effort="max")
    assert isinstance(AgentTimeout("x"), AgentError) and isinstance(AgentSilent("x"), AgentTimeout)


def test_models_picked_in_game_are_checked(caplog):
    """Only the models in MODELS get through; anything else the game sends is dropped, with a log line."""
    post = {"question": "where am I?", "player": "Thorgrim"}
    assert bridge.Question.from_post({**post, "model": "sonnet"}) == bridge.Question("where am I?", "Thorgrim", "sonnet")
    assert bridge.Question.from_post(post).model is None
    for bad in ("gpt-5", "Sonnet", "sonnet --dangerously-skip-permissions", "--dangerously-skip-permissions", "", 5,
                ["sonnet"], {"sonnet": True}, True):
        caplog.clear()
        question = bridge.Question.from_post({**post, "model": bad})
        assert question == bridge.Question("where am I?", "Thorgrim"), bad
        assert "ignoring the model picked in game" in caplog.text, bad
    assert set(bridge.MODELS) == {"fable", "opus", "sonnet", "haiku"}


def test_answers_come_from_the_picked_model(addons, caplog):
    caplog.set_level(logging.INFO, logger="wow_mcp.bridge")
    asked, said = [], []

    def agent(prompt, model, asker, effort, progress):
        asked.append((prompt.rsplit(": ", 1)[1], model))
        return f"Answer {len(asked)}."

    def publish(text, style, sender, reply, default_model, default_effort, **_):
        said.append((sender, text, default_model))

    # Claude Code started with --model opus: a pick goes to the agent and signs the answer; without one,
    # the agent uses its own, which signs it. Every reply tells the game what that is.
    b = Bridge(addons, agent, publish=publish, history=bridge.History(None), models=True, model="opus")
    b.answer(bridge.Question("where am I?", "Thorgrim", "sonnet"))
    b.answer(bridge.Question("and now?", "Thorgrim"))
    b.answer(bridge.Question("quick one", "Thorgrim", "haiku"))
    assert asked == [("where am I?", "sonnet"), ("and now?", None), ("quick one", "haiku")]
    assert said == [("Claude Sonnet", "Answer 1.", "opus"), ("Claude Opus", "Answer 2.", "opus"),
                    ("Claude Haiku", "Answer 3.", "opus")]
    # The conversation carries over from one model to the next.
    assert [(e.question, e.answer) for e in b.history.recent("Thorgrim")] == [
        ("where am I?", "Answer 1."), ("and now?", "Answer 2."), ("quick one", "Answer 3."),
    ]

    # Started without --model (or with a full model name): only picks are named, and the game isn't told one.
    for own in (None, "claude-sonnet-5"):
        said.clear()
        b = Bridge(addons, agent, publish=publish, history=bridge.History(None), models=True, model=own)
        b.answer(bridge.Question("q", "Thorgrim"))
        b.answer(bridge.Question("q", "Thorgrim", "fable"))
        assert said == [("Claude", "Answer 4." if own is None else "Answer 6.", ""),
                        ("Claude Fable", "Answer 5." if own is None else "Answer 7.", "")]

    # Another agent (--agent) answers with its own model, whatever was picked.
    said.clear()
    b = Bridge(addons, agent, publish=publish, history=bridge.History(None))
    b.answer(bridge.Question("q", "Thorgrim", "sonnet"))
    assert asked[-1] == ("q", None) and said == [("Claude", "Answer 8.", "")]
    assert "ignoring the model picked in game (sonnet): --agent answers with its own" in caplog.text


# Stands in for Claude Code: answers with the arguments it was run with.
ARGS_AGENT = """
import json, sys
sys.stdin.read()
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(sys.argv[1:])}))
"""


def test_claude_code_runs_the_picked_model(addons, tmp_path):
    """--model on the command line is the picked model, else the bridge's own; never anything else from the game."""
    command = [sys.executable, "-c", ARGS_AGENT, *claude_command("opus")[1:]]
    agent = CommandAgent(command, addons, max_time=20)

    def models(*args):
        argv = json.loads(agent("Where?", *args))
        return [argv[i + 1] for i, arg in enumerate(argv) if arg == "--model"]

    assert models() == ["opus"] and models(None) == ["opus"]
    assert models("haiku") == ["haiku"] and models("fable") == ["fable"]
    assert models() == ["opus"]  # a pick lasts one question
    assert CommandAgent([sys.executable, "-c", ARGS_AGENT, *claude_command()[1:]], addons)("Where?", "sonnet")
    with pytest.raises(ValueError, match="not a model the player can pick"):
        agent("Where?", "sonnet --dangerously-skip-permissions")
    assert bridge.with_model(["claude", "-p"], "sonnet") == ["claude", "-p", "--model", "sonnet"]

    # From a post to the command line: an unknown model is dropped on the way, so the bridge's own runs.
    ran = []
    b = Bridge(addons, lambda prompt, model, asker, effort, _: ran.append(json.loads(agent(prompt, model))) or "ok",
               publish=lambda text, style, sender, reply, **_: None, history=bridge.History(None),
               models=True, model="opus")
    for model in ("haiku", "--dangerously-skip-permissions", "rm -rf ~"):
        b.accept(stream.Post(1, len(ran) + 1, end=0, value={"question": "q", "player": "Thorgrim", "model": model}))
        b.answer(b.questions.get_nowait())
    assert [argv[argv.index("--model") + 1] for argv in ran] == ["haiku", "opus", "opus"]
    assert not any("dangerously" in arg or "rm -rf" in arg for argv in ran for arg in argv)


def test_model_picked_in_game_end_to_end(live_game, addons, tmp_path, monkeypatch, running):
    """/ai model sonnet in game: the next questions carry it, the bridge answers with it and the answer
    names it; /ai model default goes back to the bridge's own, which the game learns from the answers."""
    log, typed = tmp_path / "game.log", tmp_path / "typed.txt"
    monkeypatch.setattr(messages, "publish_message", publish_per_contract, raising=False)
    asked = []

    def agent(prompt, model, asker, effort, progress):
        asked.append((prompt, model))
        return f"Answer {len(asked)}."

    def seen(text):
        return lambda: log.exists() and text in log.read_text(encoding="utf-8")

    with live_game(GAME.replace("LOG", log.as_posix()).replace("TYPED", typed.as_posix()), seconds=30):
        running(Bridge(addons, agent, models=True, model="opus"))
        type_in(typed, "/ai model Sonnet", "/ai where am I?")
        wait_for(seen("Answer 1."), seconds=15)
        type_in(typed, "/ai model default", "/ai and now?")
        wait_for(seen("Answer 2."), seconds=15)
        type_in(typed, "/ai model")
        wait_for(seen("Answers come from Default (Opus)."), seconds=5)

    assert [model for _, model in asked] == ["sonnet", None]
    assert asked[1][0] == earlier(("where am I?", 0, "Answer 1.")) + "Thorgrim asks, in game: and now?"
    lines = log.read_text(encoding="utf-8").splitlines()
    assert "chat: |cff33ff99WowMCP|r Answers now come from Sonnet." in lines
    assert "chat: |cff33ff99[Claude Sonnet]|r Answer 1." in lines and "chat: |cff33ff99[Claude Opus]|r Answer 2." in lines
    # The first answer told the game what the bridge's own model is.
    assert "chat: |cff33ff99WowMCP|r Answers now come from the model the bridge was started with (Opus)." in lines


def test_thinking_levels_picked_in_game_are_checked(caplog):
    """Only the levels in EFFORTS get through; anything else the game sends is dropped, with a log line."""
    post = {"question": "where am I?", "player": "Thorgrim"}
    assert bridge.Question.from_post({**post, "effort": "xhigh"}) == bridge.Question("where am I?", "Thorgrim",
                                                                                     effort="xhigh")
    assert bridge.Question.from_post({**post, "model": "haiku", "effort": "low"}) == bridge.Question(
        "where am I?", "Thorgrim", "haiku", "low")
    assert bridge.Question.from_post(post).effort is None
    for bad in ("default", "High", "ultra", "high --dangerously-skip-permissions", "--dangerously-skip-permissions",
                "high; rm -rf ~", "", 3, ["high"], {"high": True}, True):
        caplog.clear()
        question = bridge.Question.from_post({**post, "model": "sonnet", "effort": bad})
        assert question == bridge.Question("where am I?", "Thorgrim", "sonnet"), bad  # the model still counts
        assert "ignoring the thinking level picked in game" in caplog.text, bad
    assert bridge.EFFORTS == ("low", "medium", "high", "xhigh", "max")


def test_answers_think_as_picked(addons, caplog):
    caplog.set_level(logging.INFO, logger="wow_mcp.bridge")
    asked, said = [], []

    def agent(prompt, model, asker, effort, progress):
        asked.append((prompt.rsplit(": ", 1)[1], model, effort))
        return f"Answer {len(asked)}."

    def publish(text, style, sender, reply, default_model, default_effort, **_):
        said.append((sender, text, default_effort))

    # Claude Code started with --effort high: a pick goes to the agent and signs the answer, after the
    # model; without one, the agent thinks at its own level, which signs it. Every reply tells the game it.
    b = Bridge(addons, agent, publish=publish, history=bridge.History(None), models=True, model="opus", effort="high")
    b.answer(bridge.Question("where am I?", "Thorgrim", "sonnet", "max"))
    b.answer(bridge.Question("and now?", "Thorgrim"))
    b.answer(bridge.Question("quick one", "Thorgrim", effort="low"))
    assert asked == [("where am I?", "sonnet", "max"), ("and now?", None, None), ("quick one", None, "low")]
    assert said == [("Claude Sonnet · max", "Answer 1.", "high"), ("Claude Opus · high", "Answer 2.", "high"),
                    ("Claude Opus · low", "Answer 3.", "high")]
    # The conversation carries over whatever the level, and the archive keeps it as before.
    assert [e.answer for e in b.history.recent("Thorgrim")] == ["Answer 1.", "Answer 2.", "Answer 3."]
    assert [(e.question, e.model) for e in b.archive.entries()] == [
        ("where am I?", "sonnet"), ("and now?", "opus"), ("quick one", "opus"),
    ]

    # Started without --effort: Claude Code's own level isn't named, and the game isn't told one.
    said.clear()
    b = Bridge(addons, agent, publish=publish, history=bridge.History(None), models=True)
    b.answer(bridge.Question("q", "Thorgrim"))
    b.answer(bridge.Question("q", "Thorgrim", "haiku", "xhigh"))
    b.answer(bridge.Question("q", "Thorgrim", effort="medium"))
    assert said == [("Claude", "Answer 4.", ""), ("Claude Haiku · xhigh", "Answer 5.", ""),
                    ("Claude · medium", "Answer 6.", "")]

    # Another agent (--agent) thinks its own way, whatever was picked.
    said.clear()
    b = Bridge(addons, agent, publish=publish, history=bridge.History(None))
    b.answer(bridge.Question("q", "Thorgrim", "sonnet", "max"))
    assert asked[-1] == ("q", None, None) and said == [("Claude", "Answer 7.", "")]
    assert "ignoring the thinking level picked in game (max): --agent thinks its own way" in caplog.text


def test_claude_code_thinks_at_the_picked_level(addons, caplog):
    """--effort on the command line is the picked level, else the bridge's own (none: Claude Code's); never
    anything else from the game."""
    command = [sys.executable, "-c", ARGS_AGENT, *claude_command("opus", "high")[1:]]
    agent = CommandAgent(command, addons, max_time=20)

    def efforts(*args, **kwargs):
        argv = json.loads(agent("Where?", *args, **kwargs))
        return [argv[i + 1] for i, arg in enumerate(argv) if arg == "--effort"]

    assert efforts() == ["high"] and efforts(effort=None) == ["high"]
    assert efforts(effort="max") == ["max"] and efforts("haiku", "Thorgrim", "low") == ["low"]
    assert efforts() == ["high"]  # a pick lasts one question
    own = CommandAgent([sys.executable, "-c", ARGS_AGENT, *claude_command()[1:]], addons, max_time=20)
    assert "--effort" not in json.loads(own("Where?"))
    assert json.loads(own("Where?", effort="xhigh"))[-2:] == ["--effort", "xhigh"]
    with pytest.raises(ValueError, match="not a thinking level the player can pick"):
        agent("Where?", effort="high --dangerously-skip-permissions")
    with pytest.raises(ValueError, match="not a thinking level the player can pick"):
        bridge.with_effort(["claude", "-p"], "default")
    assert bridge.with_effort(["claude", "-p", "--effort", "low"], "max") == ["claude", "-p", "--effort", "max"]

    # From a post to the command line: an unknown level is dropped on the way, so the bridge's own runs.
    caplog.set_level(logging.INFO, logger="wow_mcp.bridge")
    ran = []
    b = Bridge(addons, lambda prompt, model, asker, effort, _: ran.append(json.loads(agent(prompt, model, asker, effort)))
               or "ok", publish=lambda text, style, sender, reply, **_: None, history=bridge.History(None),
               models=True, model="opus", effort="high")
    for effort in ("max", "--dangerously-skip-permissions", "high; rm -rf ~", "default"):
        post = {"question": "q", "player": "Thorgrim", "model": "sonnet", "effort": effort}
        b.accept(stream.Post(1, len(ran) + 1, end=0, value=post))
        b.answer(b.questions.get_nowait())
    assert [argv[argv.index("--effort") + 1] for argv in ran] == ["max", "high", "high", "high"]
    assert all(argv.count("--effort") == 1 and argv[argv.index("--model") + 1] == "sonnet" for argv in ran)
    assert not any("dangerously" in arg or "rm -rf" in arg for argv in ran for arg in argv)
    # The question's log line keeps its wording, so old logs can still be imported (memory.read_log); the
    # level goes on a line of its own.
    assert "thinking picked in game: max" in caplog.messages
    logged = "\n".join(f"{r.levelname} {r.getMessage()}" for r in caplog.records)
    assert [(e["question"], e["model"], e["answer"]) for e in memory.read_log(logged, time.time())] == [
        ("q", "sonnet", "ok")] * 4


def test_bridge_effort_option(addons, monkeypatch, capsys):
    """--effort takes Claude Code's levels, and only for Claude Code."""
    for args, error in ((["--effort", "turbo"], "argument --effort: invalid choice: 'turbo'"),
                        (["--agent", "my-agent", "--effort", "high"], "--effort only applies to the default agent")):
        monkeypatch.setattr(sys, "argv", ["wow-mcp-bridge", "--addons-dir", str(addons), *args])
        with pytest.raises(SystemExit):
            bridge.main()
        assert error in capsys.readouterr().err


def test_thinking_picked_in_game_end_to_end(live_game, addons, tmp_path, monkeypatch, running):
    """/ai thinking high in game: the next questions carry it, the bridge passes it on and the answer says
    so; /ai effort default goes back to the bridge's own level, which the game learns from the answers."""
    log, typed = tmp_path / "game.log", tmp_path / "typed.txt"
    monkeypatch.setattr(messages, "publish_message", publish_per_contract, raising=False)
    asked = []

    def agent(prompt, model, asker, effort, progress):
        asked.append((prompt, model, effort))
        return f"Answer {len(asked)}."

    def seen(text):
        return lambda: log.exists() and text in log.read_text(encoding="utf-8")

    with live_game(GAME.replace("LOG", log.as_posix()).replace("TYPED", typed.as_posix()), seconds=30):
        running(Bridge(addons, agent, models=True, model="opus", effort="medium"))
        type_in(typed, "/ai thinking High", "/ai model sonnet", "/ai where am I?")
        wait_for(seen("Answer 1."), seconds=15)
        type_in(typed, "/ai effort default", "/ai and now?")
        wait_for(seen("Answer 2."), seconds=15)
        type_in(typed, "/ai thinking")
        wait_for(seen("The AI thinks: Default (Medium)."), seconds=5)

    assert [(model, effort) for _, model, effort in asked] == [("sonnet", "high"), ("sonnet", None)]
    assert asked[1][0] == earlier(("where am I?", 0, "Answer 1.")) + "Thorgrim asks, in game: and now?"
    lines = log.read_text(encoding="utf-8").splitlines()
    assert "chat: |cff33ff99WowMCP|r The AI now thinks: High." in lines
    assert "chat: |cff33ff99[Claude Sonnet · high]|r Answer 1." in lines
    assert "chat: |cff33ff99[Claude Sonnet · medium]|r Answer 2." in lines
    # The first answer told the game what the bridge's own level is.
    assert "chat: |cff33ff99WowMCP|r The AI now thinks at the level the bridge was started with (Medium)." in lines


def test_progress_is_live_but_calm():
    """Each update is a frame of the bell (no slot, but it holds the bell about half a second): a step goes out once it
    has lasted a second, at most every 1.5 s, and only when it changed; while the agent keeps busy without news, it
    goes again unchanged after `quiet` seconds, so the game knows the bridge is there."""
    now, sent = [0.0], []
    progress = Progress(lambda step: sent.append((now[0], step)), quiet=120, clock=lambda: now[0])

    def busy(seconds, step=None, tool=None):
        """The agent at work: it says it's on `step` (calling `tool`), then a sign of life every half second."""
        progress.saw(step, tool)
        for _ in range(round(seconds * 2)):
            now[0] += 0.5
            progress.saw()

    busy(10, "checking where you are", "get_player_location")
    busy(0.5, "checking auction prices", "get_auction_prices")  # too short to be told
    busy(20, "searching the web", "WebSearch")
    for _ in range(300):  # then silent: kept up once, `quiet` after the last update, as it was busy since that
        now[0] += 0.5
        progress.tick()
    busy(0.5, None, "WebSearch")
    busy(2, bridge.WRITING)
    assert sent == [(1.5, "checking where you are"), (11.5, "searching the web"), (131.5, "searching the web"),
                    (182.0, bridge.WRITING)]
    busy(150)  # busy, but nothing new to say: kept up after `quiet`
    assert sent[4:] == [(302.0, bridge.WRITING)]
    assert progress.used() == "get_player_location, get_auction_prices, WebSearch ×2"
    assert Progress(lambda step: None).used() == "none"

    # Steps, by Claude Code's stream-json events (with --include-partial-messages): content as it starts.
    def started(kind, **block):
        return {"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
                                                  "content_block": {"type": kind, **block}}}

    assert bridge.event_step(started("thinking", thinking="")) == (bridge.THINKING, None)
    assert bridge.event_step(started("text", text="")) == (bridge.WRITING, None)
    assert bridge.event_step(started("tool_use", name="mcp__wow__get_auction_prices")) == (
        "checking auction prices", "get_auction_prices")
    assert bridge.event_step(started("tool_use", name="mcp__wow__get_item_details"))[0] == "looking up items"
    assert bridge.event_step(started("tool_use", name="WebSearch")) == ("searching the web", "WebSearch")
    assert bridge.event_step(started("tool_use", name="mcp__wow__list_reminders"))[0] == "looking at your game"
    assert bridge.event_step(started("tool_use", name="mcp__wow__set_reminder"))[0] == "updating your game"
    assert bridge.event_step(started("tool_use", name="Task"))[0] == "using its tools"
    for other in ({"type": "system", "subtype": "thinking_tokens"}, {"type": "stream_event", "event": {
            "type": "content_block_delta", "delta": {"type": "thinking_delta"}}}, {"type": "result"}, None, [], "x"):
        assert bridge.event_step(other) == (None, None)


# Stands in for Claude Code streaming its events (stream-json): prints each of argv[1]'s [delay, output] after its
# delay (output: an event, a raw line, {"stderr": line}, or an exit status). Its pid goes into argv[4].
STREAMING_AGENT = """
import json, os, sys, time
sys.stdin.read()
open(sys.argv[4], "w").write(str(os.getpid()))
for delay, out in json.load(open(sys.argv[1], encoding="utf-8")):
    time.sleep(delay)
    if isinstance(out, int):
        sys.exit(out)
    if isinstance(out, dict) and "stderr" in out:
        print(out["stderr"], file=sys.stderr, flush=True)
    else:
        print(out if isinstance(out, str) else json.dumps(out), flush=True)
"""
INIT = {"type": "system", "subtype": "init", "tools": ["WebSearch"]}
THOUGHT = {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                             "delta": {"type": "thinking_delta", "estimated_tokens": 50}}}


def started(kind, **block):
    return {"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
                                              "content_block": {"type": kind, **block}}}


def result(text, error=False):
    return {"type": "result", "subtype": "error_during_execution" if error else "success", "is_error": error,
            "result": text}


@pytest.fixture
def streaming(addons, tmp_path):
    """A CommandAgent running STREAMING_AGENT on a schedule; its pid file is .pid on the agent."""
    made = []

    def make(schedule, **limits):
        pid = tmp_path / f"agent{len(made)}.pid"
        # The schedule goes in a file: a long one doesn't fit on a Windows command line (32,767 characters).
        events = tmp_path / f"agent{len(made)}.json"
        events.write_text(json.dumps(schedule), encoding="utf-8")
        agent = CommandAgent([sys.executable, "-c", STREAMING_AGENT, str(events), "--output-format",
                              "stream-json", str(pid)], addons, **limits)
        agent.pid = pid
        made.append(agent)
        return agent

    return make


def test_a_busy_agent_takes_as_long_as_it_needs(streaming, monkeypatch):
    """Claude Code is watched by its events, not the clock: a run several times longer than the silence it's
    allowed ends with its answer, as long as events keep coming. Each tells the game's status line what it's doing."""
    monkeypatch.setattr(bridge, "SETTLE", 0)
    thinking = [[0.1, THOUGHT]] * 8
    agent = streaming([[0.05, INIT], [0.1, started("thinking", thinking="")], *thinking,
                       [0.1, started("tool_use", name="mcp__wow__get_auction_prices", input={})],
                       [0.3, {"type": "user", "message": {"content": [{"type": "tool_result"}]}}],
                       [0.1, "a line that isn't an event"], [0.1, {"stderr": "a warning"}],
                       [0.1, started("thinking", thinking="")], *thinking, [0.1, started("text", text="")],
                       [0.05, result("Buy {item:2589:Linen Cloth}.")]], silence=0.8, max_time=30)
    assert agent.streams and agent.quiet == 0.8
    sent = []
    progress = Progress(sent.append, every=0)
    began = time.monotonic()
    assert agent("Any flips?", progress=progress) == "Buy {item:2589:Linen Cloth}."
    assert time.monotonic() - began > 3 * agent.silence
    assert sent == [bridge.THINKING, "checking auction prices", bridge.THINKING, bridge.WRITING]
    assert progress.used() == "get_auction_prices"


def test_a_silent_agent_is_stopped(streaming, addons):
    """No event at all for --timeout seconds: the agent is stopped, and the answer says why, without telling the
    player to ask something narrower (it wasn't the length)."""
    agent = streaming([[0.05, INIT], [0.1, started("thinking", thinking="")], [0.1, THOUGHT], [30, THOUGHT]],
                      silence=0.5, max_time=30)
    began = time.monotonic()
    with pytest.raises(AgentSilent, match="^no response from the model for 0.5 seconds$"):
        agent("Any flips?")
    assert time.monotonic() - began < 5
    wait_for(lambda: gone(int(agent.pid.read_text())), seconds=5)

    said = []
    b = Bridge(addons, streaming([[0.05, INIT], [30, THOUGHT]], silence=0.3),
               publish=lambda text, style, sender, reply, **_: said.append(text), notify=lambda *a, **k: 0)
    b.answer(bridge.Question("Any flips?", "Thorgrim"))
    assert said == ["Sorry, I stopped: no response from the model for 0.3 seconds."]
    assert bridge._span(120) == "2 minutes" and bridge._span(60) == "1 minute" and bridge._span(90) == "90 seconds"


def test_a_runaway_agent_is_stopped_at_the_time_limit(streaming, addons):
    """However busy, an answer stops at --max-time; then asking something narrower may help."""
    agent = streaming([[0.05, INIT], *[[0.05, THOUGHT]] * 600], silence=0.5, max_time=1)
    began = time.monotonic()
    with pytest.raises(AgentTimeout, match="^after the 1-second limit$") as stopped:
        agent("Any flips?")
    assert not isinstance(stopped.value, AgentSilent) and time.monotonic() - began < 5
    wait_for(lambda: gone(int(agent.pid.read_text())), seconds=5)
    assert bridge._span(30 * 60, before_noun=True) == "30-minute"

    said = []
    b = Bridge(addons, streaming([[0.05, INIT], *[[0.05, THOUGHT]] * 600], max_time=0.5),
               publish=lambda text, style, sender, reply, **_: said.append(text), notify=lambda *a, **k: 0)
    b.answer(bridge.Question("Any flips?", "Thorgrim"))
    assert said == ["Sorry, I stopped after the 0.5-second limit. Try asking something narrower."]


def test_streaming_agent_results_and_errors(streaming):
    """The answer is the stream's result event; errors come as an error result, or as an exit status with what it
    said on stderr. A run without an answer is an empty one (the bridge then says it has none)."""
    assert streaming([[0, INIT], [0, result("first")], [0, result("Final.")]])("q") == "Final."
    with pytest.raises(AgentError, match="^API Error: overloaded$"):
        streaming([[0, INIT], [0, result("API Error: overloaded", error=True)]])("q")
    with pytest.raises(AgentError, match="^error_max_turns$"):
        streaming([[0, {"type": "result", "subtype": "error_max_turns", "is_error": True}]])("q")
    with pytest.raises(AgentError, match="^the agent exited with status 3: not logged in$"):
        streaming([[0, INIT], [0, {"stderr": "starting"}], [0, {"stderr": "not logged in"}], [0, 3]])("q")
    assert streaming([[0, INIT], [0, result("")]])("q") == ""
    assert streaming([[0, INIT]])("q") == ""


def test_other_agents_only_have_the_time_limit(addons):
    """An agent of the player's own (--agent) prints only its answer, so silence says nothing about it: it has the
    whole --max-time, and the game is told to expect no news for that long."""
    agent = CommandAgent([sys.executable, "-c", "import time; time.sleep(1); print('Done.')"], addons, silence=0.2,
                         max_time=20)
    assert not agent.streams and agent.quiet == 20
    assert agent("q") == "Done."


def test_the_game_hears_how_its_question_comes_along(addons, caplog):
    """An ack as soon as a question arrives, then the steps as the agent says (within the slot budget), then the
    answer: each about the post the question came in. The log keeps its wording for memory.read_log, with a line
    on how long each answer took and the tools it used."""
    caplog.set_level(logging.INFO, logger="wow_mcp.bridge")
    now, notes, said = [0.0], [], []

    def agent(prompt, model, asker, effort, progress):
        if "stuck" in prompt:
            raise AgentSilent("no response from the model for 2 minutes")
        for step, tool, seconds in (("checking auction prices", "get_auction_prices", 50),
                                    (bridge.THINKING, None, 200)):
            progress.saw(step, tool)
            for _ in range(seconds):
                now[0] += 1
                progress.saw()
        return "Buy low, sell high."

    def publish(text, style, sender, reply, question=None, **_):
        said.append((text, question))

    b = Bridge(addons, agent, publish=publish, history=bridge.History(None), clock=lambda: now[0],
               notify=lambda status, text, question, quiet: notes.append((status, text, question, quiet)))
    b.accept(stream.Post(5, 7, end=0, value={"question": "any flips?", "player": "Thorgrim"}))
    assert notes == [("ack", "", (5, 7), bridge.SILENCE)]
    b.answer(b.questions.get_nowait())
    assert [(status, text) for status, text, question, _ in notes[1:] if question == (5, 7)] == [
        ("progress", "checking auction prices"), ("progress", bridge.THINKING), ("progress", bridge.THINKING)]
    assert said == [("Buy low, sell high.", (5, 7))]
    b.accept(stream.Post(5, 8, end=0, value={"question": "stuck?", "player": "Thorgrim"}))
    b.answer(b.questions.get_nowait())
    assert said[-1] == ("Sorry, I stopped: no response from the model for 2 minutes.", (5, 8))

    assert "took 4:10; tools used: get_auction_prices" in caplog.messages
    assert "took 0:00; tools used: none" in caplog.messages
    logged = "\n".join(f"2026-11-11 13:27:01,360 {r.levelname} {r.getMessage()}" for r in caplog.records)
    assert [(e["question"], e["answer"]) for e in memory.read_log(logged, time.time())] == [
        ("any flips?", "Buy low, sell high.")]

    # A garbled post and a full queue are answered by post too; resets aren't acknowledged (nothing comes back).
    notes.clear()
    b.accept(stream.Post(5, 9, end=0, error="post 9 arrived damaged"))
    b.accept(stream.Post(5, 10, end=0, value={"reset": True, "player": "Thorgrim"}))
    assert said[-1] == ("Your question arrived garbled. Please ask again.", (5, 9)) and notes == []
    for i in range(MAX_WAITING):
        b.accept(stream.Post(5, 11 + i, end=0, value={"question": f"q{i}", "player": "Thorgrim"}))
    assert said[-1] == ("I'm still busy with your earlier questions. Please ask again in a minute.", (5, 15))
    assert [question for _, _, question, _ in notes] == [(5, 11), (5, 12), (5, 13), (5, 14)]


# Claude Code's stream-json events as a live run has them (2.1.322, --include-partial-messages): every part of a message
# names the message (api_message_id).
def part(message, inner):
    return {"type": "stream_event", "event": inner, "parent_tool_use_id": None, "api_message_id": message}


def message_start(message, output_tokens=1):  # its usage so far: not what it will write
    return part(message, {"type": "message_start", "message": {
        "id": message, "type": "message", "role": "assistant", "content": [],
        "usage": {"input_tokens": 2, "output_tokens": output_tokens}}})


def block(message, index, kind, **content):
    return part(message, {"type": "content_block_start", "index": index, "content_block": {"type": kind, **content}})


def delta(message, index, kind, **fields):
    return part(message, {"type": "content_block_delta", "index": index, "delta": {"type": kind, **fields}})


def thought(message, tokens):
    """A piece of thinking: its text left out, an estimate of its tokens instead."""
    return delta(message, 0, "thinking_delta", thinking="", estimated_tokens=tokens)


def message_end(message, output_tokens, thinking=0):  # what the message wrote, exactly
    return part(message, {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {
        "input_tokens": 2, "output_tokens": output_tokens, "output_tokens_details": {"thinking_tokens": thinking}}})


def finished(text, output_tokens, thinking=0):
    return {**result(text), "num_turns": 2,
            "usage": {"input_tokens": 4, "output_tokens": output_tokens,
                      "output_tokens_details": {"thinking_tokens": thinking}}}


# A live run (Sonnet at max): it thinks, writes, calls a tool, then answers in a second message. Its counts and
# estimates as they came; the text is shorter. Each thinking estimate comes twice, the second time as a system event.
M1, M2 = "msg_011CgwXhmoCJGfkbZ9WmgfwS", "msg_011CgwXiFz63vxnS2xfY32Z8"
RUN = [
    message_start(M1, 5),
    block(M1, 0, "thinking", thinking="", signature=""),
    {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 50, "estimated_tokens_delta": 50},
    thought(M1, 50),
    {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 200, "estimated_tokens_delta": 150},
    thought(M1, 150),
    {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 500, "estimated_tokens_delta": 300},
    thought(M1, 300),
    thought(M1, None),
    delta(M1, 0, "signature_delta", signature="CAQSswwKEAgTGAI4AUII"),
    {"type": "assistant", "message": {"id": M1, "content": [{"type": "thinking"}], "usage": {"output_tokens": 5}}},
    block(M1, 1, "text", text=""),
    delta(M1, 1, "text_delta", text="Since 7, 9 and 11 are pairwise coprime, just one n lies below 693, and working it "
                                    "out gives 346."),  # 96 characters: 24 tokens
    block(M1, 2, "tool_use", id="toolu_01FrFU2511nq49Ts6bitM7Ep", name="Bash", input={}),
    delta(M1, 2, "input_json_delta", partial_json=""),
    delta(M1, 2, "input_json_delta",
          partial_json='{"command": "echo 346", "description": "Print the computed answer."}'),  # 68: 17 tokens
    message_end(M1, 1059, thinking=628),
    {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "346"}]}},
    message_start(M2, 53),
    block(M2, 0, "text", text=""),
    delta(M2, 0, "text_delta", text="The smallest such positive n is 346."),  # 9 tokens
    message_end(M2, 53),
    finished("The smallest such positive n is 346.", 1112, thinking=628),
]


def test_tokens_are_counted_over_the_whole_run():
    """The tokens the agent writes, over all its turns: while a message streams, its thinking by the estimates that
    come with it and its text and tool calls by their length; once it's done, its exact count; at the end, the result's,
    which is what the messages' exact counts add up to. Nothing counts twice."""
    tokens = Tokens()
    totals = []
    for event in RUN:
        tokens.see(event)
        if not totals or totals[-1] != tokens.total:
            totals.append(tokens.total)
    # The thinking (not 100, 400, 1000: the system events only repeat it), the text, the tool call, the first message's
    # exact count, the second message, its exact count, and the result's, which is the same.
    assert totals == [0, 50, 200, 500, 524, 541, 1059, 1068, 1112]
    assert tokens.final == 1112 == 1059 + 53

    # An older Claude Code that doesn't name each part's message, and thinking it shows: by its length.
    tokens = Tokens()
    for event in (message_start("a"), {**message_end("a", 7), "api_message_id": None},
                  message_start("b"), {**delta("b", 0, "thinking_delta", thinking="x" * 40), "api_message_id": None}):
        tokens.see(event)
    assert tokens.total == 17 and tokens.final is None
    for nothing in (None, [], "x", {"type": "result"}, {"type": "stream_event", "event": "x"}):
        tokens.see(nothing)
    assert tokens.total == 17


def test_updates_carry_the_token_count():
    """With Claude Code's events, every update to the game also carries the tokens written so far, and goes when that
    count changed too: every 1.5 s at most while the agent writes (the game runs the count on between them), none
    while a tool runs, the keepalive after `quiet`. A step too new to show waits; the count doesn't."""
    now, sent = [0.0], []
    progress = Progress(lambda step, **counts: sent.append((now[0], step, counts)), quiet=120, clock=lambda: now[0],
                        tokens=Tokens())

    def second(*events):
        """A second of the agent's run, as CommandAgent watches it: its events, then a look."""
        now[0] += 1
        for event in events:
            progress.read(event)
        progress.tick()

    second(message_start(M1), block(M1, 0, "thinking", thinking=""))
    for _ in range(5):  # thinking, 40 tokens a second
        second(thought(M1, 40))
    second(block(M1, 1, "tool_use", name="mcp__wow__get_auction_prices", input={}), message_end(M1, 300))
    for _ in range(130):  # the tool runs; Claude Code says it's still there
        second({"type": "system", "subtype": "status"})
    second(message_start(M2), block(M2, 0, "text", text=""), delta(M2, 0, "text_delta", text="x" * 40),
           message_end(M2, 12), finished("x", 312))
    second()
    second()
    assert sent == [(2, bridge.THINKING, {"tokens": 40}), (4, bridge.THINKING, {"tokens": 120}),
                    (6, bridge.THINKING, {"tokens": 200}), (8, "checking auction prices", {"tokens": 300}),
                    (128, "checking auction prices", {"tokens": 300}), (138, "checking auction prices", {"tokens": 310}),
                    (140, bridge.WRITING, {"tokens": 312})]
    assert progress.tokens.total == 312  # the result's


def test_the_game_hears_how_many_tokens_were_written(addons, caplog):
    """Claude Code's tokens: every update carries the count so far (they go every 1.5 s at most, here every 2 s, as
    the agent is looked at every second), and the answer has the exact count, with how long it took. An agent of the
    player's own (--agent) streams nothing to count: none of that, but its answer says how long it took too."""
    caplog.set_level(logging.INFO, logger="wow_mcp.bridge")
    now, notes, said = [0.0], [], []

    def agent(prompt, model, asker, effort, progress):
        for event in (message_start(M1), block(M1, 0, "thinking", thinking="")):
            progress.read(event)
        for _ in range(60):
            now[0] += 1
            progress.read(thought(M1, 25))
            progress.tick()
        for event in (block(M1, 1, "text", text=""), delta(M1, 1, "text_delta", text="Buy low, sell high."),
                      message_end(M1, 1650), finished("Buy low, sell high.", 1650)):
            progress.read(event)
        return "Buy low, sell high."

    def publish(text, style, sender, reply, question=None, default_model=None, default_effort=None, **extra):
        said.append((text, question, extra))

    for counting in (True, False):
        b = Bridge(addons, agent, publish=publish, history=bridge.History(None), clock=lambda: now[0], tokens=counting,
                   notify=lambda status, text, question, quiet, **counts: notes.append((status, text, counts)))
        b.accept(stream.Post(5, 7, end=0, value={"question": "any flips?", "player": "Thorgrim"}))
        b.answer(b.questions.get_nowait())
    assert notes == [("ack", "", {}), *(("progress", bridge.THINKING, {"tokens": 50 * k}) for k in range(1, 31)),
                     ("ack", "", {}), ("progress", bridge.THINKING, {})]
    assert said == [("Buy low, sell high.", (5, 7), {"took": 60, "tokens": 1650}),
                    ("Buy low, sell high.", (5, 7), {"took": 60})]
    assert "took 1:00; tools used: none; tokens written: 1650" in caplog.messages
    assert "took 1:00; tools used: none" in caplog.messages

    # Other replies aren't answers: no time, no count.
    said.clear()
    b.accept(stream.Post(5, 8, end=0, error="post 8 arrived damaged"))
    assert said == [("Your question arrived garbled. Please ask again.", (5, 8), {})]


def test_claude_codes_count_reaches_the_answer(streaming, addons):
    """As the bridge runs Claude Code: its events read as they come, and the answer carries the result's count."""
    said = []
    agent = streaming([[0.01, event] for event in [INIT, *RUN]])
    b = Bridge(addons, agent, notify=lambda *a, **k: 0, tokens=agent.streams,
               publish=lambda text, style, sender, reply, **extra: said.append((text, extra.get("tokens"))))
    b.answer(bridge.Question("Puzzle?", "Thorgrim"))
    assert said == [("The smallest such positive n is 346.", 1112)]


def test_only_streaming_agents_have_their_tokens_counted(addons, monkeypatch):
    """Claude Code, as the bridge runs it, streams what it writes; an agent of the player's own (--agent) only prints
    its answer, so the game is told of no tokens for it."""
    made = []

    class Started:
        def __init__(self, *args, **kwargs):
            made.append(kwargs)
            self.stop = threading.Event()

        def serve(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(bridge, "Bridge", Started)
    monkeypatch.setattr(bridge.shutil, "which", lambda program: program)  # Claude Code needn't be installed
    monkeypatch.setattr(bridge.signal, "signal", lambda *_: None)
    for agent in ([], ["--agent", f"{sys.executable} -c pass"]):
        monkeypatch.setattr(sys, "argv", ["wow-mcp-bridge", "--addons-dir", str(addons), *agent])
        bridge.main()
    assert [kwargs["tokens"] for kwargs in made] == [True, False]


def test_bridge_time_limit_options(addons, monkeypatch, capsys):
    for args in (["--timeout", "0"], ["--max-time", "-5"]):
        monkeypatch.setattr(sys, "argv", ["wow-mcp-bridge", "--addons-dir", str(addons), *args])
        with pytest.raises(SystemExit):
            bridge.main()
        assert "--timeout and --max-time must be more than 0" in capsys.readouterr().err


def test_chat_lines():
    markdown = (
        "## Trainer\n\U0001f5fa\U0000fe0f **Karolek** is in *The Drag*, see [Wowhead](https://wowhead.com/npc=3365).\n\n"
        "```\ncode\n```\n* Bring `5 silver`\n\n| Who | Where |\n|---|---|\n| Karolek | Orgrimmar |\n---\n"
    )
    assert chat_lines(markdown) == [
        "Trainer", "Karolek is in The Drag, see Wowhead.", "code", "- Bring 5 silver", "Who, Where", "Karolek, Orgrimmar",
    ]
    assert chat_lines("snake_case and 2 * 3 * 4 stay") == ["snake_case and 2 * 3 * 4 stay"]

    # Sentences of a paragraph share lines up to the width; longer ones are split between words.
    paragraph = "One. Two two. " + " ".join(["word"] * 60) + "."
    assert chat_lines(paragraph, width=100) == [
        "One. Two two.", " ".join(["word"] * 20), " ".join(["word"] * 20), " ".join(["word"] * 19) + " word.",
    ]

    # Too many lines: packed together, then cut short.
    items = "\n".join(f"- Item {i}." for i in range(30))
    lines = chat_lines(items, width=50, limit=3)
    assert len(lines) == 3 and all(len(line) <= 50 for line in lines)
    assert lines[0] == "- Item 0. - Item 1. - Item 2. - Item 3. - Item 4."
    assert lines[-1].endswith(" …") and "Item 29" not in lines[-1]
    assert chat_lines("  \n") == []


def test_chat_lines_keep_items_whole():
    # Items have spaces, full stops and hyphens inside; at the smallest width, some are longer than a line.
    answer = ("Take out {item:2318:Light Leather} (37) and {item:2319:Medium Leather}. Mr. Pinchy wants "
              "{item:5:Mr. Pinchy's Wish} back! **Then** sell {item:2934} and {item:4232:Medium Hide - large}.")
    items = item_text.ITEM.findall(answer)
    assert len(items) == 5
    for width in (20, 30, 45, 80, 250):
        lines = chat_lines(answer, width=width, limit=20)
        assert item_text.ITEM.findall("\n".join(lines)) == items, (width, lines)
        # Lines keep to the width, except a word with an item that's longer.
        assert all(len(line) <= width or (" " not in item_text.ITEM.sub("", line) and len(max(items, key=len)) > width)
                   for line in lines), (width, lines)
    assert chat_lines(answer) == [
        "Take out {item:2318:Light Leather} (37) and {item:2319:Medium Leather}. Mr. Pinchy wants "
        "{item:5:Mr. Pinchy's Wish} back! Then sell {item:2934} and {item:4232:Medium Hide - large}."
    ]
    # Cut short: an item that doesn't fit is left out rather than cut.
    for width in range(20, 60):
        for line in chat_lines(answer, width=width, limit=2):
            assert "{item:" not in item_text.ITEM.sub("", line) and "" not in line, (width, line)
    # The agent is told to write them.
    assert "{item:<id>}" in SYSTEM_PROMPT and "{item:<id>:<name>}" in SYSTEM_PROMPT


def test_only_one_bridge_per_game(tmp_path):
    from wow_mcp import bridge

    first = bridge.claim(tmp_path)
    with pytest.raises(SystemExit, match=rf"already answering for this game \(pid {os.getpid()}\)"):
        bridge.claim(tmp_path)
    first.close()
    bridge.claim(tmp_path).close()  # free again once the first one stops


def test_agent_command_line_is_split_the_platforms_way():
    words = bridge.split_command(r'"C:\Program Files\Agent\agent.exe" --flag "a b" --dir=C:\tmp "{system_prompt}"',
                                 windows=True)
    assert words == [r"C:\Program Files\Agent\agent.exe", "--flag", "a b", r"--dir=C:\tmp", "{system_prompt}"]
    assert bridge.split_command("agent 'one word' ''", windows=True) == ["agent", "one word", ""]
    # Elsewhere: the shell's rules, as ever.
    assert bridge.split_command(r'/opt/my\ agent/run --flag "a b" "{system_prompt}"', windows=False) == [
        "/opt/my agent/run", "--flag", "a b", "{system_prompt}"]


def test_agent_runs_as_given_except_on_windows(addons, monkeypatch):
    """Linux: the command exactly as asked for, and its output read the way the system reads text."""
    started = []

    def popen(command, **options):
        started.append((command, options))
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(bridge, "WINDOWS", False)
    monkeypatch.setattr(bridge.shutil, "which", lambda name: None if name == "missing" else f"/usr/local/bin/{name}")
    assert bridge.resolve_command(["agent", "--fast"]) == ["agent", "--fast"]
    assert bridge.resolve_command(["missing"]) is None and bridge.resolve_command([]) is None
    agent = CommandAgent(["agent", "--fast"], addons)
    monkeypatch.setattr(bridge.subprocess, "Popen", popen)
    with pytest.raises(AgentError, match="couldn't start agent"):
        agent("Where?")
    command, options = started[0]
    assert command == ["agent", "--fast"]
    assert options["text"] is True and options["start_new_session"] is True
    assert not {"encoding", "errors", "creationflags"} & set(options)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launchers")
def test_windows_launchers(addons, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    (tmp_path / "native.cmd").write_text('@ECHO off\n"%dp0%\\node_modules\\x\\bin\\native.exe" %*\n')
    program = tmp_path / "node_modules" / "x" / "bin" / "native.exe"
    program.parent.mkdir(parents=True)
    program.touch()
    (tmp_path / "script.cmd").write_text('@ECHO off\n"%_prog%" "%dp0%\\node_modules\\x\\cli.js" %*\n')

    # A launcher that only starts a program runs as that program, whatever its arguments.
    command = bridge.resolve_command(["native", "--append-system-prompt", "{system_prompt}"])
    assert command == [str(program), "--append-system-prompt", "{system_prompt}"]
    assert bridge.launcher_problem(CommandAgent(command, addons).command) is None

    # Any other launcher runs through cmd.exe, which the system prompt and the MCP config don't survive.
    command = bridge.resolve_command(["script", "--append-system-prompt", "{system_prompt}"])
    assert Path(command[0]).name.lower() == "script.cmd"
    problem = bridge.launcher_problem(CommandAgent(command, addons).command)
    assert f"{command[0]} is a batch launcher" in problem
    assert "WOW_MCP_SYSTEM_PROMPT and WOW_MCP_MCP_CONFIG" in problem and "{system_prompt}" in problem
    assert bridge.launcher_problem(CommandAgent([command[0], "--mcp-config", "{mcp_config}"], addons).command)
    # Without them it's fine: the agent finds both in its environment.
    assert bridge.launcher_problem(CommandAgent([command[0], "--fast", "a b"], addons).command) is None
    assert bridge.launcher_problem([str(program), "line\nbreak", '"quoted"']) is None, "a program takes anything"
    assert bridge.launcher_problem([command[0], "line\nbreak"], windows=False) is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launchers")
def test_bridge_refuses_a_launcher_that_would_garble_the_prompt(addons, tmp_path, monkeypatch):
    (tmp_path / "wrapper.cmd").write_text("@ECHO off\npython %~dp0agent.py %*\n")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["wow-mcp-bridge", "--addons-dir", str(addons),
                                      "--agent", 'wrapper --system "{system_prompt}"'])
    with pytest.raises(SystemExit) as stopped:
        bridge.main()
    assert "wrapper.cmd is a batch launcher" in str(stopped.value).lower()
    assert "WOW_MCP_SYSTEM_PROMPT" in str(stopped.value)


# Named visuals: the Refresh button, and refreshes on their own ----------------------------------

TRACKER = "Call get_my_auctions and show my active listings with undercut status"
TEXT = [{"type": "text", "text": "x"}]


def keep(name="Auction tracker", refresh=TRACKER, every=None, title="My auctions"):
    """A named visual in the server's state, as the AI makes it (tools/visuals.py)."""
    from wow_mcp.tools import visuals

    visuals.visualize(title, TEXT, name=name, refresh=refresh, refresh_minutes=every)


def refresh_post(post_id, name="auction tracker", **picks):
    return stream.Post(77, post_id, end=0, value={"refresh": name, "player": "Thorgrim", **picks})


def logged(caplog):
    return "\n".join(f"{r.levelname} {r.getMessage()}" for r in caplog.records)


def test_a_refresh_click_runs_the_visuals_instructions(addons, caplog):
    """The Refresh button is answered like a question, with the player's picks and a reply in the AI tab, by the
    visual's own instructions; it isn't part of the conversation."""
    caplog.set_level(logging.INFO, logger="wow_mcp.bridge")
    keep()
    asked, said, notes = [], [], []

    def agent(prompt, model, asker, effort, progress):
        asked.append((prompt, model, asker, effort))
        return "3 of your 8 auctions are undercut."

    b = Bridge(addons, agent, publish=lambda text, **kw: said.append((text, kw["sender"], kw["question"])),
               notify=lambda status, text="", **kw: notes.append((status, kw["question"])),
               history=bridge.History(None), models=True, model="opus")
    b.accept(refresh_post(1, model="sonnet", effort="high"))
    assert notes == [("ack", (77, 1))], "acked as it arrives, like a question"
    item = b.questions.get_nowait()
    assert isinstance(item, bridge.Refresh) and item.post == (77, 1) and not item.auto
    b.refresh(item)
    assert asked == [(
        "Thorgrim clicked Refresh on a visual in game. Update the visual named 'Auction tracker': "
        f"{TRACKER}. Use the visualize tool with name='Auction tracker'. Then reply in one short sentence: what "
        "changed, or what stands out.",
        "sonnet", "Thorgrim", "high",
    )]
    assert said == [("3 of your 8 auctions are undercut.", "Claude Sonnet · high", (77, 1))]
    # Not part of the conversation or the archive.
    assert b.history.recent("Thorgrim") == [] and b.archive.entries() == []
    # Lines of its own in the log, which the memory importer passes over.
    assert "Thorgrim refreshes the visual 'auction tracker' (sonnet)" in caplog.text
    assert "refresh result: 3 of your 8 auctions are undercut." in caplog.text
    assert " asks" not in caplog.text and "answer:" not in caplog.text
    assert memory.read_log(logged(caplog), time.time()) == []

    # Another agent (--agent) refreshes its own way, whatever was picked.
    other = Bridge(addons, agent, publish=lambda text, **kw: None, notify=lambda *a, **kw: None)
    other.accept(refresh_post(2, model="haiku", effort="low"))
    other.refresh(other.questions.get_nowait())
    assert asked[-1][1:] == (None, "Thorgrim", None)


def test_refreshes_keep_out_of_the_questions_log_lines(addons, caplog):
    """memory.read_log pairs each "asks" line with the next "answer:" one, and a failure's error with the question
    it waits on: refreshes, done or failed, between them don't get in the way."""
    caplog.set_level(logging.INFO, logger="wow_mcp.bridge")
    keep()
    outcomes = [AgentError("not logged in"), "Orgrimmar, by the bank.", "2 undercut.", "Go west."]

    def agent(prompt, model, asker, effort, progress):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    b = Bridge(addons, agent, publish=lambda text, **kw: None, notify=lambda *a, **kw: None,
               history=bridge.History(None))
    # Each time a question is logged as it arrives, before the refresh ahead of it is done.
    for n in (1, 3):
        b.accept(refresh_post(n))
        b.accept(question(n + 1, "where am I?" if n == 1 else "and then?"))
        while not b.questions.empty():
            item = b.questions.get_nowait()
            b.refresh(item) if isinstance(item, bridge.Refresh) else b.answer(item)
    assert "refresh of the visual 'Auction tracker' failed: not logged in" in caplog.text
    assert "refresh result: 2 undercut." in caplog.text
    assert [(e["question"], e["answer"]) for e in memory.read_log(logged(caplog), time.time())] == [
        ("where am I?", "Orgrimmar, by the bank."), ("and then?", "Go west."),
    ]


def test_refreshes_that_cant_be_done(addons):
    from wow_mcp.tools import visuals

    keep("Flip list", refresh=None)
    said = []
    b = Bridge(addons, lambda *args: pytest.fail("no agent"), notify=lambda *a, **kw: None,
               publish=lambda text, **kw: said.append((text, kw["question"])), history=bridge.History(None))
    for n, name in enumerate(["Nope", "flip list"], 1):
        b.accept(refresh_post(n, name))
        b.refresh(b.questions.get_nowait())
    assert said == [
        ("I can't refresh that: there's no visual named 'Nope' any more.", (77, 1)),
        ("I can't refresh that: 'Flip list' has no refresh instructions: ask me to rebuild it, and say how.", (77, 2)),
    ]
    # One refresh per visual at a time: the click meanwhile is told it's on its way, by its new version.
    keep()
    b.accept(refresh_post(3))
    b.accept(refresh_post(4, "Auction Tracker"))
    assert b.questions.qsize() == 1
    b.questions.get_nowait()
    b._refreshing.clear()
    # The player's questions come first: with the queue full, the click is told to try again.
    for n in range(MAX_WAITING):
        b.questions.put_nowait(bridge.Question(f"q{n}", "Thorgrim"))
    b.accept(refresh_post(5))
    assert said[-1] == ("I'm still busy with your earlier questions. Please click Refresh again in a minute.", (77, 5))
    assert visuals.find_named(transport().load_state(), "Auction tracker")["title"] == "My auctions"


def test_visuals_refresh_on_their_own_while_the_game_runs(addons, monkeypatch, caplog):
    """Named visuals with refresh_minutes are refreshed once they're due, only while the game runs, with the
    player's last picks, quietly: no ack, progress or reply; their new versions don't come to the front."""
    from wow_mcp import game
    from wow_mcp.tools import visuals

    caplog.set_level(logging.INFO, logger="wow_mcp.bridge")
    keep(every=30)
    keep("Flip list", refresh="Call get_auction_prices for the flip candidates and show margins", every=60)
    keep("Crafting", refresh="Call get_known_recipes and show what's worth crafting")  # only when clicked
    now = time.time()
    asked, said, notes = [], [], []
    mark = addons / "WowMCP" / visuals.BACKGROUND_FILE

    def agent(prompt, model, asker, effort, progress):
        asked.append((prompt, model, asker, effort, mark.exists()))
        if "Auction tracker" in prompt:
            visuals.visualize("My auctions, later", TEXT, name="Auction tracker")
            return "Updated."
        raise AgentError("not logged in")

    b = Bridge(addons, agent, publish=lambda text, **kw: said.append(text), notify=lambda *a, **kw: notes.append(a),
               history=bridge.History(None), models=True)
    # Not while the game isn't running.
    assert not b.game_running() and b.refresh_due(now + 3600) == []
    monkeypatch.setattr(game, "running_folders", lambda proc=None: {"_forever_"})
    assert b.game_running()
    # Not before it's due.
    assert b.refresh_due(now + 29 * 60) == []
    # Due: with what the player picked last.
    b.picked("Thorgrim", "haiku", "low")
    assert b.refresh_due(now + 31 * 60) == ["Auction tracker"]
    assert b.refresh_due(now + 32 * 60) == [], "on its way already"
    item = b.questions.get_nowait()
    assert item.auto and (item.name, item.player, item.model, item.effort) == ("Auction tracker", "Thorgrim", "haiku",
                                                                             "low")
    b.refresh(item)
    prompt, model, asker, effort, marked = asked[0]
    assert prompt == (f"Update the visual named 'Auction tracker': {TRACKER}. Use the visualize tool with "
                      "name='Auction tracker'. This runs on its own, on a timer, and nobody reads your reply: keep it "
                      "to a few words.")
    assert (model, asker, effort, marked) == ("haiku", "Thorgrim", "low", True) and not mark.exists()
    assert said == [] and notes == [], "no live-update slots but its own"
    assert visuals.find_named(transport().load_state(), "Auction tracker")["auto"] is True
    assert "refreshing the visual 'Auction tracker' on its own: every 30 min, last updated " in caplog.text
    # How long it took depends on the game's receipt for the new version, which nothing sends here.
    assert re.search(r"refresh of the visual 'Auction tracker' \(on its own\) took \d+:\d\d; tools used: none",
                     caplog.text), caplog.text

    # Next due 30 minutes after its new version. A failed one is tried again an interval later, not sooner.
    updated = time.time()
    assert b.refresh_due(updated + 29 * 60) == []
    assert b.refresh_due(now + 61 * 60) == ["Auction tracker", "Flip list"]
    for _ in range(2):
        b.refresh(b.questions.get_nowait())
    assert "refresh of the visual 'Flip list' failed: not logged in" in caplog.text and said == []
    assert "Flip list" not in b.refresh_due(now + 62 * 60)
    assert "Flip list" in b.refresh_due(now + 122 * 60)

    # Not when turned off (--no-visual-refresh): the loop doesn't start.
    off = Bridge(addons, agent, publish=lambda text, **kw: None, refresh_every=None)
    assert off.refresh_every is None


def test_the_x_on_a_tab_removes_it_on_the_server_too(addons, caplog):
    from wow_mcp.tools import visuals

    caplog.set_level(logging.INFO, logger="wow_mcp.bridge")
    keep(every=30)
    b = Bridge(addons, lambda *args: pytest.fail("no agent"), publish=lambda text, **kw: pytest.fail("no reply"),
               notify=lambda *a, **kw: pytest.fail("no note"))
    b.accept(stream.Post(77, 1, end=0, value={"remove": "Auction tracker", "player": "Thorgrim"}))
    assert visuals.named_visuals(transport().load_state()) == [] and b.questions.empty()
    assert "Thorgrim removed the visual 'Auction tracker' in game" in caplog.text
    # Gone already (the AI removed it meanwhile): nothing to do.
    b.accept(stream.Post(77, 2, end=0, value={"remove": "Auction tracker", "player": "Thorgrim"}))


def test_refresh_in_game_end_to_end(live_game, addons, tmp_path, running):
    """The player refreshes a named visual in game (its button, here /mcp visuals refresh); the bridge has the agent
    rebuild it by its instructions, with the player's picks, and the game shows the new version and the reply."""
    from wow_mcp.tools import visuals

    log, typed = tmp_path / "game.log", tmp_path / "typed.txt"
    keep(title="My auctions, before")
    asked = []

    def agent(prompt, model, asker, effort, progress):
        asked.append((prompt, model, asker))
        visuals.visualize("My auctions, now", TEXT, name="Auction tracker")
        return "2 of your auctions are undercut."

    setup = GAME.replace("LOG", log.as_posix()).replace("TYPED", typed.as_posix()) + """
WowMCPDB.aiTab.models = { ["Thorgrim-Firemaw"] = "sonnet" }
C_Timer.NewTicker(0.5, function()
    local f = WowMCPVisualFrame
    if f and f:IsShown() then Log("window: " .. f.Title.text .. " | " .. f.Footer.text) end
end)
"""
    with live_game(setup, seconds=25):
        running(Bridge(addons, agent, models=True))
        wait_for(lambda: log.exists() and "window: My auctions, before" in log.read_text(), seconds=10)
        type_in(typed, "/mcp visuals refresh")
        wait_for(lambda: "2 of your auctions are undercut." in log.read_text(), seconds=15)
        wait_for(lambda: "window: My auctions, now | Updated" in log.read_text(), seconds=5)

    assert [(model, asker) for _, model, asker in asked] == [("sonnet", "Thorgrim")]
    assert "Update the visual named 'Auction tracker': " + TRACKER in asked[0][0]
    lines = log.read_text(encoding="utf-8").splitlines()
    assert "chat: |cff33ff99[Claude Sonnet]|r 2 of your auctions are undercut." in lines
    assert not any("wow-mcp-bridge" in line for line in lines)


def test_a_refresh_counts_tokens_too(addons):
    # A bridge that counts the tokens Claude Code writes (as it does in game) sets up the count for a refresh as for a
    # question: a mismatch there once stopped the bridge answering at all.
    keep()
    said = []

    def agent(prompt, model, asker, effort, progress):
        return "Updated."

    b = Bridge(addons, agent, publish=lambda text, **kw: said.append(text), notify=lambda *a, **kw: None,
               history=bridge.History(None), tokens=True)
    b.accept(refresh_post(1))
    b.refresh(b.questions.get_nowait())
    assert said == ["Updated."]


def test_one_failure_doesnt_stop_the_answers(addons, caplog):
    # Something going wrong with one question or refresh: the player hears so, and the next is answered.
    keep()
    said = []

    def agent(prompt, model, asker, effort, progress):
        return "Here."

    b = Bridge(addons, agent, publish=lambda text, **kw: said.append((text, kw["question"])),
               notify=lambda *a, **kw: None, history=bridge.History(None))

    def broken(item):
        raise TypeError("a bug")

    b.refresh = broken
    b.accept(refresh_post(1))
    b.accept(question(2, "hello?"))
    worker = threading.Thread(target=b._work)
    worker.start()
    deadline = time.time() + 10
    while len(said) < 2 and time.time() < deadline:
        time.sleep(0.05)
    b.stop.set()
    worker.join(5)
    assert said[0][0] == "Sorry, something went wrong on my side (TypeError: a bug). Please ask again." and \
        said[0][1] == (77, 1), said
    assert said[1] == ("Here.", (1, 2)), said
    assert "couldn't handle" in caplog.text
