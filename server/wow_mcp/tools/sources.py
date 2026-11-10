"""Which information sources to trust for the game version being played (guidance only)."""

from wow_mcp import app, game

# Wowhead's path for each Classic expansion; retail has none.
WOWHEAD_CLASSIC = {1: "classic", 2: "tbc", 3: "wotlk", 4: "cata", 5: "mop-classic"}
RETAIL_PRODUCTS = ("wow", "wowt", "wowxptr", "wow_beta")

FOREVER = (
    "Sources for World of Warcraft: Forever (patch 1.60.x, launched 2026-11-04): the vanilla world plus new "
    "content (the zones Mount Hyjal, Riverglades, Shen'dralas and Zephras Isle, 1,000+ new quests, also in the "
    "original zones, new dungeons and raids, the Skyborne race, new race/class combinations). Look things up in "
    "this order:\n"
    "1. Wowhead's Forever database: pages under wowhead.com/forever/ (e.g. wowhead.com/forever/quest=..., "
    "/npc=..., /item=..., /zone=...).\n"
    "2. warcraft.wiki.gg: pages about Forever-only content say \"exclusive to World of Warcraft: Forever\"; on "
    "pages shared with other versions, use what's said about Forever or Classic.\n"
    "3. Guides: icy-veins.com/wow-forever, maxroll.gg/wow-forever.\n"
    "Forever's data is still being filled in. When it doesn't cover something, fall back to Classic Era data "
    "(wowhead.com/classic/, patch 1.15.x): content Forever kept from vanilla mostly matches, but quests, rewards "
    "and NPCs may have changed. Tell the player when your answer relies on Classic Era data.\n"
    "Not Forever: wowhead.com/ with no version in the path (retail, the world reshaped by Cataclysm, e.g. Northern "
    "and Southern Barrens, and retail's own Mount Hyjal zone); /tbc/, /wotlk/, /cata/, /mop-classic/; Season of "
    "Discovery pages (runes); private-server databases (patch 1.12)."
)

UNKNOWN = (
    "Wowhead's URL path tells which version a page is for: wowhead.com/forever/ (WoW: Forever), /classic/ "
    "(Classic Era, vanilla), /tbc/, /wotlk/, /cata/, /mop-classic/ (Classic progression), none (retail). Use the "
    "player's version and say so when you fall back to another."
)


def current_game() -> game.Game | None:
    """The version being played, or the one the addon is installed for (as in app.game_note)."""
    folder = app.addons_dir()
    if not folder:
        return None
    try:
        now = game.situation(folder)
    except (IndexError, OSError):
        return None
    return now.mismatch or (now.running[0] if now.running else None) or now.installed


def _paths(exclude: str | None = None) -> str:
    return ", ".join(f"/{p}/" for p in ("forever", *WOWHEAD_CLASSIC.values()) if p != exclude)


def source_hints(current: game.Game | None) -> str:
    if current is None:
        return UNKNOWN
    product = game.FOLDER_PRODUCTS.get(current.folder, "")
    if product == "wowf":
        return FOREVER
    try:
        major = int((current.version or "").split(".")[0])
    except ValueError:
        major = None
    if product.startswith(("wow_classic", "wow_anniversary")) and major in WOWHEAD_CLASSIC:
        path = WOWHEAD_CLASSIC[major]
        patch = ".".join(current.version.split(".")[:2])
        hint = (
            f"Sources for {current.name}: Wowhead pages under wowhead.com/{path}/ (e.g. wowhead.com/{path}/quest=...) "
            "and warcraft.wiki.gg (what it says about this version). Not this version: wowhead.com/ with no version "
            f"in the path (retail), other Wowhead paths ({_paths(path)}), patches other than {patch}.x."
        )
        if major == 1:
            hint += (" Season of Discovery and WoW: Forever pages describe other vanilla-based versions with extra "
                     "content.")
        return hint + " Say so when you have to use another version's data."
    if product in RETAIL_PRODUCTS:
        return (
            f"Sources for {current.name}: Wowhead pages with no version in the path (wowhead.com/quest=...) and "
            f"warcraft.wiki.gg. Wowhead pages under {_paths()} are for Classic versions or WoW: Forever, and guides "
            "older than the current expansion may be outdated. Say so when you have to use them."
        )
    return UNKNOWN


app.guidance(source_hints(current_game()), order=5)
