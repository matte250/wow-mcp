"""Blizzard's API client (battlenet.py) against a fake of the HTTP layer: credentials, the access token, conditional
GETs, retries, the endpoints' paths, and that the secret and the token never show up anywhere."""

import base64
import email.utils
import logging

import httpx2 as httpx
import pytest

from wow_mcp import battlenet

CLIENT_ID, SECRET, TOKEN = "client-id-123", "s3cret-VALUE-9f2c", "TOKEN-abc-77ab"
BASIC = "Basic " + base64.b64encode(f"{CLIENT_ID}:{SECRET}".encode()).decode()
LAST_MODIFIED = 1794352416  # 2026-11-10 23:13:36 UTC
CONFIG = battlenet.Config(CLIENT_ID, SECRET)


class Clock:
    def __init__(self, now: float = 1_800_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class Fake:
    """Answers like Blizzard's API; `script` holds statuses to answer API requests with first."""

    def __init__(self):
        self.tokens = 0
        self.requests: list[httpx.Request] = []
        self.script: list[int | Exception] = []
        self.token_status = 200
        self.expires_in = 86399

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "oauth.battle.net":
            assert request.method == "POST" and request.content == b"grant_type=client_credentials"
            if request.headers["authorization"] != BASIC or self.token_status != 200:
                return httpx.Response(self.token_status if self.token_status != 200 else 401,
                                      json={"error": "invalid_client", "error_description": "Bad client credentials"})
            self.tokens += 1
            return httpx.Response(200, json={"access_token": f"{TOKEN}-{self.tokens}", "token_type": "bearer",
                                             "expires_in": self.expires_in, "sub": CLIENT_ID})
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
            return httpx.Response(step, headers={"Retry-After": "1"} if step == 429 else {})
        assert request.headers["authorization"].startswith(f"Bearer {TOKEN}-")
        assert request.url.params["locale"] == "en_US"
        modified = email.utils.formatdate(LAST_MODIFIED, usegmt=True)
        if request.headers.get("if-modified-since") == modified:
            return httpx.Response(304, headers={"Last-Modified": modified})
        path, ns = request.url.path, request.headers["battlenet-namespace"]
        if ns == "dynamic-forever-eu" and path == "/data/wow/ruleset/index":
            body = {"rulesets": [{"key": {"href": "..."}, "id": 15}, {"id": 1}]}
        elif ns == "dynamic-forever-eu" and path in ("/data/wow/ruleset/1", "/data/wow/ruleset/15"):
            body = {"id": int(path[-2:].strip("/")), "name": "PvP" if path.endswith("/1") else "Roleplay PvP"}
        elif ns == "dynamic-forever-eu" and path == "/data/wow/ruleset/1/auctions/":
            body = {"auctions": [{"key": {"href": "..."}, "name": "Alliance Auction House", "id": 110},
                                 {"name": "Blackwater Auction House", "id": 116}]}
        elif ns == "dynamic-forever-eu" and path == "/data/wow/ruleset/1/auctions/110":
            body = {"id": 110, "name": "Alliance Auction House", "auctions": [
                {"id": 1, "item": {"id": 2589}, "buyout": 400, "quantity": 1, "time_left": "LONG"}]}
        elif ns == "dynamic-forever-eu" and path == "/data/wow/ruleset/1/auctions/110/commodities":
            body = {"auctions": [
                {"id": 2, "item": {"id": 2592}, "quantity": 20, "unit_price": 60, "time_left": "SHORT"}]}
        else:
            return httpx.Response(404)
        return httpx.Response(200, json=body, headers={"Last-Modified": modified})

    def api_paths(self) -> list[str]:
        return [r.url.path for r in self.requests if r.url.host != "oauth.battle.net"]


@pytest.fixture
def fake():
    return Fake()


def client(fake: Fake, clock: Clock | None = None, config: battlenet.Config = CONFIG, waits: list | None = None):
    return battlenet.Client(config, "EU", transport=httpx.MockTransport(fake), clock=clock or Clock(),
                            sleep=(waits.append if waits is not None else lambda s: None))


def test_config_file_and_environment(tmp_path, monkeypatch):
    path = tmp_path / "battlenet.env"
    path.write_text(f"# Blizzard API client\nexport BATTLENET_CLIENT_ID='{CLIENT_ID}'\n"
                    f"BATTLENET_CLIENT_SECRET=\"{SECRET}\"\nBATTLENET_AUCTION_HOUSE=110\n\nnot a setting\n")
    config = battlenet.load_config(path, environ={})
    assert (config.client_id, config.client_secret, config.auction_house, config.region) == (
        CLIENT_ID, SECRET, 110, None)
    assert config.has_credentials and SECRET not in repr(config) and SECRET not in str(config)
    # The environment's variables win; empty ones don't count.
    config = battlenet.load_config(path, environ={"BATTLENET_CLIENT_ID": "other", "BATTLENET_REGION": "US",
                                                  "BATTLENET_CLIENT_SECRET": ""})
    assert (config.client_id, config.client_secret, config.region) == ("other", SECRET, "us")
    with pytest.raises(battlenet.BattleNetError, match="BATTLENET_AUCTION_HOUSE .* must be an auction house ID"):
        battlenet.load_config(path, environ={"BATTLENET_AUCTION_HOUSE": "Alliance"})
    with pytest.raises(battlenet.BattleNetError, match="BATTLENET_REGION .* one of us, eu, kr, tw"):
        battlenet.load_config(path, environ={"BATTLENET_REGION": "cn"})


def test_missing_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME")
    config = battlenet.load_config(environ={})
    assert config.path == tmp_path / ".config" / "wow-mcp" / "battlenet.env" and not config.has_credentials
    with pytest.raises(battlenet.MissingCredentials) as e:
        battlenet.Client(config, "eu")
    assert str(e.value) == (
        "No Blizzard API credentials (BATTLENET_CLIENT_ID and BATTLENET_CLIENT_SECRET missing): set up "
        "~/.config/wow-mcp/battlenet.env with BATTLENET_CLIENT_ID=... and BATTLENET_CLIENT_SECRET=... (a client from "
        "https://develop.battle.net/access/clients; chmod 600 the file).")
    with pytest.raises(battlenet.MissingCredentials, match="BATTLENET_CLIENT_SECRET missing"):
        battlenet.Config(CLIENT_ID).require()


def test_token_is_kept_until_it_expires(fake):
    clock = Clock()
    api = client(fake, clock)
    first = api.token()
    assert fake.tokens == 1 and api.token() == first
    api.get("/data/wow/ruleset/1")
    assert fake.tokens == 1, "one token for many requests"
    clock.now += 86399 - battlenet.TOKEN_MARGIN - 1
    assert api.token() == first and fake.tokens == 1
    clock.now += 2  # within the margin before it expires: renewed
    assert api.token() != first and fake.tokens == 2
    # A token the API turns down early is renewed once.
    fake.script = [401]
    assert api.get("/data/wow/ruleset/1").data["name"] == "PvP" and fake.tokens == 3


def test_requests_and_conditional_gets(fake):
    api = client(fake)
    assert repr(api) == "Client(region='eu')" and api.namespace("static") == "static-forever-eu"
    assert api.rulesets() == [{"id": 1, "name": "PvP"}, {"id": 15, "name": "Roleplay PvP"}]
    assert api.auction_houses(1) == [{"id": 110, "name": "Alliance Auction House"},
                                     {"id": 116, "name": "Blackwater Auction House"}]
    assert "/data/wow/ruleset/1/auctions/" in fake.api_paths(), "the index only answers with the trailing slash"
    with pytest.raises(battlenet.NotFound, match="isn't in Blizzard's API"):
        api.get("/data/wow/ruleset/1/auctions")
    auctions = api.auctions(1, 110)
    assert auctions.last_modified == LAST_MODIFIED and auctions.data["auctions"][0]["buyout"] == 400
    assert auctions.size == len(b'{"id":110,"name":"Alliance Auction House","auctions":[{"id":1,"item":{"id":2589},'
                                b'"buyout":400,"quantity":1,"time_left":"LONG"}]}')
    # Unchanged since that Last-Modified: a 304, no body.
    assert api.auctions(1, 110, since=LAST_MODIFIED) is None
    assert fake.requests[-1].headers["if-modified-since"] == "Tue, 10 Nov 2026 23:13:36 GMT"
    assert api.commodities(1, 110, since=LAST_MODIFIED - 3600).data["auctions"][0]["unit_price"] == 60
    assert fake.api_paths()[-1] == "/data/wow/ruleset/1/auctions/110/commodities"


def test_retries(fake):
    waits: list[float] = []
    api = client(fake, waits=waits)
    # Server errors and network errors are tried again, waiting longer each time.
    fake.script = [503, httpx.ConnectError("connection refused")]
    assert api.get("/data/wow/ruleset/1").data["id"] == 1 and waits == [2.0, 4.0]
    # Too many requests: as long as Blizzard says.
    fake.script = [429]
    waits.clear()
    assert api.get("/data/wow/ruleset/1").data["id"] == 1 and waits == [1.0]
    fake.script = [500, 500, 500]
    with pytest.raises(battlenet.BattleNetError, match=r"didn't answer for /data/wow/ruleset/1 \(HTTP 500; tried 3"):
        api.get("/data/wow/ruleset/1")
    fake.script = [418]
    with pytest.raises(battlenet.BattleNetError, match="answered HTTP 418 for /data/wow/ruleset/1"):
        api.get("/data/wow/ruleset/1")


def test_the_secret_and_token_never_show(fake, caplog, capsys):
    caplog.set_level(logging.DEBUG)  # every logger, httpx2's too
    api = client(fake)
    problems = []
    api.rulesets()
    api.auctions(1, 110, since=LAST_MODIFIED)
    fake.script = [500, 500, 500]
    with pytest.raises(battlenet.BattleNetError) as e:
        api.get("/data/wow/ruleset/1")
    problems.append(e.value)
    fake.token_status = 401
    refused = client(fake)
    with pytest.raises(battlenet.BattleNetError) as e:
        refused.token()
    problems.append(e.value)
    assert str(e.value).startswith("Blizzard refused the API credentials (HTTP 401): check BATTLENET_CLIENT_ID and "
                                   "BATTLENET_CLIENT_SECRET in ") and str(e.value).endswith("/wow-mcp/battlenet.env.")
    for bad in (httpx.ReadTimeout("timed out"), httpx.ConnectError("refused")):
        fake.token_status, fake.script = 200, [bad] * 3
        with pytest.raises(battlenet.BattleNetError) as e:
            client(fake).get("/data/wow/ruleset/1")
        problems.append(e.value)
    out = capsys.readouterr()
    shown = "\n".join([caplog.text, out.out, out.err, repr(api), repr(CONFIG), str(CONFIG), *map(str, problems),
                       *map(repr, problems), *(repr(p.__cause__) for p in problems),
                       *(repr(p.__context__) for p in problems)])
    assert caplog.records, "requests were logged"
    assert SECRET not in shown and TOKEN not in shown and CLIENT_ID + ":" not in shown
    assert BASIC.split()[1] not in shown


def test_http_dates():
    assert battlenet.parse_http_date("Tue, 10 Nov 2026 21:13:40 GMT") == 1794345220
    assert battlenet.parse_http_date("yesterday") is None and battlenet.parse_http_date(None) is None
