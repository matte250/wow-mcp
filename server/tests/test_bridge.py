"""/ai in game (Chat.lua) and the bridge that answers it (bridge.py). The agent is always a fake
here: a Python function, or a small Python script standing in for the agent command."""

import asyncio
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from wow_mcp import bridge, item_text, stream
from wow_mcp.app import transport
from wow_mcp.bridge import (
    MAX_WAITING, SYSTEM_PROMPT, AgentError, AgentTimeout, Bridge, CommandAgent, chat_lines, claude_command,
)
from wow_mcp.tools import messages, player


def publish_per_contract(text, style="chat", sender="Claude", reply=False, default_model=None):
    """Stands in for tools/messages.py: the `messages` payload as docs/PLAN.md (W7) defines it."""

    def change(state, seq):
        message = {"id": seq, "text": text, "style": style, "from": sender, **({"reply": True} if reply else {}),
                   **({"defaultModel": default_model} if reply and default_model is not None else {})}
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
        for _, m in ipairs(payload.messages or {}) do Log(string.format("message %d %s: %s", m.id, m.from, m.text)) end
        return receive(payload)
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

    def agent(prompt, model):
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
    # One message per answer (one live-update slot); the game prints it line by line.
    state = json.loads((addons / "WowMCP" / "server-state.json").read_text())
    assert [(m["text"], m["style"], m["reply"]) for m in state["messages"]] == [
        ("You're in Durotar, at 50, 50.", "chat", True),
        ("Go and see Gornek.\n- He's in the den.\n- Then come back.", "chat", True),
    ]


def test_new_conversation_in_game_end_to_end(live_game, addons, tmp_path, monkeypatch, running):
    """/ai new in game clears the conversation; /ai new quests near me? is just a follow-up."""
    log, typed = tmp_path / "game.log", tmp_path / "typed.txt"
    monkeypatch.setattr(messages, "publish_message", publish_per_contract, raising=False)
    prompts = []

    def agent(prompt, model):
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
    # The reset itself sends nothing back (no live-update slot spent).
    state = json.loads((addons / "WowMCP" / "server-state.json").read_text())
    assert [m["text"] for m in state["messages"]] == ["Answer 1.", "Answer 2.", "Answer 3."]
    saved = json.loads((addons / "WowMCP" / "bridge-history.json").read_text())
    assert [(e["question"], e["answer"]) for e in saved["Thorgrim"]] == [("what now?", "Answer 3.")]


def test_follow_ups_see_the_conversation(addons):
    now = [1_000_000.0]
    prompts = []

    def agent(prompt, model):
        prompts.append(prompt)
        return f"Answer {len(prompts)}:\n\n- **Karolek**   is in The Drag."

    history = bridge.History(addons / "WowMCP" / "bridge-history.json", clock=lambda: now[0])
    b = Bridge(addons, agent, publish=lambda text, style, sender, reply, default_model: None, history=history)
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
    outcomes = [AgentError("not logged in"), AgentTimeout("it took over 180 seconds"), KeyError("broken"), "",
                "It's in Orgrimmar.", "Go west."]

    def agent(prompt, model):
        prompts.append(prompt)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    b = Bridge(addons, agent, publish=lambda text, style, sender, reply, default_model: said.append(text))
    for q in ("q1", "q2", "q3", "q4", "where is it?", "and then?"):
        b.answer(bridge.Question(q, "Thorgrim"))
    assert said[3] == "I have no answer to that."
    assert prompts[:5] == [f"Thorgrim asks, in game: {q}" for q in ("q1", "q2", "q3", "q4", "where is it?")]
    assert prompts[5] == earlier(("where is it?", 0, "It's in Orgrimmar.")) + "Thorgrim asks, in game: and then?"

    # Nor is an answer the player never sees because the bridge is stopping.
    b.agent = lambda prompt, model: "Too late."
    b.stop.set()
    b.answer(bridge.Question("one more?", "Thorgrim"))
    assert [e.answer for e in b.history.recent("Thorgrim")] == ["It's in Orgrimmar.", "Go west."]


def test_history_survives_a_restart(addons):
    prompts = []

    def agent(prompt, model):
        prompts.append(prompt)
        return "Karolek, in The Drag."

    quiet = lambda text, style, sender, reply, default_model: None  # noqa: E731
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

    def agent(prompt, model):
        prompts.append(prompt)
        started.set()
        release.wait(5)
        return "An answer."

    said = []
    b = running(Bridge(addons, agent, publish=lambda text, style, sender, reply, default_model: said.append(text)))
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

        client.Type("/ai")
        assert(Said("/ai <question>") and Said("/ai new - start a fresh conversation") and #posted == 0, "usage")

        -- Links become text the agent can read; the echo keeps them clickable.
        local link = "|cff1eff00|Hitem:2589::::::::60:::::::::|h[Linen Cloth]|h|r"
        client.Type("/ask who buys " .. link .. "?  ")
        assert(posted[1].question == "who buys [Linen Cloth] (item 2589)?" and posted[1].player == "Thorgrim")
        assert(Said("Asking… who buys " .. link .. "?"))

        -- No update at all for a while: the bridge may not be running.
        client.Wait(170)
        assert(not Said("Still no answer"))
        client.Wait(12)
        assert(Said("Still no answer"))

        -- Any update in the meantime (the answer, or markers) means it is.
        wipe(client.chat)
        client.Type("/ai where am I?")
        ns.run.seq = ns.run.seq + 1
        client.Wait(200)
        assert(not Said("Still no answer"))

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
        client.Wait(200)
        assert(not Said("Still no answer"))

        -- A question that only starts with "new" is a question.
        client.Type("/ai new quests near me?")
        assert(posted[7].question == "new quests near me?" and posted[7].reset == nil)
        assert(Said("Asking… new quests near me?"))
    """)


def test_questions_wait_their_turn(addons, running):
    said, busy, started, release = [], [], threading.Event(), threading.Event()

    def agent(prompt, model):
        busy.append(prompt)
        assert len(busy) == 1, "one question at a time"
        started.set()
        release.wait(5)
        busy.remove(prompt)
        return "Answer: " + prompt.rsplit(": ", 1)[1]

    b = running(Bridge(addons, agent, publish=lambda text, style, sender, reply, default_model: said.append(text)))
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

    def agent(prompt, model):
        raise AgentError("the agent exited with status 1: not logged in")

    b = Bridge(addons, agent, publish=lambda text, style, sender, reply, default_model: said.append((sender, text)))
    b.answer(bridge.Question("where am I?"))
    b.accept(stream.Post(1, 1, end=0, error="post 1 arrived damaged"))
    b.accept(stream.Post(1, 2, end=0, value={"not": "a question"}))
    b.agent = lambda prompt, model: {}["broken"]
    b.answer(bridge.Question("where am I?"))
    assert said == [
        ("Claude", "Sorry, I couldn't answer that: the agent exited with status 1: not logged in"),
        ("Claude", "Your question arrived garbled. Please ask again."),
        ("Claude", "Sorry, I couldn't answer that: 'broken'"),
    ]

    # A stuck agent is stopped, together with what it started (e.g. its MCP servers).
    child = tmp_path / "child.pid"
    stuck = (
        "import subprocess, time\n"
        f"open({str(child)!r}, 'w').write(str(subprocess.Popen(['sleep', '60']).pid))\n"
        "time.sleep(60)\n"
    )
    b.agent = CommandAgent([sys.executable, "-c", stuck], addons, timeout=1)
    started = time.monotonic()
    b.answer(bridge.Question("where am I?"))
    assert time.monotonic() - started < 8
    assert said[-1] == ("Claude", "Sorry, I gave up: it took over 1 seconds. Try asking something narrower.")
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
    agent = CommandAgent([sys.executable, "-c", FAKE_AGENT, "{system_prompt}", "{mcp_config}"], addons, timeout=20)
    assert agent("Where?") == f"Where? in {addons}, {SYSTEM_PROMPT[:30]}"

    def run(code):
        return CommandAgent([sys.executable, "-c", code], addons, timeout=20)("Where?")

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
    assert option("--output-format") == "json"
    assert option("--model") == "sonnet"
    assert "--model" not in claude_command()
    assert isinstance(AgentTimeout("x"), AgentError)


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

    def agent(prompt, model):
        asked.append((prompt.rsplit(": ", 1)[1], model))
        return f"Answer {len(asked)}."

    def publish(text, style, sender, reply, default_model):
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
    agent = CommandAgent(command, addons, timeout=20)

    def models(*args):
        argv = json.loads(agent("Where?", *args))
        return [argv[i + 1] for i, arg in enumerate(argv) if arg == "--model"]

    assert models() == ["opus"] and models(None) == ["opus"]
    assert models("haiku") == ["haiku"] and models("fable") == ["fable"]
    assert models() == ["opus"]  # a pick lasts one question
    assert CommandAgent([sys.executable, "-c", ARGS_AGENT, *claude_command()[1:]], addons, timeout=20)("Where?", "sonnet")
    with pytest.raises(ValueError, match="not a model the player can pick"):
        agent("Where?", "sonnet --dangerously-skip-permissions")
    assert bridge.with_model(["claude", "-p"], "sonnet") == ["claude", "-p", "--model", "sonnet"]

    # From a post to the command line: an unknown model is dropped on the way, so the bridge's own runs.
    ran = []
    b = Bridge(addons, lambda prompt, model: ran.append(json.loads(agent(prompt, model))) or "ok",
               publish=lambda text, style, sender, reply, default_model: None, history=bridge.History(None),
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

    def agent(prompt, model):
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
