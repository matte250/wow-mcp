"""What a request asks the game for: a REQUEST frame of the bell (bell.py) carries the kind in 3 bits. Must
match the addon's Requests.lua."""

COUNT = 8

STATE = 0  # location, character, professions (Player.lua); money, XP, hearthstone (Stats.lua)
QUESTS = 1  # completed quests, quest log (Quests.lua)
ITEMS = 2  # bags, equipment, bank (Inventory.lua)
REPUTATION = 3  # faction standings (Reputation.lua); spellbook, talents (Player.lua)
TRAVEL = 4  # known flight paths (Travel.lua)
KNOWLEDGE = 5  # recipes and vendors seen (Knowledge.lua)
STATUS = 6  # addon build, live channel, slots (Status.lua)
ITEM_DETAILS = 7  # tooltip and stats of the items in the itemLookup payload (Inventory.lua)
