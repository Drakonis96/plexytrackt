#!/usr/bin/env python3
"""
PlexyTrackt – Synchronizes Plex watched history with Trakt.

• Compatible with PlexAPI ≥ 4.15
• Safe conversion of ``viewedAt`` (datetime, numeric timestamp or string)
• Handles movies without year (``year == None``) to avoid Plex 500 errors
• Replaced ``searchShows`` (removed in PlexAPI ≥ 4.14) with generic search ``libtype="show"``
"""

import os
import json
import logging
from datetime import datetime, timezone
from numbers import Number
from typing import Dict, List, Optional, Set, Tuple, Union
import time

import requests
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    has_request_context,
)
from flask import send_file
from apscheduler.schedulers.background import BackgroundScheduler
from plexapi.server import PlexServer
from plexapi.exceptions import BadRequest, NotFound

# --------------------------------------------------------------------------- #
# LOGGING
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# FLASK + APSCHEDULER
# --------------------------------------------------------------------------- #
app = Flask(__name__)

SYNC_INTERVAL_MINUTES = 60  # default frequency
SYNC_COLLECTION = False
SYNC_RATINGS = True
SYNC_WATCHED = True  # ahora sí se respeta este flag
SYNC_LIKED_LISTS = False
SYNC_WATCHLISTS = False
LIVE_SYNC = False
SYNC_PROVIDER = "none"  # trakt | simkl | none
PROVIDER_FILE = "provider.json"
scheduler = BackgroundScheduler()
plex = None  # will hold PlexServer instance

# --------------------------------------------------------------------------- #
# TRAKT / SIMKL OAUTH CONSTANTS
# --------------------------------------------------------------------------- #
TRAKT_REDIRECT_URI = os.environ.get("TRAKT_REDIRECT_URI")
TOKEN_FILE = "trakt_tokens.json"

SIMKL_REDIRECT_URI = os.environ.get("SIMKL_REDIRECT_URI")
SIMKL_TOKEN_FILE = "simkl_tokens.json"


# --------------------------------------------------------------------------- #
# PROVIDER SELECTION
# --------------------------------------------------------------------------- #
def load_provider() -> None:
    """Load selected sync provider from file."""
    global SYNC_PROVIDER
    if os.path.exists(PROVIDER_FILE):
        try:
            with open(PROVIDER_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            SYNC_PROVIDER = data.get("provider", "none")
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load provider: %s", exc)


def save_provider(provider: str) -> None:
    """Persist selected sync provider to file."""
    global SYNC_PROVIDER
    SYNC_PROVIDER = provider
    try:
        with open(PROVIDER_FILE, "w", encoding="utf-8") as f:
            json.dump({"provider": provider}, f, indent=2)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to save provider: %s", exc)


# --------------------------------------------------------------------------- #
# CUSTOM EXCEPTIONS
# --------------------------------------------------------------------------- #
class TraktAccountLimitError(Exception):
    """Raised when Trakt returns HTTP 420 (account limit exceeded)."""

    pass


def get_trakt_redirect_uri() -> str:
    """Return the Trakt redirect URI derived from the current request."""
    global TRAKT_REDIRECT_URI
    if TRAKT_REDIRECT_URI:
        return TRAKT_REDIRECT_URI
    if has_request_context():
        TRAKT_REDIRECT_URI = request.url_root.rstrip("/") + "/oauth/trakt"
        return TRAKT_REDIRECT_URI
    return "http://localhost:5030/oauth/trakt"


def get_simkl_redirect_uri() -> str:
    """Return the Simkl redirect URI derived from the current request."""
    global SIMKL_REDIRECT_URI
    if SIMKL_REDIRECT_URI:
        return SIMKL_REDIRECT_URI
    if has_request_context():
        SIMKL_REDIRECT_URI = request.url_root.rstrip("/") + "/oauth/simkl"
        return SIMKL_REDIRECT_URI
    return "http://localhost:5030/oauth/simkl"


# --------------------------------------------------------------------------- #
# UTILITIES
# --------------------------------------------------------------------------- #
def to_iso_z(value) -> Optional[str]:
    """Convert any ``viewedAt`` variant to ISO-8601 UTC ("...Z")."""
    if value is None:
        return None

    if isinstance(value, datetime):  # datetime / pendulum / arrow
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    if isinstance(value, Number):  # int / float
        return datetime.utcfromtimestamp(value).isoformat() + "Z"

    if isinstance(value, str):  # str
        try:  # integer as text
            return datetime.utcfromtimestamp(int(value)).isoformat() + "Z"
        except (TypeError, ValueError):
            pass
        try:  # ISO string
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass

    logger.warning("Unrecognized viewedAt format: %r (%s)", value, type(value))
    return None


def normalize_year(value: Union[str, int, None]) -> Optional[int]:
    """Return ``value`` as ``int`` if possible, otherwise ``None``."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.debug("Invalid year value: %r", value)
        return None


def _parse_guid_value(raw: str) -> Optional[str]:
    """Convert a raw Plex GUID string to a known imdb/tmdb/tvdb prefix."""
    if raw.startswith("imdb://"):
        return raw.split("?", 1)[0]
    if raw.startswith("tmdb://"):
        return raw.split("?", 1)[0]
    if raw.startswith("tvdb://"):
        return raw.split("?", 1)[0]
    if "themoviedb://" in raw:
        val = raw.split("themoviedb://", 1)[1].split("?", 1)[0]
        return f"tmdb://{val.split('/')[0]}"
    if "thetvdb://" in raw:
        val = raw.split("thetvdb://", 1)[1].split("?", 1)[0]
        return f"tvdb://{val.split('/')[0]}"
    if "imdb://" in raw:
        val = raw.split("imdb://", 1)[1].split("?", 1)[0]
        return f"imdb://{val.split('/')[0]}"
    return None


def best_guid(item) -> Optional[str]:
    """Return a preferred GUID (imdb → tmdb → tvdb) for a Plex item."""
    try:
        for g in getattr(item, "guids", []) or []:
            val = _parse_guid_value(g.id)
            if val and val.startswith("imdb://"):
                return val
        for g in getattr(item, "guids", []) or []:
            val = _parse_guid_value(g.id)
            if val and (val.startswith("tmdb://") or val.startswith("tvdb://")):
                return val
        if getattr(item, "guid", None):
            return _parse_guid_value(item.guid)
    except Exception as exc:
        logger.debug("Failed retrieving GUIDs: %s", exc)
    return None


def imdb_guid(item) -> Optional[str]:
    """Return an IMDb, TMDb or TVDb GUID for a Plex item."""
    try:
        for g in getattr(item, "guids", []) or []:
            val = _parse_guid_value(g.id)
            if val and val.startswith("imdb://"):
                return val
        for g in getattr(item, "guids", []) or []:
            val = _parse_guid_value(g.id)
            if val and val.startswith("tmdb://"):
                return val
        for g in getattr(item, "guids", []) or []:
            val = _parse_guid_value(g.id)
            if val and val.startswith("tvdb://"):
                return val
        if getattr(item, "guid", None):
            val = _parse_guid_value(item.guid)
            if val and val.startswith("imdb://"):
                return val
            if val and val.startswith("tmdb://"):
                return val
            if val and val.startswith("tvdb://"):
                return val
    except Exception as exc:
        logger.debug("Failed retrieving IMDb GUID: %s", exc)
    return None


def get_show_from_library(plex, title):
    """Return a show object from any library section."""
    for sec in plex.library.sections():
        if sec.type == "show":
            try:
                return sec.get(title)
            except NotFound:
                continue
    return None


def find_item_by_guid(plex, guid):
    """Return a movie or show from Plex matching the given GUID."""
    for sec in plex.library.sections():
        if sec.type in ("movie", "show"):
            try:
                return sec.getGuid(guid)
            except Exception:
                continue
    return None


def ensure_collection(plex, section, name, first_item=None):
    """Return a Plex collection with ``name`` creating it if needed."""
    try:
        return section.collection(name)
    except Exception:
        if first_item is None:
            raise
        return plex.createCollection(name, section, items=[first_item])


def movie_key(
    title: str, year: Optional[int], guid: Optional[str]
) -> Union[str, Tuple[str, Optional[int]]]:
    """Return a unique key for comparing movies."""
    if guid:
        return guid
    return (title, year)


def guid_to_ids(guid: str) -> Dict[str, Union[str, int]]:
    """Convert a Plex GUID to a Trakt ids mapping."""
    if guid.startswith("imdb://"):
        return {"imdb": guid.split("imdb://", 1)[1]}
    if guid.startswith("tmdb://"):
        return {"tmdb": int(guid.split("tmdb://", 1)[1])}
    if guid.startswith("tvdb://"):
        return {"tvdb": int(guid.split("tvdb://", 1)[1])}
    return {}


def valid_guid(guid: Optional[str]) -> bool:
    """Return True if ``guid`` is a valid IMDb, TMDb or TVDb identifier."""
    return bool(guid) and guid.startswith(("imdb://", "tmdb://", "tvdb://"))


def trakt_movie_key(m: dict) -> Union[str, Tuple[str, Optional[int]]]:
    """Return a unique key for a Trakt movie object using IMDb or TMDb."""
    ids = m.get("ids", {})
    if ids.get("imdb"):
        return f"imdb://{ids['imdb']}"
    if ids.get("tmdb"):
        return f"tmdb://{ids['tmdb']}"
    return m["title"].lower()


def episode_key(
    show: str, code: str, guid: Optional[str]
) -> Union[str, Tuple[str, str]]:
    if guid and (guid.startswith("imdb://") or guid.startswith("tmdb://")):
        return guid
    return (show.lower(), code)


def trakt_episode_key(show: dict, e: dict) -> Union[str, Tuple[str, str]]:
    ids = e.get("ids", {})
    if ids.get("imdb"):
        return f"imdb://{ids['imdb']}"
    if ids.get("tmdb"):
        return f"tmdb://{ids['tmdb']}"
    return (
        show["title"].lower(),
        f"S{e['season']:02d}E{e['number']:02d}",
    )


def simkl_episode_key(show: dict, e: dict) -> Union[str, Tuple[str, str]]:
    """Return a unique key for a Simkl episode object using ONLY TVDb IDs."""
    ep_ids = e.get("ids", {})
    if ep_ids.get("tvdb"):
        return f"tvdb://{ep_ids['tvdb']}"
    show_data = show.get("show", show)
    show_ids = show_data.get("ids", {})
    if show_ids.get("tvdb"):
        show_guid = f"tvdb://{show_ids['tvdb']}"
        return f"{show_guid}:S{e['season']:02d}E{e['number']:02d}"
    # Si no hay TVDb, no hacemos matching
    return None



# --------------------------------------------------------------------------- #
# TRAKT TOKENS
# --------------------------------------------------------------------------- #
def load_trakt_tokens() -> None:
    if os.environ.get("TRAKT_ACCESS_TOKEN") and os.environ.get("TRAKT_REFRESH_TOKEN"):
        return
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            os.environ["TRAKT_ACCESS_TOKEN"] = data.get("access_token", "")
            os.environ["TRAKT_REFRESH_TOKEN"] = data.get("refresh_token", "")
            logger.info("Loaded Trakt tokens from %s", TOKEN_FILE)
        except Exception as exc:
            logger.error("Failed to load Trakt tokens: %s", exc)


def save_trakt_tokens(access_token: str, refresh_token: Optional[str]) -> None:
    try:
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {"access_token": access_token, "refresh_token": refresh_token},
                f,
                indent=2,
            )
        logger.info("Saved Trakt tokens to %s", TOKEN_FILE)
    except Exception as exc:
        logger.error("Failed to save Trakt tokens: %s", exc)


def exchange_code_for_tokens(code: str) -> Optional[dict]:
    client_id = os.environ.get("TRAKT_CLIENT_ID")
    client_secret = os.environ.get("TRAKT_CLIENT_SECRET")
    if not all([code, client_id, client_secret]):
        logger.error("Missing code or Trakt client credentials.")
        return None

    payload = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": get_trakt_redirect_uri(),
        "grant_type": "authorization_code",
    }
    try:
        resp = requests.post(
            "https://api.trakt.tv/oauth/token", json=payload, timeout=30
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.error("Failed to obtain Trakt tokens: %s", exc)
        return None

    data = resp.json()
    os.environ["TRAKT_ACCESS_TOKEN"] = data["access_token"]
    os.environ["TRAKT_REFRESH_TOKEN"] = data.get("refresh_token")
    save_trakt_tokens(data["access_token"], data.get("refresh_token"))
    logger.info("Trakt tokens obtained via authorization code")
    return data


def refresh_trakt_token() -> Optional[str]:
    refresh_token = os.environ.get("TRAKT_REFRESH_TOKEN")
    client_id = os.environ.get("TRAKT_CLIENT_ID")
    client_secret = os.environ.get("TRAKT_CLIENT_SECRET")
    if not all([refresh_token, client_id, client_secret]):
        logger.error("Missing Trakt OAuth environment variables.")
        return None

    payload = {
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": get_trakt_redirect_uri(),
        "grant_type": "refresh_token",
    }
    try:
        resp = requests.post(
            "https://api.trakt.tv/oauth/token", json=payload, timeout=30
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.error("Failed to refresh Trakt token: %s", exc)
        return None

    data = resp.json()
    os.environ["TRAKT_ACCESS_TOKEN"] = data["access_token"]
    os.environ["TRAKT_REFRESH_TOKEN"] = data.get("refresh_token", refresh_token)
    save_trakt_tokens(data["access_token"], os.environ["TRAKT_REFRESH_TOKEN"])
    logger.info("Trakt access token refreshed")
    return data["access_token"]


def load_simkl_tokens() -> None:
    if os.environ.get("SIMKL_ACCESS_TOKEN"):
        return
    if os.path.exists(SIMKL_TOKEN_FILE):
        try:
            with open(SIMKL_TOKEN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            os.environ["SIMKL_ACCESS_TOKEN"] = data.get("access_token", "")
            logger.info("Loaded Simkl token from %s", SIMKL_TOKEN_FILE)
        except Exception as exc:
            logger.error("Failed to load Simkl token: %s", exc)


def save_simkl_token(access_token: str) -> None:
    try:
        with open(SIMKL_TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump({"access_token": access_token}, f, indent=2)
        logger.info("Saved Simkl token to %s", SIMKL_TOKEN_FILE)
    except Exception as exc:
        logger.error("Failed to save Simkl token: %s", exc)


def exchange_code_for_simkl_tokens(code: str) -> Optional[dict]:
    client_id = os.environ.get("SIMKL_CLIENT_ID")
    client_secret = os.environ.get("SIMKL_CLIENT_SECRET")
    if not all([code, client_id, client_secret]):
        logger.error("Missing code or Simkl client credentials.")
        return None

    payload = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": get_simkl_redirect_uri(),
        "grant_type": "authorization_code",
    }
    try:
        resp = requests.post(
            "https://api.simkl.com/oauth/token", json=payload, timeout=30
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.error("Failed to obtain Simkl token: %s", exc)
        return None

    data = resp.json()
    os.environ["SIMKL_ACCESS_TOKEN"] = data["access_token"]
    save_simkl_token(data["access_token"])
    logger.info("Simkl token obtained via authorization code")
    return data


def simkl_request(
    method: str, endpoint: str, headers: dict, **kwargs
) -> requests.Response:
    url = f"https://api.simkl.com{endpoint}"
    resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)
    resp.raise_for_status()
    return resp


def trakt_request(
    method: str, endpoint: str, headers: dict, **kwargs
) -> requests.Response:
    url = f"https://api.trakt.tv{endpoint}"
    resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)
    if resp.status_code == 401:  # token expired
        new_token = refresh_trakt_token()
        if new_token:
            headers["Authorization"] = f"Bearer {new_token}"
            resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)

    if resp.status_code == 420:
        msg = (
            "Trakt API returned 420 – account limit exceeded. "
            "Upgrade to VIP or reduce the size of your collection/watchlist."
        )
        logger.warning(msg)
        raise TraktAccountLimitError(msg)

    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", "1"))
        logger.warning(
            "Trakt API rate limit reached. Retrying in %s seconds", retry_after
        )
        time.sleep(retry_after)
        resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)

    resp.raise_for_status()
    return resp


# --------------------------------------------------------------------------- #
# PLEX ↔ TRAKT
# --------------------------------------------------------------------------- #
def get_plex_history(plex) -> Tuple[
    Dict[str, Dict[str, Optional[str]]],
    Dict[str, Dict[str, Optional[str]]],
]:
    """Return watched movies and episodes from Plex keyed by IMDb or TMDb GUID."""
    movies: Dict[str, Dict[str, Optional[str]]] = {}
    episodes: Dict[str, Dict[str, Optional[str]]] = {}

    logger.info("Fetching Plex history…")
    for entry in plex.history():
        watched_at = to_iso_z(getattr(entry, "viewedAt", None))

        # Movies
        if entry.type == "movie":
            try:
                item = entry.source() or plex.fetchItem(entry.ratingKey)
            except Exception as exc:
                logger.debug(
                    "Failed to fetch movie %s from Plex: %s", entry.ratingKey, exc
                )
                continue

            title = item.title
            year = normalize_year(getattr(item, "year", None))
            guid = imdb_guid(item)
            if not guid:
                continue
            if guid not in movies:
                movies[guid] = {
                    "title": title,
                    "year": year,
                    "watched_at": watched_at,
                    "guid": guid,
                }

        # Episodes
        elif entry.type == "episode":
            season = getattr(entry, "parentIndex", None)
            number = getattr(entry, "index", None)
            show = getattr(entry, "grandparentTitle", None)
            try:
                item = entry.source() or plex.fetchItem(entry.ratingKey)
            except Exception as exc:
                logger.debug(
                    "Failed to fetch episode %s from Plex: %s", entry.ratingKey, exc
                )
                item = None
            if item:
                season = season or item.seasonNumber
                number = number or item.index
                show = show or item.grandparentTitle
                guid = imdb_guid(item)
            else:
                guid = None
            if None in (season, number, show):
                continue
            code = f"S{int(season):02d}E{int(number):02d}"
            if guid and guid not in episodes:
                episodes[guid] = {
                    "show": show,
                    "code": code,
                    "watched_at": watched_at,
                    "guid": guid,
                }

    logger.info("Fetching watched flags from Plex library…")
    for section in plex.library.sections():
        try:
            if section.type == "movie":
                for item in section.search(viewCount__gt=0):
                    title = item.title
                    year = normalize_year(getattr(item, "year", None))
                    guid = imdb_guid(item)
                    if guid and guid not in movies:
                        movies[guid] = {
                            "title": title,
                            "year": year,
                            "watched_at": to_iso_z(getattr(item, "lastViewedAt", None)),
                            "guid": guid,
                        }
            elif section.type == "show":
                for ep in section.searchEpisodes(viewCount__gt=0):
                    code = f"S{int(ep.seasonNumber):02d}E{int(ep.episodeNumber):02d}"
                    guid = imdb_guid(ep)
                    if guid and guid not in episodes:
                        episodes[guid] = {
                            "show": ep.grandparentTitle,
                            "code": code,
                            "watched_at": to_iso_z(getattr(ep, "lastViewedAt", None)),
                            "guid": guid,
                        }
        except Exception as exc:
            logger.debug(
                "Failed fetching watched items from section %s: %s", section.title, exc
            )

    return movies, episodes


def get_trakt_history(
    headers: dict,
) -> Tuple[
    Dict[str, Tuple[str, Optional[int], Optional[str]]],
    Dict[str, Tuple[str, str, Optional[str]]],
]:
    """Return Trakt history keyed by IMDb or TMDb GUID for movies and episodes.

    ``watched_at`` is included so items can be synced with the original
    timestamp when possible.
    """
    movies: Dict[str, Tuple[str, Optional[int], Optional[str]]] = {}
    episodes: Dict[str, Tuple[str, str, Optional[str]]] = {}

    page = 1
    logger.info("Fetching Trakt history…")
    while True:
        resp = trakt_request(
            "GET",
            "/sync/history",
            headers,
            params={"page": page, "limit": 100},
        )
        data = resp.json()
        if not isinstance(data, list):
            logger.error("Unexpected Trakt history format: %r", data)
            break
        if not data:
            break
        for item in data:
            watched_at = item.get("watched_at")
            if item["type"] == "movie":
                m = item["movie"]
                ids = m.get("ids", {})
                if ids.get("imdb"):
                    guid = f"imdb://{ids['imdb']}"
                elif ids.get("tmdb"):
                    guid = f"tmdb://{ids['tmdb']}"
                else:
                    guid = None
                if not guid:
                    continue
                if guid not in movies:
                    movies[guid] = (
                        m["title"],
                        normalize_year(m.get("year")),
                        watched_at,
                    )
            elif item["type"] == "episode":
                e = item["episode"]
                show = item["show"]
                ids = e.get("ids", {})
                if ids.get("imdb"):
                    guid = f"imdb://{ids['imdb']}"
                elif ids.get("tmdb"):
                    guid = f"tmdb://{ids['tmdb']}"
                else:
                    guid = None
                if not guid:
                    continue
                if guid not in episodes:
                    episodes[guid] = (
                        show["title"],
                        f"S{e['season']:02d}E{e['number']:02d}",
                        watched_at,
                    )
        page += 1

    return movies, episodes


def update_trakt(
    headers: dict,
    movies: List[Tuple[str, Optional[int], Optional[str], Optional[str]]],
    episodes: List[Tuple[str, str, Optional[str], Optional[str]]],
) -> None:
    payload = {"movies": [], "episodes": []}

    for title, year, watched_at, guid in movies:
        movie_obj = {"title": title}
        if year is not None:
            movie_obj["year"] = year
        if guid:
            movie_ids = guid_to_ids(guid)
            if movie_ids:
                movie_obj["ids"] = movie_ids
        if watched_at:
            movie_obj["watched_at"] = watched_at
        payload["movies"].append(movie_obj)

    for show, code, watched_at, guid in episodes:
        season = int(code[1:3])
        number = int(code[4:6])
        ep_obj = {"season": season, "number": number}
        if guid:
            ep_obj["ids"] = guid_to_ids(guid)
        else:
            # Usamos los IDs de la serie para que Trakt encuentre el episodio
            show_obj = get_show_from_library(plex, show)
            show_ids = guid_to_ids(best_guid(show_obj)) if show_obj else {}
            if show_ids:
                ep_obj["show"] = {"ids": show_ids}
            else:
                ep_obj["title"] = show  # último recurso
        if watched_at:
            ep_obj["watched_at"] = watched_at
        payload["episodes"].append(ep_obj)

    if not payload["movies"] and not payload["episodes"]:
        logger.info("Nothing new to send to Trakt.")
        return

    trakt_request("POST", "/sync/history", headers, json=payload)
    logger.info(
        "Sent %d movies and %d episodes to Trakt",
        len(payload["movies"]),
        len(payload["episodes"]),
    )


def get_simkl_history(
    headers: dict,
) -> Tuple[
    Dict[str, Tuple[str, Optional[int], Optional[str]]],
    Dict[str, Tuple[str, str, Optional[str]]],
]:
    """Return Simkl history keyed by IMDb or TMDb GUID.

    ``watched_at`` is included so items can be synced with the original
    timestamp when possible.
    """
    movies: Dict[str, Tuple[str, Optional[int], Optional[str]]] = {}
    episodes: Dict[str, Tuple[str, str, Optional[str]]] = {}

    logger.info("Fetching Simkl history…")
    resp = simkl_request(
        "GET",
        "/sync/all-items",
        headers,
        params={"extended": "full", "episode_watched_at": "yes"},
    )
    data = resp.json()
    logger.info(f"Simkl API response: {data}")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        logger.error("Unexpected Simkl history format: %r", data)
        return movies, episodes

    for m in data.get("movies", []):
        movie_data = m.get("movie", m)  # fallback for legacy format
        ids = movie_data.get("ids", {})
        watched_at = m.get("last_watched_at") or m.get("watched_at")
        guid = None
        # Solo usar TVDb para Simkl
        if ids.get("tvdb"):
            guid = f"tvdb://{ids['tvdb']}"
        if guid and guid not in movies:
            movies[guid] = (
                movie_data.get("title", ""),
                normalize_year(movie_data.get("year")),
                watched_at,
            )

    for show in data.get("shows", []):
        show_data = show.get("show", show)  # fallback for legacy format
        title = show_data.get("title", "")
        
        for season in show.get("seasons", []):
            s_num = season.get("number")
            if s_num is None:
                continue  # Skip seasons without valid numbers
                
            for ep in season.get("episodes", []):
                watched_at = ep.get("watched_at")
                ep_num = ep.get("number")
                
                if ep_num is None:
                    continue  # Skip episodes without valid numbers
                
                # Create episode object with season and number for the key function
                episode_obj = {
                    "season": s_num,
                    "number": ep_num,
                    "ids": ep.get("ids", {})  # Include episode-specific IDs if available
                }
                
                # Generate unique episode identifier using only TVDb
                ep_guid = simkl_episode_key(show, episode_obj)
                if not ep_guid:
                    continue  # Solo episodios con TVDb
                if ep_guid not in episodes:
                    episodes[ep_guid] = (
                        title,
                        f"S{s_num:02d}E{ep_num:02d}",
                        watched_at,
                    )

    return movies, episodes


def update_simkl(
    headers: dict,
    movies: List[Tuple[str, Optional[int], Optional[str], Optional[str]]],
    episodes: List[Tuple[str, str, Optional[str], Optional[str]]],
) -> None:
    payload = {"movies": [], "episodes": []}
    list_movies = []
    list_shows: Dict[str, dict] = {}

    for title, year, watched_at, guid in movies:
        movie_obj = {"title": title}
        if year is not None:
            movie_obj["year"] = year
        if guid:
            movie_ids = guid_to_ids(guid)
            if movie_ids:
                movie_obj["ids"] = movie_ids
        if watched_at:
            movie_obj["watched_at"] = watched_at
        payload["movies"].append(movie_obj)

        list_movie = {"title": title, "to": "completed"}
        if year is not None:
            list_movie["year"] = year
        if guid:
            list_ids = guid_to_ids(guid)
            if list_ids:
                list_movie["ids"] = list_ids
        if watched_at:
            list_movie["watched_at"] = watched_at
        list_movies.append(list_movie)

    for show, code, watched_at, guid in episodes:
        season = int(code[1:3])
        number = int(code[4:6])
        ep_obj = {"season": season, "number": number}
        if guid:
            ep_ids = guid_to_ids(guid)
            if ep_ids:
                ep_obj["ids"] = ep_ids
        else:
            show_obj = get_show_from_library(plex, show)
            show_ids = guid_to_ids(best_guid(show_obj)) if show_obj else {}
            if show_ids:
                ep_obj["show"] = {"ids": show_ids}
            else:
                ep_obj["title"] = show
        if watched_at:
            ep_obj["watched_at"] = watched_at
        payload["episodes"].append(ep_obj)

        show_obj = get_show_from_library(plex, show)
        show_guid = best_guid(show_obj) if show_obj else None
        key = show_guid if show_guid else show
        show_payload = list_shows.get(key)
        if not show_payload:
            show_payload = {"title": show, "to": "completed"}
            if show_obj and getattr(show_obj, "year", None):
                show_payload["year"] = normalize_year(show_obj.year)
            if show_guid:
                show_ids_payload = guid_to_ids(show_guid)
                if show_ids_payload:
                    show_payload["ids"] = show_ids_payload
            if watched_at:
                show_payload["watched_at"] = watched_at
            list_shows[key] = show_payload
        else:
            if watched_at and (
                "watched_at" not in show_payload or watched_at > show_payload["watched_at"]
            ):
                show_payload["watched_at"] = watched_at

    if not payload["movies"] and not payload["episodes"]:
        logger.info("Nothing new to send to Simkl.")
        return

    simkl_request("POST", "/sync/history", headers, json=payload)
    logger.info(
        "Sent %d movies and %d episodes to Simkl",
        len(payload["movies"]),
        len(payload["episodes"]),
    )

    if list_movies:
        simkl_request("POST", "/sync/my-movies", headers, json={"movies": list_movies})
        logger.info("Marked %d movies as completed on Simkl", len(list_movies))

    if list_shows:
        simkl_request(
            "POST",
            "/sync/my-shows",
            headers,
            json={"shows": list(list_shows.values())},
        )
        logger.info("Marked %d shows as completed on Simkl", len(list_shows))


def update_plex(
    plex,
    movies: Set[Tuple[str, Optional[int], Optional[str]]],
    episodes: Set[Tuple[str, str, Optional[str]]],
) -> None:
    """Mark items as watched in Plex when they appear in Trakt but not in Plex."""
    movie_count = 0
    episode_count = 0

    # Movies
    for title, year, guid in movies:
        item = None
        if guid:
            try:
                item = plex.fetchItem(guid)
            except Exception as exc:
                logger.debug("GUID fetch failed for %s: %s", guid, exc)
        if item:
            item.markWatched()
            movie_count += 1
            continue
        try:
            if year:
                results = plex.library.search(title=title, year=year, libtype="movie")
            else:
                results = plex.library.search(title=title, libtype="movie")
            if not results:
                logger.warning("No match in Plex for %s (%s)", title, year)
            for it in results:
                it.markWatched()
                movie_count += 1
        except BadRequest as exc:
            logger.warning("Plex search error for %s (%s): %s", title, year, exc)

    # Episodes
    for show, code, guid in episodes:
        item = None
        if guid:
            try:
                item = plex.fetchItem(guid)
            except Exception as exc:
                logger.debug("GUID fetch failed for %s: %s", guid, exc)
        if item:
            item.markWatched()
            episode_count += 1
            continue
        season = int(code[1:3])
        number = int(code[4:6])
        try:
            series = plex.library.search(title=show, libtype="show")
            found = False
            for show_obj in series:
                try:
                    ep = show_obj.episode(season=season, episode=number)
                except NotFound:
                    continue
                if ep:
                    ep.markWatched()
                    episode_count += 1
                    found = True
            if not found:
                logger.warning("No match in Plex for %s %s", show, code)
        except BadRequest as exc:
            logger.warning("Plex search error for %s %s: %s", show, code, exc)

    if movie_count or episode_count:
        logger.info(
            "Marked %d movies and %d episodes as watched in Plex",
            movie_count,
            episode_count,
        )
    else:
        logger.info("Nothing new to send to Plex.")


# --------------------------------------------------------------------------- #
# ADDITIONAL SYNC FEATURES
# --------------------------------------------------------------------------- #


def sync_collection(plex, headers):
    """Add all Plex movies to the user's Trakt collection."""
    movies = []
    for section in plex.library.sections():
        if section.type == "movie":
            for item in section.all():
                guid = best_guid(item)
                obj = {"title": item.title}
                if getattr(item, "year", None):
                    obj["year"] = normalize_year(item.year)
                if guid:
                    obj["ids"] = guid_to_ids(guid)
                movies.append(obj)
    if movies:
        trakt_request("POST", "/sync/collection", headers, json={"movies": movies})
        logger.info("Synced %d Plex movies to Trakt collection", len(movies))


def sync_ratings(plex, headers):
    """Send user ratings from Plex to Trakt."""
    movies: List[dict] = []
    shows: List[dict] = []
    episodes: List[dict] = []

    rated_now = to_iso_z(datetime.utcnow())

    for section in plex.library.sections():
        if section.type == "movie":
            for item in section.all():
                rating = getattr(item, "userRating", None)
                if rating is not None:
                    guid = best_guid(item)
                    obj = {
                        "title": item.title,
                        "rating": int(round(float(rating))),
                        "rated_at": rated_now,
                    }
                    if getattr(item, "year", None):
                        obj["year"] = normalize_year(item.year)
                    if guid:
                        obj["ids"] = guid_to_ids(guid)
                    movies.append(obj)

        elif section.type == "show":
            for show in section.all():
                show_guid = best_guid(show)
                show_ids = guid_to_ids(show_guid) if show_guid else {}
                base = {"title": show.title}
                if getattr(show, "year", None):
                    base["year"] = normalize_year(show.year)
                if show_ids:
                    base["ids"] = show_ids

                seasons_list = []
                has_show_data = False

                show_rating = getattr(show, "userRating", None)
                if show_rating is not None:
                    base["rating"] = int(round(float(show_rating)))
                    base["rated_at"] = rated_now
                    has_show_data = True

                for season in show.seasons():
                    season_rating = getattr(season, "userRating", None)
                    if season_rating is not None:
                        seasons_list.append(
                            {
                                "number": int(season.index),
                                "rating": int(round(float(season_rating))),
                                "rated_at": rated_now,
                            }
                        )
                        has_show_data = True

                    for ep in season.episodes():
                        ep_rating = getattr(ep, "userRating", None)
                        if ep_rating is not None:
                            ep_obj = {
                                "season": int(season.index),
                                "number": int(ep.index),
                                "rating": int(round(float(ep_rating))),
                                "rated_at": rated_now,
                            }
                            ep_guid = best_guid(ep)
                            if ep_guid:
                                ep_obj["ids"] = guid_to_ids(ep_guid)
                            elif show_ids:
                                ep_obj["show"] = {"ids": show_ids}
                            else:
                                ep_obj["title"] = show.title
                            episodes.append(ep_obj)

                if seasons_list:
                    base["seasons"] = seasons_list

                if has_show_data:
                    shows.append(base)

    payload = {}
    if movies:
        payload["movies"] = movies
    if shows:
        payload["shows"] = shows
    if episodes:
        payload["episodes"] = episodes

    if payload:
        trakt_request("POST", "/sync/ratings", headers, json=payload)
        logger.info(
            "Synced %d movie ratings, %d shows and %d episode ratings to Trakt",
            len(movies),
            len(shows),
            len(episodes),
        )
    else:
        logger.info("No Plex ratings to sync")


def sync_liked_lists(plex, headers):
    """Create Plex collections from liked Trakt lists."""
    try:
        likes = trakt_request("GET", "/users/likes/lists", headers).json()
    except Exception as exc:
        logger.error("Failed to fetch liked lists: %s", exc)
        return
    for like in likes:
        lst = like.get("list", {})
        owner = lst.get("user", {}).get("ids", {}).get("slug") or lst.get(
            "user", {}
        ).get("username")
        slug = lst.get("ids", {}).get("slug")
        name = lst.get("name", slug)
        if not owner or not slug:
            continue
        try:
            items = trakt_request(
                "GET", f"/users/{owner}/lists/{slug}/items", headers
            ).json()
        except Exception as exc:
            logger.error("Failed to fetch list %s/%s: %s", owner, slug, exc)
            continue
        movie_items = []
        show_items = []
        for it in items:
            data = it.get(it["type"], {})
            ids = data.get("ids", {})
            guid = None
            if ids.get("imdb"):
                guid = f"imdb://{ids['imdb']}"
            elif ids.get("tmdb"):
                guid = f"tmdb://{ids['tmdb']}"
            if not guid:
                continue
            plex_item = find_item_by_guid(plex, guid)
            if plex_item:
                if plex_item.TYPE == "movie":
                    movie_items.append(plex_item)
                elif plex_item.TYPE == "show":
                    show_items.append(plex_item)
        if movie_items or show_items:
            for sec in plex.library.sections():
                if sec.type == "movie" and movie_items:
                    coll = ensure_collection(plex, sec, name, first_item=movie_items[0])
                    try:
                        if len(movie_items) > 1:
                            coll.addItems(movie_items[1:])
                    except Exception:
                        pass
                if sec.type == "show" and show_items:
                    coll = ensure_collection(plex, sec, name, first_item=show_items[0])
                    try:
                        if len(show_items) > 1:
                            coll.addItems(show_items[1:])
                    except Exception:
                        pass


def sync_collections_to_trakt(plex, headers):
    """Create or update Trakt lists from Plex collections."""
    try:
        user_data = trakt_request("GET", "/users/settings", headers).json()
        username = user_data.get("user", {}).get("ids", {}).get(
            "slug"
        ) or user_data.get("user", {}).get("username")
        lists = trakt_request("GET", f"/users/{username}/lists", headers).json()
    except Exception as exc:
        logger.error("Failed to fetch Trakt lists: %s", exc)
        return

    slug_by_name = {l.get("name"): l.get("ids", {}).get("slug") for l in lists}

    for sec in plex.library.sections():
        if sec.type not in ("movie", "show"):
            continue
        for coll in sec.collections():
            slug = slug_by_name.get(coll.title)
            if not slug:
                try:
                    resp = trakt_request(
                        "POST",
                        f"/users/{username}/lists",
                        headers,
                        json={"name": coll.title},
                    )
                    slug = resp.json().get("ids", {}).get("slug")
                    slug_by_name[coll.title] = slug
                except Exception as exc:
                    logger.error("Failed creating list %s: %s", coll.title, exc)
                    continue
            try:
                items = trakt_request(
                    "GET",
                    f"/users/{username}/lists/{slug}/items",
                    headers,
                ).json()
            except Exception as exc:
                logger.error("Failed to fetch list %s items: %s", slug, exc)
                continue
            trakt_guids = set()
            for it in items:
                data = it.get(it["type"], {})
                ids = data.get("ids", {})
                if ids.get("imdb"):
                    trakt_guids.add(f"imdb://{ids['imdb']}")
                elif ids.get("tmdb"):
                    trakt_guids.add(f"tmdb://{ids['tmdb']}")
            movies = []
            shows = []
            for item in coll.items():
                guid = imdb_guid(item)
                if not guid or guid in trakt_guids:
                    continue
                if item.type == "movie":
                    movies.append({"ids": guid_to_ids(guid)})
                elif item.type == "show":
                    shows.append({"ids": guid_to_ids(guid)})
            payload = {}
            if movies:
                payload["movies"] = movies
            if shows:
                payload["shows"] = shows
            if payload:
                try:
                    trakt_request(
                        "POST",
                        f"/users/{username}/lists/{slug}/items",
                        headers,
                        json=payload,
                    )
                    logger.info(
                        "Updated Trakt list %s with %d items",
                        slug,
                        len(movies) + len(shows),
                    )
                except Exception as exc:
                    logger.error("Failed updating list %s: %s", slug, exc)


def sync_watchlist(plex, headers, plex_history, trakt_history):
    """Two-way sync of Plex and Trakt watchlists."""
    # Use MyPlexAccount to access watchlist API
    account = plex.myPlexAccount()
    try:
        plex_watch = account.watchlist()
    except Exception as exc:
        logger.error("Failed to fetch Plex watchlist: %s", exc)
        plex_watch = []
    try:
        trakt_movies = trakt_request("GET", "/sync/watchlist/movies", headers).json()
        trakt_shows = trakt_request("GET", "/sync/watchlist/shows", headers).json()
    except Exception as exc:
        logger.error("Failed to fetch Trakt watchlist: %s", exc)
        return

    plex_guids = set()
    for item in plex_watch:
        g = imdb_guid(item)
        if g:
            plex_guids.add(g)

    trakt_guids = set()
    for lst in (trakt_movies, trakt_shows):
        for it in lst:
            ids = it.get(it["type"], {}).get("ids", {})
            if ids.get("imdb"):
                trakt_guids.add(f"imdb://{ids['imdb']}")
            elif ids.get("tmdb"):
                trakt_guids.add(f"tmdb://{ids['tmdb']}")

    # Add Plex watchlist items to Trakt
    movies_to_add = []
    shows_to_add = []
    for item in plex_watch:
        guid = imdb_guid(item)
        if not guid or guid in trakt_guids:
            continue
        data = guid_to_ids(guid)
        if item.TYPE == "movie":
            movies_to_add.append({"ids": data})
        elif item.TYPE == "show":
            shows_to_add.append({"ids": data})
    payload = {}
    if movies_to_add:
        payload["movies"] = movies_to_add
    if shows_to_add:
        payload["shows"] = shows_to_add
    if payload:
        trakt_request("POST", "/sync/watchlist", headers, json=payload)
        logger.info(
            "Added %d items to Trakt watchlist", len(movies_to_add) + len(shows_to_add)
        )

    # Add Trakt watchlist items to Plex
    add_to_plex = []
    for lst in (trakt_movies, trakt_shows):
        for it in lst:
            data = it.get(it["type"], {})
            ids = data.get("ids", {})
            guid = None
            if ids.get("imdb"):
                guid = f"imdb://{ids['imdb']}"
            elif ids.get("tmdb"):
                guid = f"tmdb://{ids['tmdb']}"
            if not guid or guid in plex_guids:
                continue
            item = find_item_by_guid(plex, guid)
            if item:
                add_to_plex.append(item)
    if add_to_plex:
        try:
            account.addToWatchlist(add_to_plex)
            logger.info("Added %d items to Plex watchlist", len(add_to_plex))
        except Exception as exc:
            logger.error("Failed adding Plex watchlist items: %s", exc)

    # Remove watched items from watchlists
    for guid in list(plex_guids):
        if guid in trakt_history or guid in plex_history:
            try:
                item = find_item_by_guid(plex, guid)
                if item:
                    account.removeFromWatchlist([item])
            except Exception:
                pass
    remove = []
    for lst in (trakt_movies, trakt_shows):
        for it in lst:
            data = it.get(it["type"], {})
            ids = data.get("ids", {})
            guid = None
            if ids.get("imdb"):
                guid = f"imdb://{ids['imdb']}"
            elif ids.get("tmdb"):
                guid = f"tmdb://{ids['tmdb']}"
            if (
                guid
                and (guid in plex_history or guid in trakt_history)
                and guid not in plex_guids
            ):
                remove.append({"ids": guid_to_ids(guid)})
    if remove:
        trakt_request(
            "POST",
            "/sync/watchlist/remove",
            headers,
            json={"movies": remove, "shows": remove},
        )
        logger.info("Removed %d items from Trakt watchlist", len(remove))


def sync_simkl_library(plex, headers):
    """Add Plex movies and shows to Simkl My Movies and My TV Shows."""
    movies = []
    shows = []
    for section in plex.library.sections():
        if section.type == "movie":
            for item in section.all():
                m = {"title": item.title}
                if getattr(item, "year", None):
                    m["year"] = normalize_year(item.year)
                guid = best_guid(item)
                if guid:
                    m["ids"] = guid_to_ids(guid)
                movies.append(m)
        elif section.type == "show":
            for item in section.all():
                s = {"title": item.title}
                if getattr(item, "year", None):
                    s["year"] = normalize_year(item.year)
                guid = best_guid(item)
                if guid:
                    s["ids"] = guid_to_ids(guid)
                shows.append(s)
    if movies:
        try:
            simkl_request("POST", "/sync/my-movies", headers, json={"movies": movies})
            logger.info("Synced %d Plex movies to Simkl My Movies", len(movies))
        except Exception as exc:
            logger.error("Failed syncing movies to Simkl: %s", exc)
    if shows:
        try:
            simkl_request("POST", "/sync/my-shows", headers, json={"shows": shows})
            logger.info("Synced %d Plex shows to Simkl My TV Shows", len(shows))
        except Exception as exc:
            logger.error("Failed syncing shows to Simkl: %s", exc)


def sync_collections_to_simkl(plex, headers):
    """Create or update Simkl lists from Plex collections."""
    try:
        user_data = simkl_request("GET", "/users/settings", headers).json()
        username = (
            user_data.get("user", {}).get("username")
            or user_data.get("user", {}).get("ids", {}).get("slug")
        )
        resp = simkl_request("GET", f"/users/{username}/lists", headers)
        lists = resp.json()
        if not isinstance(lists, list):
            logger.error("Unexpected response when fetching Simkl lists: %s", lists)
            return
    except Exception as exc:
        logger.error("Failed to fetch Simkl lists: %s", exc)
        return

    slug_by_name = {l.get("name"): l.get("ids", {}).get("slug") for l in lists}

    for sec in plex.library.sections():
        if sec.type not in ("movie", "show"):
            continue
        for coll in sec.collections():
            slug = slug_by_name.get(coll.title)
            if not slug:
                try:
                    resp = simkl_request(
                        "POST",
                        f"/users/{username}/lists",
                        headers,
                        json={"name": coll.title},
                    )
                    slug = resp.json().get("ids", {}).get("slug")
                    slug_by_name[coll.title] = slug
                except Exception as exc:
                    logger.error("Failed creating Simkl list %s: %s", coll.title, exc)
                    continue
            try:
                items = simkl_request(
                    "GET",
                    f"/users/{username}/lists/{slug}/items",
                    headers,
                ).json()
            except Exception as exc:
                logger.error("Failed to fetch Simkl list %s items: %s", slug, exc)
                continue
            simkl_guids = set()
            for it in items:
                data = it.get(it["type"], {})
                ids = data.get("ids", {})
                if ids.get("imdb"):
                    simkl_guids.add(f"imdb://{ids['imdb']}")
                elif ids.get("tmdb"):
                    simkl_guids.add(f"tmdb://{ids['tmdb']}")
            movies = []
            shows = []
            for item in coll.items():
                guid = imdb_guid(item)
                if not guid or guid in simkl_guids:
                    continue
                if item.type == "movie":
                    movies.append({"ids": guid_to_ids(guid)})
                elif item.type == "show":
                    shows.append({"ids": guid_to_ids(guid)})
            payload = {}
            if movies:
                payload["movies"] = movies
            if shows:
                payload["shows"] = shows
            if payload:
                try:
                    simkl_request(
                        "POST",
                        f"/users/{username}/lists/{slug}/items",
                        headers,
                        json=payload,
                    )
                    logger.info(
                        "Updated Simkl list %s with %d items",
                        slug,
                        len(movies) + len(shows),
                    )
                except Exception as exc:
                    logger.error("Failed updating Simkl list %s: %s", slug, exc)


# --------------------------------------------------------------------------- #
# SCHEDULER TASK
# --------------------------------------------------------------------------- #
def sync():
    global plex
    logger.info("Starting synchronization job")

    plex_baseurl = os.environ.get("PLEX_BASEURL")
    plex_token = os.environ.get("PLEX_TOKEN")
    trakt_token = os.environ.get("TRAKT_ACCESS_TOKEN")
    trakt_client_id = os.environ.get("TRAKT_CLIENT_ID")
    simkl_token = os.environ.get("SIMKL_ACCESS_TOKEN")
    simkl_client_id = os.environ.get("SIMKL_CLIENT_ID")

    trakt_enabled = SYNC_PROVIDER == "trakt" and bool(trakt_token and trakt_client_id)
    simkl_enabled = SYNC_PROVIDER == "simkl" and bool(simkl_token and simkl_client_id)

    if not all([plex_baseurl, plex_token]):
        logger.error("Missing environment variables for Plex.")
        return
    if not (trakt_enabled or simkl_enabled):
        logger.error("Missing environment variables for selected provider.")
        return

    plex = PlexServer(plex_baseurl, plex_token)

    headers = None
    if trakt_enabled:
        headers = {
            "Authorization": f"Bearer {trakt_token}",
            "Content-Type": "application/json",
            "User-Agent": "PlexyTrackt/1.0.0",
            "trakt-api-version": "2",
            "trakt-api-key": trakt_client_id,
        }
    simkl_headers = None
    if simkl_enabled:
        simkl_headers = {
            "Authorization": f"Bearer {simkl_token}",
            "Content-Type": "application/json",
            "simkl-api-key": simkl_client_id,
        }

    if SYNC_COLLECTION and trakt_enabled:
        try:
            sync_collection(plex, headers)
        except TraktAccountLimitError as exc:
            logger.error("Collection sync skipped: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.error("Collection sync failed: %s", exc)
    if SYNC_COLLECTION and simkl_enabled:
        try:
            sync_simkl_library(plex, simkl_headers)
        except Exception as exc:  # noqa: BLE001
            logger.error("Simkl library sync failed: %s", exc)
    if SYNC_RATINGS and trakt_enabled:
        try:
            sync_ratings(plex, headers)
        except Exception as exc:  # noqa: BLE001
            logger.error("Ratings sync failed: %s", exc)

    try:
        plex_movies, plex_episodes = get_plex_history(plex)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to retrieve Plex history: %s", exc)
        plex_movies, plex_episodes = {}, {}
    plex_movie_guids = set(plex_movies.keys())
    plex_episode_guids = set(plex_episodes.keys())

    if trakt_enabled:
        try:
            trakt_movies, trakt_episodes = get_trakt_history(headers)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to retrieve Trakt history: %s", exc)
            trakt_movies, trakt_episodes = {}, {}
        trakt_movie_guids = set(trakt_movies.keys())
        trakt_episode_guids = set(trakt_episodes.keys())
    else:
        trakt_movies, trakt_episodes = {}, {}
        trakt_movie_guids = set()
        trakt_episode_guids = set()

    if simkl_enabled:
        try:
            simkl_movies, simkl_episodes = get_simkl_history(simkl_headers)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to retrieve Simkl history: %s", exc)
            simkl_movies, simkl_episodes = {}, {}
        simkl_movie_guids = set(simkl_movies.keys())
        simkl_episode_guids = set(simkl_episodes.keys())
    else:
        simkl_movies, simkl_episodes = {}, {}
        simkl_movie_guids = set()
        simkl_episode_guids = set()

    logger.info(
        "Plex history:   %d movies, %d episodes",
        len(plex_movie_guids),
        len(plex_episode_guids),
    )
    if trakt_enabled:
        logger.info(
            "Trakt history:  %d movies, %d episodes",
            len(trakt_movie_guids),
            len(trakt_episode_guids),
        )
    if simkl_enabled:
        logger.info(
            "Simkl history:  %d movies, %d episodes",
            len(simkl_movie_guids),
            len(simkl_episode_guids),
        )

    new_movies = [
        (data["title"], data["year"], data["watched_at"], guid)
        for guid, data in plex_movies.items()
        if guid not in trakt_movie_guids and guid not in simkl_movie_guids
    ]
    new_episodes = [
        (data["show"], data["code"], data["watched_at"], guid)
        for guid, data in plex_episodes.items()
        if guid not in trakt_episode_guids and guid not in simkl_episode_guids
    ]

    # Permite desactivar la sync de vistos desde la interfaz
    if SYNC_WATCHED:
        try:
            if trakt_enabled:
                update_trakt(headers, new_movies, new_episodes)
            if simkl_enabled:
                update_simkl(simkl_headers, new_movies, new_episodes)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed updating Trakt/Simkl history: %s", exc)
    missing_movies = {
        (title, year, guid)
        for guid, (title, year, _watched) in trakt_movies.items()
        if guid not in plex_movie_guids
    }
    missing_episodes = {
        (show, code, guid)
        for guid, (show, code, _watched) in trakt_episodes.items()
        if guid not in plex_episode_guids
    }
    if simkl_enabled:
        missing_movies.update(
            {
                (title, year, guid)
                for guid, (title, year, _watched) in simkl_movies.items()
                if guid not in plex_movie_guids
            }
        )
        missing_episodes.update(
            {
                (show, code, guid)
                for guid, (show, code, _watched) in simkl_episodes.items()
                if guid not in plex_episode_guids
            }
        )
    try:
        update_plex(plex, missing_movies, missing_episodes)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed updating Plex history: %s", exc)

    if SYNC_LIKED_LISTS and trakt_enabled:
        try:
            sync_liked_lists(plex, headers)
            sync_collections_to_trakt(plex, headers)
        except TraktAccountLimitError as exc:
            logger.error("Liked-lists sync skipped: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.error("Liked-lists sync failed: %s", exc)
    if SYNC_LIKED_LISTS and simkl_enabled:
        try:
            sync_collections_to_simkl(plex, simkl_headers)
        except Exception as exc:  # noqa: BLE001
            logger.error("Simkl lists sync failed: %s", exc)
    if SYNC_WATCHLISTS and trakt_enabled:
        try:
            sync_watchlist(
                plex,
                headers,
                plex_movie_guids | plex_episode_guids,
                trakt_movie_guids | trakt_episode_guids,
            )
        except TraktAccountLimitError as exc:
            logger.error("Watchlist sync skipped: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.error("Watchlist sync failed: %s", exc)

    logger.info("Synchronization job finished")


# --------------------------------------------------------------------------- #
# BACKUP HANDLING
# --------------------------------------------------------------------------- #


def fetch_trakt_history_full(headers) -> list:
    """Return full watch history from Trakt."""
    all_items = []
    page = 1
    while True:
        resp = trakt_request(
            "GET",
            "/sync/history",
            headers,
            params={"page": page, "limit": 100},
        )
        data = resp.json()
        if not data:
            break
        all_items.extend(data)
        page += 1
    return all_items


def fetch_trakt_ratings(headers) -> list:
    """Return all ratings from Trakt."""
    all_items = []
    page = 1
    while True:
        resp = trakt_request(
            "GET",
            "/sync/ratings",
            headers,
            params={"page": page, "limit": 100},
        )
        data = resp.json()
        if not data:
            break
        all_items.extend(data)
        page += 1
    return all_items


def fetch_trakt_watchlist(headers) -> dict:
    """Return movies and shows from the Trakt watchlist."""
    movies = trakt_request("GET", "/sync/watchlist/movies", headers).json()
    shows = trakt_request("GET", "/sync/watchlist/shows", headers).json()
    return {"movies": movies, "shows": shows}


def restore_backup(headers, data: dict) -> None:
    """Restore Trakt data from backup dict."""
    history = data.get("history", [])
    movies = []
    episodes = []
    for item in history:
        if item.get("type") == "movie":
            m = item.get("movie", {})
            ids = m.get("ids", {})
            if ids:
                obj = {"ids": ids}
                if item.get("watched_at"):
                    obj["watched_at"] = item["watched_at"]
                if m.get("title"):
                    obj["title"] = m.get("title")
                if m.get("year"):
                    obj["year"] = m.get("year")
                movies.append(obj)
        elif item.get("type") == "episode":
            ep = item.get("episode", {})
            ids = ep.get("ids", {})
            if ids:
                obj = {
                    "ids": ids,
                    "season": ep.get("season"),
                    "number": ep.get("number"),
                }
                if item.get("watched_at"):
                    obj["watched_at"] = item["watched_at"]
                show = item.get("show", {})
                if show.get("ids"):
                    obj["show"] = {"ids": show.get("ids")}
                episodes.append(obj)
    payload = {}
    if movies:
        payload["movies"] = movies
    if episodes:
        payload["episodes"] = episodes
    if payload:
        trakt_request("POST", "/sync/history", headers, json=payload)

    ratings = data.get("ratings", [])
    r_movies, r_shows, r_episodes, r_seasons = [], [], [], []
    for item in ratings:
        typ = item.get("type")
        ids = item.get(typ, {}).get("ids", {}) if typ else {}
        if not ids:
            continue
        obj = {"ids": ids, "rating": item.get("rating")}
        if item.get("rated_at"):
            obj["rated_at"] = item["rated_at"]
        if typ == "movie":
            r_movies.append(obj)
        elif typ == "show":
            r_shows.append(obj)
        elif typ == "season":
            r_seasons.append(obj)
        elif typ == "episode":
            r_episodes.append(obj)
    payload = {}
    if r_movies:
        payload["movies"] = r_movies
    if r_shows:
        payload["shows"] = r_shows
    if r_seasons:
        payload["seasons"] = r_seasons
    if r_episodes:
        payload["episodes"] = r_episodes
    if payload:
        trakt_request("POST", "/sync/ratings", headers, json=payload)

    watchlist = data.get("watchlist", {})
    wl_movies = []
    for it in watchlist.get("movies", []):
        m = it.get("movie", {})
        ids = m.get("ids", {})
        if ids:
            wl_movies.append({"ids": ids})
    wl_shows = []
    for it in watchlist.get("shows", []):
        s = it.get("show", {}) if "show" in it else it.get("movie", {})
        ids = s.get("ids", {})
        if ids:
            wl_shows.append({"ids": ids})
    payload = {}
    if wl_movies:
        payload["movies"] = wl_movies
    if wl_shows:
        payload["shows"] = wl_shows
    if payload:
        trakt_request("POST", "/sync/watchlist", headers, json=payload)


# --------------------------------------------------------------------------- #
# FLASK ROUTES
# --------------------------------------------------------------------------- #
@app.route("/", methods=["GET", "POST"])
def index():
    global SYNC_INTERVAL_MINUTES, SYNC_COLLECTION, SYNC_RATINGS, SYNC_WATCHED, SYNC_LIKED_LISTS, SYNC_WATCHLISTS, LIVE_SYNC

    load_trakt_tokens()
    load_simkl_tokens()
    load_provider()

    # 2) Change interval
    if request.method == "POST":
        minutes = int(request.form.get("minutes", 60))
        SYNC_INTERVAL_MINUTES = minutes
        SYNC_COLLECTION = request.form.get("collection") is not None
        SYNC_RATINGS = request.form.get("ratings") is not None
        SYNC_WATCHED = request.form.get("watched") is not None
        SYNC_LIKED_LISTS = request.form.get("liked_lists") is not None
        SYNC_WATCHLISTS = request.form.get("watchlists") is not None
        LIVE_SYNC = request.form.get("live_sync") is not None
        start_scheduler()
        # Schedule an immediate sync without blocking the request
        scheduler.add_job(
            sync,
            "interval",
            minutes=minutes,
            id="sync_job",
            replace_existing=True,
            next_run_time=datetime.now(),
        )
        return redirect(
            url_for("index", message="Sync started successfully!", mtype="success")
        )

    message = request.args.get("message")
    mtype = request.args.get("mtype", "success") if message else None
    next_run = None
    job = scheduler.get_job("sync_job")
    if job:
        next_run = job.next_run_time
    return render_template(
        "index.html",
        minutes=SYNC_INTERVAL_MINUTES,
        collection=SYNC_COLLECTION,
        ratings=SYNC_RATINGS,
        watched=SYNC_WATCHED,
        liked_lists=SYNC_LIKED_LISTS,
        watchlists=SYNC_WATCHLISTS,
        live_sync=LIVE_SYNC,
        message=message,
        mtype=mtype,
        next_run=next_run,
    )


@app.route("/oauth")
def oauth_index():
    """Landing page for OAuth callbacks."""
    return render_template("oauth.html", service=None, code=None)


@app.route("/oauth/<service>")
def oauth_callback(service: str):
    """Display OAuth code for the given service."""
    service = service.lower()
    if service not in {"trakt", "simkl"}:
        return redirect(url_for("oauth_index"))
    code = request.args.get("code", "")
    return render_template(
        "oauth.html",
        service=service.capitalize(),
        code=code,
    )


@app.route("/trakt")
def trakt_callback():
    code = request.args.get("code", "")
    return redirect(url_for("oauth_callback", service="trakt", code=code))


@app.route("/simkl")
def simkl_callback():
    code = request.args.get("code", "")
    return redirect(url_for("oauth_callback", service="simkl", code=code))


@app.route("/config", methods=["GET", "POST"])
def config_page():
    """Display configuration status for Trakt and Simkl."""
    load_trakt_tokens()
    load_simkl_tokens()
    load_provider()
    if request.method == "POST":
        provider = request.form.get("provider", "none")
        save_provider(provider)
        if provider == "none":
            stop_scheduler()
        else:
            start_scheduler()
        return redirect(url_for("config_page"))
    trakt_configured = bool(os.environ.get("TRAKT_ACCESS_TOKEN"))
    simkl_configured = bool(os.environ.get("SIMKL_ACCESS_TOKEN"))
    return render_template(
        "config.html",
        trakt_configured=trakt_configured,
        simkl_configured=simkl_configured,
        provider=SYNC_PROVIDER,
    )


@app.route("/authorize/<service>", methods=["GET", "POST"])
def authorize_service(service: str):
    """Handle authorization for Trakt or Simkl."""
    service = service.lower()
    prefill = request.args.get("code", "").strip()
    if request.method == "POST":
        code = request.form.get("code", "").strip()
        if service == "trakt" and code and exchange_code_for_tokens(code):
            if SYNC_PROVIDER == "none":
                save_provider("trakt")
            start_scheduler()
            return redirect(url_for("config_page"))
        if service == "simkl" and code and exchange_code_for_simkl_tokens(code):
            if SYNC_PROVIDER == "none":
                save_provider("simkl")
            start_scheduler()
            return redirect(url_for("config_page"))

    if service == "trakt":
        auth_url = (
            "https://trakt.tv/oauth/authorize"
            f"?response_type=code&client_id={os.environ.get('TRAKT_CLIENT_ID')}"
            f"&redirect_uri={get_trakt_redirect_uri()}"
        )
    elif service == "simkl":
        auth_url = (
            "https://simkl.com/oauth/authorize"
            f"?response_type=code&client_id={os.environ.get('SIMKL_CLIENT_ID')}"
            f"&redirect_uri={get_simkl_redirect_uri()}"
        )
    else:
        return redirect(url_for("config_page"))

    return render_template(
        "authorize.html",
        auth_url=auth_url,
        service=service.capitalize(),
        code=prefill,
    )


@app.route("/clear/<service>", methods=["POST"])
def clear_service(service: str):
    """Remove stored tokens for the given service."""
    service = service.lower()
    if service == "trakt":
        os.environ.pop("TRAKT_ACCESS_TOKEN", None)
        os.environ.pop("TRAKT_REFRESH_TOKEN", None)
        if os.path.exists(TOKEN_FILE):
            try:
                os.remove(TOKEN_FILE)
                logger.info("Removed Trakt token file")
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to remove Trakt token file: %s", exc)
        if SYNC_PROVIDER == "trakt":
            save_provider("none")
    elif service == "simkl":
        os.environ.pop("SIMKL_ACCESS_TOKEN", None)
        if os.path.exists(SIMKL_TOKEN_FILE):
            try:
                os.remove(SIMKL_TOKEN_FILE)
                logger.info("Removed Simkl token file")
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to remove Simkl token file: %s", exc)
        if SYNC_PROVIDER == "simkl":
            save_provider("none")
    return redirect(url_for("config_page"))


@app.route("/stop", methods=["POST"])
def stop():
    stop_scheduler()
    return redirect(
        url_for("index", message="Sync stopped successfully!", mtype="stopped")
    )


@app.route("/backup")
def backup_page():
    message = request.args.get("message")
    mtype = request.args.get("mtype", "success") if message else None
    return render_template("backup.html", message=message, mtype=mtype)


@app.route("/backup/download")
def download_backup():
    load_trakt_tokens()
    trakt_token = os.environ.get("TRAKT_ACCESS_TOKEN")
    trakt_client_id = os.environ.get("TRAKT_CLIENT_ID")
    if not trakt_token or not trakt_client_id:
        return redirect(
            url_for("backup_page", message="Missing Trakt credentials", mtype="error")
        )
    headers = {
        "Authorization": f"Bearer {trakt_token}",
        "Content-Type": "application/json",
        "User-Agent": "PlexyTrackt/1.0.0",
        "trakt-api-version": "2",
        "trakt-api-key": trakt_client_id,
    }
    data = {
        "history": fetch_trakt_history_full(headers),
        "ratings": fetch_trakt_ratings(headers),
        "watchlist": fetch_trakt_watchlist(headers),
    }
    tmp_path = "trakt_backup.json"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return send_file(tmp_path, as_attachment=True, download_name="trakt_backup.json")


@app.route("/backup/restore", methods=["POST"])
def restore_backup_route():
    load_trakt_tokens()
    trakt_token = os.environ.get("TRAKT_ACCESS_TOKEN")
    trakt_client_id = os.environ.get("TRAKT_CLIENT_ID")
    if not trakt_token or not trakt_client_id:
        return redirect(
            url_for("backup_page", message="Missing Trakt credentials", mtype="error")
        )
    headers = {
        "Authorization": f"Bearer {trakt_token}",
        "Content-Type": "application/json",
        "User-Agent": "PlexyTrackt/1.0.0",
        "trakt-api-version": "2",
        "trakt-api-key": trakt_client_id,
    }
    file = request.files.get("backup")
    if not file:
        return redirect(
            url_for("backup_page", message="No file uploaded", mtype="error")
        )
    try:
        data = json.load(file)
    except Exception:
        return redirect(url_for("backup_page", message="Invalid JSON", mtype="error"))
    try:
        restore_backup(headers, data)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to restore backup: %s", exc)
        return redirect(url_for("backup_page", message="Restore failed", mtype="error"))
    return redirect(url_for("backup_page", message="Backup restored", mtype="success"))


@app.route("/webhook", methods=["POST"])
def plex_webhook():
    """Handle Plex webhook events for live synchronization."""
    if LIVE_SYNC:
        # Trigger a one-off sync immediately
        scheduler.add_job(sync, "date", run_date=datetime.now())
    return "", 204


# --------------------------------------------------------------------------- #
# SCHEDULER STARTUP
# --------------------------------------------------------------------------- #
def test_connections() -> bool:
    plex_baseurl = os.environ.get("PLEX_BASEURL")
    plex_token = os.environ.get("PLEX_TOKEN")
    trakt_token = os.environ.get("TRAKT_ACCESS_TOKEN")
    trakt_client_id = os.environ.get("TRAKT_CLIENT_ID")
    simkl_token = os.environ.get("SIMKL_ACCESS_TOKEN")
    simkl_client_id = os.environ.get("SIMKL_CLIENT_ID")

    trakt_enabled = SYNC_PROVIDER == "trakt" and bool(trakt_token and trakt_client_id)
    simkl_enabled = SYNC_PROVIDER == "simkl" and bool(simkl_token and simkl_client_id)

    if not all([plex_baseurl, plex_token]):
        logger.error("Missing environment variables for Plex.")
        return False
    if not (trakt_enabled or simkl_enabled):
        logger.error("Missing environment variables for selected provider.")
        return False

    try:
        PlexServer(plex_baseurl, plex_token).account()
        logger.info("Successfully connected to Plex.")
    except Exception as exc:
        logger.error("Failed to connect to Plex: %s", exc)
        return False

    if trakt_enabled:
        headers = {
            "Authorization": f"Bearer {trakt_token}",
            "Content-Type": "application/json",
            "User-Agent": "PlexyTrackt/1.0.0",
            "trakt-api-version": "2",
            "trakt-api-key": trakt_client_id,
        }
        try:
            trakt_request("GET", "/users/settings", headers)
            logger.info("Successfully connected to Trakt.")
        except Exception as exc:
            logger.error("Failed to connect to Trakt: %s", exc)
            return False

    if simkl_enabled:
        s_headers = {
            "Authorization": f"Bearer {simkl_token}",
            "Content-Type": "application/json",
            "simkl-api-key": simkl_client_id,
        }
        try:
            simkl_request("GET", "/sync/all-items", s_headers)
            logger.info("Successfully connected to Simkl.")
        except Exception as exc:
            logger.error("Failed to connect to Simkl: %s", exc)
            return False

    return True


def start_scheduler():
    if scheduler.running:
        scheduler.add_job(
            sync,
            "interval",
            minutes=SYNC_INTERVAL_MINUTES,
            id="sync_job",
            replace_existing=True,
        )
        logger.info("Sync job added with interval %d minutes", SYNC_INTERVAL_MINUTES)
        return
    if not test_connections():
        logger.error("Connection test failed. Scheduler will not start.")
        return
    scheduler.add_job(
        sync,
        "interval",
        minutes=SYNC_INTERVAL_MINUTES,
        id="sync_job",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started with interval %d minutes", SYNC_INTERVAL_MINUTES)


def stop_scheduler():
    job = scheduler.get_job("sync_job")
    if job:
        scheduler.remove_job("sync_job")
        logger.info("Synchronization job stopped")


# --------------------------------------------------------------------------- #
# MAIN
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    logger.info("Starting PlexyTrackt application")
    load_trakt_tokens()
    load_simkl_tokens()
    load_provider()
    start_scheduler()
    app.run(host="0.0.0.0", port=5030)
