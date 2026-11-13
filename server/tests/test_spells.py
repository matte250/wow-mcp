"""The character's spellbook and talents, and any spell by ID (Player.lua, tools/spells.py).

The stub's spellbook, talents and spells are in stub/apis/spells.lua: player.lua's level 12 Dwarf Hunter with a pet,
three professions, and 3 talent points, 2 of them in Efficiency.
"""

import asyncio
import json
import zlib

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from wow_mcp import kinds
from wow_mcp.tools import items, spells

GAME = ("World of Warcraft: Forever 1.60.1 (build 70862, EU region, enUS client), a vanilla-era world on the modern "
        "client")


def spell(spell_id, name, rank=None, subtext=None, passive=False, lower_ranks=(), cooldown_left=None):
    return {"spell_id": spell_id, "name": name, "rank": rank, "subtext": subtext, "passive": passive,
            "lower_ranks": list(lower_ranks), "cooldown_left": cooldown_left}


def group(name, kind, spell_list, not_learned=()):
    return {"name": name, "kind": kind, "spells": spell_list, "not_learned": list(not_learned)}


def talent(name, spell_id, max_rank, tier, rank=0, needs=None, requires=(), can_take=False, pending_rank=None):
    return {"name": name, "spell_id": spell_id, "rank": rank, "max_rank": max_rank, "tier": tier,
            "points_in_tree_needed": needs, "requires": list(requires), "can_take": can_take, "choices": None,
            "pending_rank": pending_rank}


def details(spell_id, status="ok", name=None, rank=None, subtext=None, passive=None, known=None, cast_time=None,
            min_range=None, max_range=None, costs=(), cooldown=None, cooldown_left=None, description=None, tooltip=(),
            shown_in_red=(), talent=None, error=None):
    return {"spell_id": spell_id, "status": status, "name": name, "rank": rank, "subtext": subtext, "passive": passive,
            "known": known, "cast_time": cast_time, "min_range": min_range, "max_range": max_range,
            "costs": list(costs), "cooldown": cooldown, "cooldown_left": cooldown_left, "description": description,
            "tooltip": list(tooltip), "shown_in_red": list(shown_in_red), "talent": talent, "error": error}


def mana(amount):
    return {"power": "mana", "amount": amount, "percent": None, "per_second": None}


SPELLBOOK = [
    group("General", "general", [
        spell(6603, "Attack"), spell(75, "Auto Shot"), spell(2481, "Find Treasure", subtext="Racial"),
        spell(20594, "Stoneform", subtext="Racial"),
        spell(20595, "Gun Specialization", subtext="Racial Passive", passive=True), spell(81, "Dodge", passive=True),
    ]),
    group("Beast Mastery", "class", [
        spell(13163, "Aspect of the Monkey"), spell(1515, "Tame Beast"), spell(883, "Call Pet"),
        spell(136, "Mend Pet", rank=1),
    ], [
        {"spell_id": 13165, "name": "Aspect of the Hawk", "rank": 1, "subtext": None, "learn_at_level": 10,
         "trainable_now": True},
        {"spell_id": 1513, "name": "Scare Beast", "rank": 1, "subtext": None, "learn_at_level": 14,
         "trainable_now": False},
    ]),
    group("Marksmanship", "class", [
        spell(3044, "Arcane Shot", rank=1), spell(1130, "Hunter's Mark", rank=1),
        spell(13549, "Serpent Sting", rank=2, lower_ranks=[1978]),
    ]),
    # Track Humanoids is another specialization's; the hidden skill line isn't a tab.
    group("Survival", "class", [
        spell(14260, "Raptor Strike", rank=2, lower_ranks=[2973]), spell(1494, "Track Beasts"),
    ]),
    group("Leatherworking", "profession", [spell(2108, "Leatherworking", subtext="Apprentice")]),
    group("Skinning", "profession", [spell(8613, "Skinning", subtext="Journeyman", passive=True)]),
    group("Cooking", "profession", [spell(2550, "Cooking", subtext="Apprentice"), spell(818, "Basic Campfire")]),
    group("Pet", "pet", [spell(17253, "Bite", rank=1), spell(2649, "Growl", rank=1)]),
]

SETUP = """
    client.spec = { 2, "Marksmanship", "DAMAGER" }
    client.cooldowns = {}
"""


def test_spellbook_and_talents(live_game):
    with live_game(SETUP):
        book = asyncio.run(spells.get_spells())
        talents = asyncio.run(spells.get_talents())

    assert (book.game, book.player_class, book.level, book.note) == (GAME, "Hunter", 12, None)
    assert [g.model_dump() for g in book.groups] == SPELLBOOK

    assert (talents.game, talents.player_class, talents.level, talents.specialization, talents.role) == (
        GAME, "Hunter", 12, "Marksmanship", "damage")
    assert (talents.unspent_points, talents.points_spent, talents.talent_set, talents.note) == (1, 2, None, None)
    assert [(t.name, t.points_spent, len(t.talents)) for t in talents.trees] == [
        ("Beast Mastery", 0, 16), ("Marksmanship", 2, 14), ("Survival", 0, 16)]
    beast, marks, survival = (t.talents for t in talents.trees)
    assert [t.model_dump() for t in marks[:5]] == [
        talent("Improved Concussive Shot", 19407, 5, 1, can_take=True),
        talent("Efficiency", 19416, 5, 1, rank=2, can_take=True),
        talent("Improved Hunter's Mark", 19421, 5, 2, needs=5),
        talent("Lethal Shots", 19426, 5, 2, needs=5),
        talent("Aimed Shot", 19434, 1, 3, needs=10),
    ]
    assert marks[-1].model_dump() == talent("Trueshot Aura", 19506, 1, 7, needs=30, requires=["Aimed Shot"])
    assert [(t.name, t.requires) for t in beast + survival if t.requires] == [
        ("Frenzy", ["Ferocity"]), ("Bestial Wrath", ["Intimidation"]), ("Counterattack", ["Deterrence"])]
    assert [t.name for tree in talents.trees for t in tree.talents if t.can_take] == [
        "Improved Aspect of the Hawk", "Endurance Training", "Improved Concussive Shot", "Efficiency",
        "Monster Slaying", "Humanoid Slaying", "Deflection"]
    # Tiers top down, each tier's talents left to right.
    assert [t.tier for t in survival] == [1, 1, 1, 2, 2, 2, 3, 3, 3, 4, 4, 5, 5, 5, 6, 7]


def test_addon_spellbook_and_talents(run_client):
    run_client(r"""
        local ns = client.LoadAddon()
        local function Ask()
            local reply
            ns.Stream.Send = function(_, value) reply = value end
            ns.Answer(3, ns.Kinds.REPUTATION)
            assert(not reply.errors, reply.errors and next(reply.errors))
            return reply.spellbook, reply.talents
        end
        local function Find(tabs, id)
            for _, tab in ipairs(tabs) do
                for _, spell in ipairs(tab.spells) do
                    if spell.id == id then return spell end
                end
            end
        end

        -- Cooldowns: seconds left, not the global cooldown, nothing when the game keeps them secret.
        client.now = 2
        client.cooldowns[3044] = 1
        client.spells[3044].cooldown = 1.5
        local book = Ask()
        assert(Find(book.tabs, 14260).cd == 4 and Find(book.tabs, 3044).cd == nil)
        local function Secret() error("attempt to compare a secret value") end
        local secret = setmetatable({}, { __le = Secret, __lt = Secret })
        local GetCooldown = C_SpellBook.GetSpellBookItemCooldown
        C_SpellBook.GetSpellBookItemCooldown = function()
            return { startTime = 0, duration = secret, isEnabled = true }
        end
        book = Ask()
        assert(Find(book.tabs, 14260).cd == nil and Find(book.tabs, 14260).lower[1] == 2973)
        C_SpellBook.GetSpellBookItemCooldown = GetCooldown

        -- A client that can't tell lower ranks lists every rank on its own.
        local LowRank = C_SpellBook.IsSpellBookItemLowRank
        C_SpellBook.IsSpellBookItemLowRank = nil
        book = Ask()
        assert(Find(book.tabs, 1978).rank == "Rank 1" and Find(book.tabs, 13549).lower == nil)
        C_SpellBook.IsSpellBookItemLowRank = LowRank

        -- The pet's tab shows when the spellbook would show it.
        function PetHasSpellbook() return false end
        book = Ask()
        assert(book.tabs[#book.tabs].kind == "profession")
        PetHasSpellbook = nil

        -- A pick not applied yet; dual specialization.
        client.talents.trees[3].talents[3].pending = 1
        client.talents.groups = 2
        local _, talents = Ask()
        local deflection = talents.trees[3].talents[3]
        assert(deflection.name == "Deflection" and deflection.rank == 0 and deflection.pending == 1)
        assert(talents.unspent == 1 and talents.spent == 2, "as applied")
        assert(talents.trees[3].spent == 1, "the tree's header counts the pick")
        assert(talents.group == 1 and talents.groups == 2)
        assert(talents.spec == nil, "no specialization yet")

        -- No talent config yet.
        client.talents.config = nil
        _, talents = Ask()
        assert(talents.none and #talents.trees == 0 and talents.class == "Hunter")

        -- A client without the APIs says so.
        C_SpellBook.GetNumSpellBookSkillLines = nil
        C_Traits = nil
        book, talents = Ask()
        assert(book.problem:find("C_SpellBook") and talents.problem:find("C_Traits"))
        print("OK")
    """)


def test_full_spellbook_and_talents_stay_compact(run_client, tmp_path):
    """A level 60 caster: four tabs with every rank of 40 spells, five professions, and 51 points over three full
    talent trees. Both sections together should stream in a second or two."""
    out = tmp_path / "reply.json"
    run_client(r"""
        local words = { "Arcane", "Fire", "Frost", "Missiles", "Blast", "Bolt", "Nova", "Armor", "Intellect", "Shield",
            "Polymorph", "Conjure", "Mana", "Water", "Food", "Explosion", "Ward", "Blink", "Counterspell", "Evocation",
            "Flamestrike", "Scorch", "Cone", "Cold", "Ice", "Barrier", "Remove", "Curse", "Slow", "Fall", "Teleport" }
        local id, lines = 10000, {}
        local function Spell(name, rank, passive)
            id = id + 1
            client.spells[id] = { name = name, rank = rank, passive = passive }
            return id
        end
        local tabs = { { "General", 8, 1 }, { "Arcane", 14, 8 }, { "Fire", 12, 12 }, { "Frost", 14, 11 } }
        local n = 0
        for _, tab in ipairs(tabs) do
            local items = {}
            for i = 1, tab[2] do
                n = n + 1
                local name = words[n % #words + 1] .. " " .. words[n * 7 % #words + 1]
                local ranks = (n * 5) % tab[3] + 1
                for r = 1, ranks do
                    local id = Spell(name, ranks > 1 and ("Rank " .. r) or nil, n % 9 == 0)
                    items[#items + 1] = { id = id, low = r < ranks }
                end
            end
            lines[#lines + 1] = { name = tab[1], items = items }
        end
        client.spellbook = { lines = lines, pet = {}, professions = {
            [1] = { name = "Tailoring", rank = 300, maxRank = 300, items = { { id = Spell("Tailoring", "Artisan") } } },
            [2] = { name = "Enchanting", rank = 300, maxRank = 300,
                items = { { id = Spell("Enchanting", "Artisan") }, { id = Spell("Disenchant") } } },
            [3] = { name = "First Aid", rank = 300, maxRank = 300, items = { { id = Spell("First Aid", "Artisan") } } },
            [4] = { name = "Fishing", rank = 150, maxRank = 150, items = { { id = Spell("Fishing", "Journeyman") } } },
            [5] = { name = "Cooking", rank = 225, maxRank = 225,
                items = { { id = Spell("Cooking", "Expert") }, { id = Spell("Basic Campfire") } } },
        } }
        client.talents.earned = 51
        local spend = { 21, 30, 0 }
        for t, tree in ipairs(client.talents.trees) do
            for _, talent in ipairs(tree.talents) do
                local points = math.min(talent.max, spend[t])
                talent.rank, spend[t] = points, spend[t] - points
            end
        end
        local ns = client.LoadAddon()
        local reply
        ns.Stream.Send = function(_, value) reply = value end
        ns.Answer(3, ns.Kinds.REPUTATION)
        assert(#reply.spellbook.tabs == 9 and #reply.talents.trees == 3 and reply.talents.unspent == 0)
        local f = assert(io.open("OUT", "w"))
        f:write(C_EncodingUtil.SerializeJSON({ spellbook = reply.spellbook, talents = reply.talents }))
        f:close()
        print("OK")
    """.replace("OUT", out.as_posix()))
    reply = json.loads(out.read_bytes())
    book, talents = (zlib.compress(json.dumps(reply[k]).encode(), 9) for k in ("spellbook", "talents"))
    assert sum(len(s["lower"]) for tab in reply["spellbook"]["tabs"] for s in tab["spells"] if "lower" in s) > 150
    assert len(book) < 3000 and len(talents) < 3000 and len(book) + len(talents) < 5000, (len(book), len(talents))


@pytest.fixture
def replies(monkeypatch):
    """The spellDetails section of every reply the tool gets, in order."""
    seen = []
    ask = items.request

    async def recording(kind):
        reply, version = await ask(kind)
        seen.append(reply.get("spellDetails"))
        return reply, version

    monkeypatch.setattr(items, "request", recording)
    return seen


def get_spell_details(ids):
    return asyncio.run(spells.get_spell_details(ids))


ARCANE_SHOT = details(
    3044, name="Arcane Shot", rank=1, passive=False, known=True, cast_time=0.0, min_range=8, max_range=35,
    costs=[mana(25)], cooldown=6.0, description="An instant shot that causes 13 Arcane damage.",
    tooltip=["Arcane Shot\tRank 1", "25 Mana\t35 yd range", "Instant\t6 sec cooldown",
             "An instant shot that causes 13 Arcane damage."],
)
EFFICIENCY = details(
    19416, name="Efficiency", passive=True, known=True, cast_time=0.0, min_range=0, max_range=0,
    description="Reduces the Mana cost of your Shots and Stings by 2%.",
    tooltip=["Efficiency", "Reduces the Mana cost of your Shots and Stings by 2%."],
    talent={"rank": 2, "max_rank": 5, "this_rank": ["Reduces the Mana cost of your Shots and Stings by 4%."],
            "next_rank": ["Reduces the Mana cost of your Shots and Stings by 6%."],
            "max_rank_text": ["Reduces the Mana cost of your Shots and Stings by 10%."]},
)
SCARE_BEAST = details(
    1513, name="Scare Beast", rank=1, passive=False, known=False, cast_time=1.5, min_range=0, max_range=30,
    costs=[mana(30)], cooldown=30.0, description="Scares a beast, causing it to run in fear for up to 10 sec.",
    tooltip=["Scare Beast\tRank 1", "30 Mana\t30 yd range", "1.5 sec cast\t30 sec cooldown", "Requires Level 14",
             "Scares a beast, causing it to run in fear for up to 10 sec."],
    shown_in_red=["Requires Level 14"],
)


def test_spell_details(live_game, replies):
    setup = SETUP + "client.knownSpells[3044], client.knownSpells[19416] = true, true"
    with live_game(setup):
        result = get_spell_details([3044, 19416, 1513, 999999, 3044])
        asked = len(replies)
        # Asking again for the same spells needs no new update: the game has the list.
        again = get_spell_details([3044, 19416, 1513, 999999])

    assert result.game == GAME
    assert [s.model_dump() for s in result.spells] == [ARCANE_SHOT, EFFICIENCY, SCARE_BEAST, details(999999, "unknown")]
    assert again == result and len(replies) == asked + 1
    state = spells.transport().load_state()
    assert state["seq"] == 1 and state["spellLookup"]["spells"] == [3044, 19416, 1513, 999999]


def test_spell_that_loads_on_a_retry(live_game, replies):
    with live_game(SETUP):
        result = get_spell_details([5019])
    assert replies[0]["spells"][0]["status"] == "loading" and len(replies) >= 2
    shoot = result.spells[0]
    assert (shoot.status, shoot.name, shoot.description, shoot.max_range, shoot.known) == (
        "ok", "Shoot", "Shoot with a wand.", 30, False)


def test_client_without_the_apis(live_game):
    with live_game("C_SpellBook, C_Traits, C_Spell = nil, nil, nil"):
        book = asyncio.run(spells.get_spells())
        talents = asyncio.run(spells.get_talents())
        found = get_spell_details([133])
    assert book.groups == [] and book.note == (
        "The game couldn't read the spellbook: This game client has no spellbook API (C_SpellBook).")
    assert talents.trees == [] and talents.note == (
        "The game couldn't read the talents: This game client has no talent API (C_Traits).")
    assert [s.model_dump() for s in found.spells] == [
        details(133, "error", error="This game client has no spell data API (C_Spell, C_TooltipInfo).")]


def test_addon_spell_lookups(run_client):
    run_client(r"""
        local ns = client.LoadAddon()
        local function Ask()
            local reply
            ns.Stream.Send = function(_, value) reply = value end
            ns.Answer(7, ns.Kinds.ITEM_DETAILS)
            return reply.spellDetails
        end
        local function Lookup(id, list)
            WowMCP_Receive({ seq = id, spellLookup = { id = id, at = 0, spells = list } })
        end

        -- Nothing asked for yet.
        assert(Ask().lookup == nil and #Ask().spells == 0)

        -- Bad payloads are ignored; lists are capped at 20 numbers.
        WowMCP_Receive({ seq = 1, spellLookup = "nope" })
        WowMCP_Receive({ seq = 2, spellLookup = { id = 2 } })
        assert(Ask().lookup == nil)
        local many = { "x" }
        for i = 1, 25 do many[#many + 1] = 133 end
        Lookup(3, many)
        assert(Ask().lookup == 3 and #Ask().spells == 20 and Ask().spells[1].name == "Fireball")

        -- Uncached spells start loading as soon as the list arrives; one the client can't load is unknown.
        Lookup(4, { 5019, 999999, 3044 })
        assert(client.spells[5019].loading)
        local first = Ask()
        assert(first.spells[1].status == "loading" and first.spells[2].status == "unknown")
        assert(first.spells[3].name == "Arcane Shot")
        client.Wait(42)
        assert(Ask().spells[1].description == "Shoot with a wand.")

        -- The answer is there for two minutes; then other ITEM_DETAILS replies stop carrying it.
        client.Wait(200)
        local late = Ask()
        assert(late.lookup == 4 and late.expired and late.spells == nil)

        -- A talent's ranks only for talents in the character's trees; a spell that fails to read is an error.
        Lookup(5, { 19416, 19552, 3044 })
        local efficiency, hawk = Ask().spells[1].talent, Ask().spells[2].talent
        assert(efficiency.rank == 2 and efficiency.max == 5 and efficiency.now and efficiency.next and efficiency.top)
        assert(hawk.rank == 0 and hawk.now == nil and hawk.next == nil, "no text for that talent in the stub")
        local GetSpellInfo = C_Spell.GetSpellInfo
        C_Spell.GetSpellInfo = function(id) if id == 3044 then error("boom") end return GetSpellInfo(id) end
        local broken = Ask().spells[3]
        assert(broken.status == "error" and broken.error:find("boom"))
        print("OK")
    """)


def test_reply_quirks(monkeypatch):
    """The real client sends empty Lua tables as {}; missing or odd values don't break the tools."""
    replies = {
        "spellbook": {"class": "Hunter", "level": 12, "tabs": [
            {"name": "General", "kind": "general", "spells": {}},
            {"name": "Odd", "kind": "racial", "spells": [{"id": 75, "name": "Auto Shot", "rank": "Passive",
                                                          "lower": {}}, {"name": "no id"}, "junk"]},
        ]},
        "talents": {"class": "Hunter", "level": 9, "trees": {}, "none": True},
    }

    async def fake(kind):
        assert kind == kinds.REPUTATION
        return replies, "the game"

    monkeypatch.setattr(spells, "request", fake)
    book = asyncio.run(spells.get_spells())
    assert [g.model_dump() for g in book.groups] == [
        group("General", "general", []), group("Odd", "class", [spell(75, "Auto Shot")])]
    talents = asyncio.run(spells.get_talents())
    assert talents.trees == [] and talents.unspent_points is None and "no talents yet" in talents.note

    replies.update(errors={"spellbook": "boom"})
    del replies["spellbook"]
    with pytest.raises(ToolError, match="couldn't collect spellbook: boom"):
        asyncio.run(spells.get_spells())
    del replies["talents"]
    with pytest.raises(ToolError, match="doesn't send talents yet"):
        asyncio.run(spells.get_talents())
