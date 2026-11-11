"""Blizzard's Game Data API, for World of Warcraft: Forever's auction houses (online_auctions.py).

Forever's data has been there since 2026-11-06 but isn't documented. As checked against the live API:
- OAuth client credentials at oauth.battle.net. The namespaces are dynamic-forever-{region} (rulesets, auctions)
  and static-forever-{region} (game data, which lacks many items, so names and stats come from the game).
- Rulesets (PvP, Normal, Roleplay, Roleplay PvP) take the place of realms; the realm indexes are 404s.
- /data/wow/ruleset/{id}/auctions/ lists the auction houses, and only with the trailing slash. Every ruleset lists
  the same 15 houses with the same auctions. A house has /auctions/{house} (single items: a buyout per auction) and
  /auctions/{house}/commodities (stackables: a unit price per auction).
- A house's snapshot changes about once an hour; with If-Modified-Since an unchanged one is a 304 with no body.

The credentials come from ~/.config/wow-mcp/battlenet.env (BATTLENET_CLIENT_ID=..., BATTLENET_CLIENT_SECRET=...)
or from environment variables of the same names, which win. The secret and the access token only ever go to
Blizzard: they're kept out of reprs, logs and error messages.
"""

import email.utils
import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2 as httpx

log = logging.getLogger("wow_mcp.battlenet")

ID_KEY = "BATTLENET_CLIENT_ID"
SECRET_KEY = "BATTLENET_CLIENT_SECRET"
HOUSE_KEY = "BATTLENET_AUCTION_HOUSE"  # the player's auction house, instead of matching it
REGION_KEY = "BATTLENET_REGION"  # instead of the game install's region
KEYS = (ID_KEY, SECRET_KEY, HOUSE_KEY, REGION_KEY)
CONFIG_NAME = "battlenet.env"
REGIONS = ("us", "eu", "kr", "tw")

TOKEN_URL = "https://oauth.battle.net/token"
API_URL = "https://{region}.api.blizzard.com"
LOCALE = "en_US"
USER_AGENT = "wow-mcp"
TIMEOUT = httpx.Timeout(60.0, connect=10.0)  # a busy house is ~18 MB of JSON (under 2 MB gzipped)
ATTEMPTS = 3
RETRY_WAIT = 2.0  # seconds before the second try, doubled for each one after
MAX_RETRY_WAIT = 30.0
RETRY_STATUS = {429, 500, 502, 503, 504}
TOKEN_MARGIN = 300  # seconds before it expires that a token is renewed (they last a day)


class BattleNetError(Exception):
    """Blizzard's API couldn't be used. The message says why, and never has the secret or a token in it."""


class MissingCredentials(BattleNetError):
    pass


class NotFound(BattleNetError):
    pass


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "wow-mcp" / CONFIG_NAME


def shown(path: Path) -> str:
    """A path as the player would write it (~ for their home folder; / also on Windows, which takes both)."""
    home = Path.home().as_posix()
    text = path.as_posix()
    return "~" + text[len(home):] if text == home or text.startswith(home + "/") else text


@dataclass(frozen=True)
class Config:
    client_id: str | None = None
    client_secret: str | None = field(default=None, repr=False)
    auction_house: int | None = None
    region: str | None = None
    path: Path | None = None

    @property
    def has_credentials(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def setup_hint(self) -> str:
        return (f"set up {shown(self.path or config_path())} with {ID_KEY}=... and {SECRET_KEY}=... (a client from "
                "https://develop.battle.net/access/clients; chmod 600 the file)")

    def require(self) -> "Config":
        if not self.has_credentials:
            missing = " and ".join(k for k, v in ((ID_KEY, self.client_id), (SECRET_KEY, self.client_secret)) if not v)
            raise MissingCredentials(f"No Blizzard API credentials ({missing} missing): {self.setup_hint()}.")
        return self


def _read_env_file(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise BattleNetError(f"Can't read {shown(path)}: {e.strerror}.") from None
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.removeprefix("export ").partition("=")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_config(path: Path | None = None, environ: Mapping[str, str] = os.environ) -> Config:
    """The settings in battlenet.env, with the environment's variables of the same names over them."""
    path = path or config_path()
    values = _read_env_file(path)
    values.update({key: environ[key] for key in KEYS if environ.get(key)})
    house = values.get(HOUSE_KEY, "").strip() or None
    if house is not None:
        if not house.isdigit() or int(house) <= 0:
            raise BattleNetError(f"{HOUSE_KEY} in {shown(path)} must be an auction house ID, e.g. 110 "
                                 "(wow-mcp-auctions houses lists them).")
        house = int(house)
    region = values.get(REGION_KEY, "").strip().lower() or None
    if region is not None and region not in REGIONS:
        raise BattleNetError(f"{REGION_KEY} in {shown(path)} must be one of {', '.join(REGIONS)}.")
    return Config(values.get(ID_KEY) or None, values.get(SECRET_KEY) or None, house, region, path)


def parse_http_date(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return email.utils.parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        return None


@dataclass
class Response:
    data: Any
    last_modified: float | None  # unix time: when Blizzard took the snapshot
    size: int  # bytes of JSON
    downloaded: int  # bytes on the wire (gzipped)


class Client:
    """Blizzard's API for one region: the access token is fetched when needed and kept until shortly before it
    expires; requests are retried on network errors, 429 and 5xx."""

    def __init__(self, config: Config, region: str, transport: httpx.BaseTransport | None = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 attempts: int = ATTEMPTS):
        config.require()
        if region.lower() not in REGIONS:
            raise BattleNetError(f"Blizzard's API has no region {region!r} (one of {', '.join(REGIONS)}).")
        self.region = region.lower()
        self._id, self._secret, self._hint = config.client_id, config.client_secret, shown(config.path or config_path())
        self._token: str | None = None
        self._expires = 0.0
        self.clock, self.sleep, self.attempts = clock, sleep, attempts
        self.http = httpx.Client(transport=transport, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})

    def __repr__(self) -> str:
        return f"Client(region={self.region!r})"

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def namespace(self, kind: str = "dynamic") -> str:
        return f"{kind}-forever-{self.region}"

    def token(self) -> str:
        if self._token and self.clock() < self._expires:
            return self._token
        response = self._send("POST", TOKEN_URL, "an access token", data={"grant_type": "client_credentials"},
                              auth=httpx.BasicAuth(self._id, self._secret))
        if response.status_code in (400, 401, 403):
            raise BattleNetError(f"Blizzard refused the API credentials (HTTP {response.status_code}): check {ID_KEY} "
                                 f"and {SECRET_KEY} in {self._hint}.")
        if response.status_code != 200:
            raise BattleNetError(f"Blizzard didn't give an access token (HTTP {response.status_code}).")
        try:
            body = response.json()
            token, lasts = body["access_token"], float(body.get("expires_in") or 0)
        except (ValueError, KeyError, TypeError):
            raise BattleNetError("Blizzard's answer with the access token wasn't understood.") from None
        if not isinstance(token, str) or not token:
            raise BattleNetError("Blizzard's answer had no access token.")
        self._token, self._expires = token, self.clock() + max(0.0, lasts - TOKEN_MARGIN)
        return token

    def get(self, path: str, namespace: str = "dynamic", since: float | None = None) -> Response | None:
        """An API path's JSON, or None if it hasn't changed since `since` (the Last-Modified it had)."""
        url = API_URL.format(region=self.region) + path
        for renewed in (False, True):
            headers = {"Authorization": f"Bearer {self.token()}", "Battlenet-Namespace": self.namespace(namespace)}
            if since:
                headers["If-Modified-Since"] = email.utils.formatdate(since, usegmt=True)
            response = self._send("GET", url, path, headers=headers, params={"locale": LOCALE})
            if response.status_code != 401 or renewed:
                break
            self._token = None  # revoked or expired early: one more try with a new one
        status = response.status_code
        if status == 304:
            return None
        if status == 404:
            raise NotFound(f"{path} isn't in Blizzard's API ({self.namespace(namespace)}).")
        if status != 200:
            raise BattleNetError(f"Blizzard's API answered HTTP {status} for {path}.")
        try:
            data = response.json()
        except ValueError:
            raise BattleNetError(f"Blizzard's answer for {path} wasn't JSON.") from None
        return Response(data, parse_http_date(response.headers.get("Last-Modified")), len(response.content),
                        response.num_bytes_downloaded)

    def _send(self, method: str, url: str, what: str, **options: Any) -> httpx.Response:
        for attempt in range(1, self.attempts + 1):
            wait = RETRY_WAIT * 2 ** (attempt - 1)
            try:
                response = self.http.request(method, url, **options)
            except httpx.RequestError as e:  # network errors, timeouts, a broken gzip stream
                problem = type(e).__name__
            else:
                if response.status_code not in RETRY_STATUS:
                    return response
                problem = f"HTTP {response.status_code}"
                retry_after = response.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    wait = float(retry_after)
            if attempt == self.attempts:
                raise BattleNetError(f"Blizzard's API didn't answer for {what} ({problem}; tried {attempt} times).")
            wait = min(wait, MAX_RETRY_WAIT)
            log.info("%s: %s, trying again in %.0f s", what, problem, wait)
            self.sleep(wait)
        raise AssertionError("unreachable")

    # The endpoints ---------------------------------------------------------------------------

    def rulesets(self) -> list[dict]:
        """[{id, name}], e.g. 1 "PvP"."""
        index = self.get("/data/wow/ruleset/index")
        found = []
        for entry in index.data.get("rulesets") or []:
            if isinstance(entry, dict) and isinstance(entry.get("id"), int):
                detail = self.get(f"/data/wow/ruleset/{entry['id']}")
                found.append({"id": entry["id"], "name": str(detail.data.get("name") or entry["id"])})
        return sorted(found, key=lambda r: r["id"])

    def auction_houses(self, ruleset: int) -> list[dict]:
        """[{id, name}], e.g. 110 "Alliance Auction House"."""
        index = self.get(f"/data/wow/ruleset/{ruleset}/auctions/")  # without the trailing slash, a 404
        return [{"id": a["id"], "name": str(a.get("name") or "")} for a in index.data.get("auctions") or []
                if isinstance(a, dict) and isinstance(a.get("id"), int)]

    def auctions(self, ruleset: int, house: int, since: float | None = None) -> Response | None:
        """{id, name, auctions: [{id, item: {id, context?, bonus_lists?, modifiers?}, buyout?, bid?, quantity,
        time_left}], commodities, ruleset}: the single items, a buyout per auction."""
        return self.get(f"/data/wow/ruleset/{ruleset}/auctions/{house}", since=since)

    def commodities(self, ruleset: int, house: int, since: float | None = None) -> Response | None:
        """{auctions: [{id, item: {id}, quantity, unit_price, time_left}]}: stackables, a price per item."""
        return self.get(f"/data/wow/ruleset/{ruleset}/auctions/{house}/commodities", since=since)
