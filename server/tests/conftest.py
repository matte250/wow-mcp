"""Shared test setup. The addon runs in Lua against a stub client (stub/client.lua plus one file
per game API area in stub/apis/), with the real installer, transport and tools writing the
files in between."""

import contextlib
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from wow_mcp import app, battlenet, game, online_auctions
from wow_mcp.install import install

HERE = Path(__file__).parent
STUB = HERE / "stub" / "client.lua"
RUNNING_FOLDERS = game.running_folders  # before any test replaces it
LUA = shutil.which("lua")

# How a Lua test script hands control back to publish through the real map tools.
PUBLISH_SCRIPT = """
import json, sys
from wow_mcp.tools.map import Marker, clear_map, show_on_map
args = json.loads(sys.argv[1])
if args.get("clear"):
    clear_map(all_waypoints=args.get("all_waypoints", False))
else:
    show_on_map([Marker(**m) for m in args["markers"]], title=args.get("title"),
                nearest_first=args.get("nearest_first", False))
"""

BUILD_INFO = (
    "Branch!STRING:0|Active!DEC:1|Build Key!HEX:16|CDN Key!HEX:16|Install Key!HEX:16|IM Size!DEC:4|CDN Path!STRING:0|"
    "CDN Hosts!STRING:0|CDN Servers!STRING:0|Tags!STRING:0|Armadillo!STRING:0|Last Activated!STRING:0|Version!STRING:0|"
    "KeyRing!HEX:16|Product!STRING:0\n"
    + "".join(
        f"eu|1|aa|bb|||tpr/wow|level3.blizzard.com|http://x|Windows x86_64 EU? acct-SWE? geoip-SE? enUS speech?:"
        f"Windows x86_64 EU? acct-SWE? geoip-SE? enUS text?|||{version}||{product}\n"
        for version, product in [("12.1.5.70783", "wow"), ("2.5.6.70577", "wow_anniversary"),
                                 ("5.5.4.70631", "wow_classic"), ("1.60.1.70862", "wowf")]
    )
)


@pytest.fixture(autouse=True)
def no_online_prices(tmp_path, monkeypatch):
    """Keep every test away from the player's Blizzard API credentials and online price cache
    (battlenet.py, online_auctions.py): config and cache folders of its own, with nothing in them."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    for key in battlenet.KEYS:
        monkeypatch.delenv(key, raising=False)

    def offline(config, region):
        raise AssertionError("a test reached for Blizzard's real API")

    monkeypatch.setattr(online_auctions, "make_client", offline)


@pytest.fixture
def wow_root(tmp_path, monkeypatch):
    root = tmp_path / "World of Warcraft"
    root.mkdir()
    (root / ".build.info").write_text(BUILD_INFO)
    # Don't let WoW clients running on this machine affect the tests.
    monkeypatch.setattr(game, "running_folders", lambda proc=None: set())
    return root


@pytest.fixture
def addons(wow_root, tmp_path, monkeypatch):
    """The addon installed in a fresh AddOns folder, which the tools are pointed at."""
    addons_dir = wow_root / "_forever_" / "Interface" / "AddOns"
    addons_dir.mkdir(parents=True)
    install(addons_dir)
    script = tmp_path / "publish.py"
    script.write_text(PUBLISH_SCRIPT)
    monkeypatch.setenv("WOW_MCP_ADDONS_DIR", str(addons_dir))
    monkeypatch.setenv("WOWMCP_PUBLISH", f"{sys.executable} {script}")
    monkeypatch.setattr(app, "_transport", None)
    yield addons_dir
    if sys.platform == "win32":  # the folder's file watcher (opens_windows.py) would keep it from being removed
        from wow_mcp import opens_windows

        opens_windows.stop(addons_dir / "WowMCP" / "out")


@pytest.fixture
def run_client(addons, tmp_path):
    """Run a Lua test script against the stub client; it controls time with client.Tick/Wait."""

    def run(script: str) -> None:
        if not LUA:
            pytest.skip("lua interpreter not installed")
        test = tmp_path / "test.lua"
        test.write_text(textwrap.dedent(script))
        result = subprocess.run([LUA, str(STUB), str(addons), str(test)], capture_output=True, text=True)
        assert result.returncode == 0 and "OK" in result.stdout, result.stdout + result.stderr

    return run


@pytest.fixture
def live_game(addons, tmp_path, monkeypatch):
    """Context manager: the addon logged in and ticking in real time in another process, so the
    tools can talk to it. Takes Lua that sets up the stub before login."""
    if not LUA:
        pytest.skip("lua interpreter not installed")
    monkeypatch.setattr(game, "running_folders", lambda proc=None: {"_forever_"})

    @contextlib.contextmanager
    def start(setup: str = "", seconds: int = 8):
        script = tmp_path / "game.lua"
        script.write_text(f"{textwrap.dedent(setup)}\nclient.LoadAddon()\nclient.RunFor({seconds})\n")
        client = subprocess.Popen([LUA, str(STUB), str(addons), str(script)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(1.5)  # logged in and polling
            yield client
        finally:
            client.kill()
            _, errors = client.communicate()
            if errors.strip():
                print(errors)  # Lua errors show up in pytest's report

    return start
