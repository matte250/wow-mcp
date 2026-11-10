"""Faction standings (Reputation.lua, tools/reputation.py)."""

import asyncio

from wow_mcp import kinds
from wow_mcp.tools import reputation


def test_reputations(live_game):
    with live_game():
        result = asyncio.run(reputation.get_reputations())
    assert result.game.startswith("World of Warcraft: Forever")
    assert [f.model_dump() for f in result.factions] == [
        {"name": "Gnomeregan Exiles", "faction_id": 54, "group": "Alliance", "standing": "Friendly", "standing_rank": 5,
         "progress": 1500, "progress_max": 6000, "at_war": False},
        {"name": "Ironforge", "faction_id": 47, "group": "Alliance", "standing": "Honored", "standing_rank": 6,
         "progress": 3250, "progress_max": 12000, "at_war": False},
        {"name": "Steamwheedle Cartel", "faction_id": 169, "group": "Other", "standing": "Neutral", "standing_rank": 4,
         "progress": 1200, "progress_max": 3000, "at_war": False},
        {"name": "Booty Bay", "faction_id": 21, "group": "Other > Steamwheedle Cartel", "standing": "Neutral",
         "standing_rank": 4, "progress": 1500, "progress_max": 3000, "at_war": False},
        {"name": "Bloodsail Buccaneers", "faction_id": 87, "group": "Other", "standing": "Hostile", "standing_rank": 2,
         "progress": 500, "progress_max": 3000, "at_war": True},
        {"name": "Wintersaber Trainers", "faction_id": 589, "group": "Inactive", "standing": "Neutral",
         "standing_rank": 4, "progress": 0, "progress_max": 3000, "at_war": False},
    ]


def test_collapsed_headers_are_read_and_restored(run_client):
    run_client(r"""
        local ns = client.LoadAddon()
        local sent
        ns.Stream.Send = function(_, value) sent = value end
        local function Read() ns.Answer(3, ns.Kinds.REPUTATION) return sent end
        local function Collapsed()
            local names = {}
            for _, row in ipairs(client.factions) do
                if row.collapsed then names[#names + 1] = row.name end
            end
            return table.concat(names, ", ")
        end

        assert(C_Reputation.GetNumFactions() == 5)
        assert(#Read().factions == 6, "factions under collapsed headers and sub-headers are included")
        assert(Collapsed() == "Other, Steamwheedle Cartel, Inactive", Collapsed())
        assert(C_Reputation.GetNumFactions() == 5)

        -- An open header with a collapsed sub-header.
        client.factions[4].collapsed = false
        assert(#Read().factions == 6)
        assert(Collapsed() == "Steamwheedle Cartel, Inactive", Collapsed())

        -- A read that fails still closes what it opened.
        client.secretFaction = "Booty Bay"
        local reply = Read()
        assert(reply.factions == nil and reply.errors.factions:find("arithmetic", 1, true), reply.errors.factions)
        assert(Collapsed() == "Steamwheedle Cartel, Inactive", Collapsed())
    """)


def test_no_factions(monkeypatch):
    # An empty Lua table serializes as {} rather than [].
    async def request(kind):
        assert kind == kinds.REPUTATION
        return {"factions": {}}, "WoW"

    monkeypatch.setattr(reputation, "request", request)
    assert asyncio.run(reputation.get_reputations()).model_dump() == {"game": "WoW", "factions": []}
