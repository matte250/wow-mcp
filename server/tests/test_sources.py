"""Which sources agents are pointed to for the game version (tools/sources.py)."""

import importlib

from wow_mcp import app, game
from wow_mcp.tools import sources


def test_forever_sources(addons):
    current = sources.current_game()
    assert current.folder == "_forever_" and current.version == "1.60.1"
    hints = sources.source_hints(current)
    assert hints.startswith("Sources for World of Warcraft: Forever (patch 1.60.x")
    # Forever's own pages first, then Classic Era as a fallback the agent owns up to.
    assert hints.index("wowhead.com/forever/") < hints.index("warcraft.wiki.gg") < hints.index("wowhead.com/classic/")
    assert "Tell the player when your answer relies on Classic Era data." in hints
    assert "Not Forever: wowhead.com/ with no version in the path (retail" in hints


def test_guidance_registered_at_import(addons, monkeypatch):
    monkeypatch.setattr(app, "GUIDANCE", [])
    importlib.reload(sources)
    assert app.GUIDANCE == [(5, sources.FOREVER)]


def test_other_versions(wow_root, monkeypatch):
    def hints(folder):
        return sources.source_hints(game.installed_game(wow_root, folder))

    anniversary = hints("_anniversary_")
    assert anniversary.startswith("Sources for World of Warcraft Classic Anniversary (The Burning Crusade): "
                                  "Wowhead pages under wowhead.com/tbc/")
    assert "other Wowhead paths (/forever/, /classic/, /wotlk/, /cata/, /mop-classic/), patches other than 2.5.x" \
        in anniversary
    assert "Say so when you have to use another version's data." in anniversary
    assert "wowhead.com/mop-classic/quest=" in hints("_classic_")
    era = sources.source_hints(game.Game("_classic_era_", "1.15.8"))
    assert "wowhead.com/classic/" in era and "patches other than 1.15.x" in era and "Season of Discovery" in era

    retail = hints("_retail_")
    assert retail.startswith("Sources for World of Warcraft (Midnight): Wowhead pages with no version in the path")
    assert "/forever/, /classic/" in retail

    # Unknown version (not configured, or a flavour this doesn't know): how to tell versions apart.
    monkeypatch.delenv("WOW_MCP_ADDONS_DIR", raising=False)
    assert sources.current_game() is None
    assert sources.source_hints(None) == sources.UNKNOWN
    assert sources.source_hints(game.Game("_alpha_")) == sources.UNKNOWN
    assert "wowhead.com/forever/ (WoW: Forever)" in sources.UNKNOWN
