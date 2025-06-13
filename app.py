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

from utils import (
    to_iso_z,
    normalize_year,
    _parse_guid_value,
    best_guid,
    imdb_guid,
    get_show_from_library,
    find_item_by_guid,
    ensure_collection,
    movie_key,
    guid_to_ids,
    valid_guid,
    trakt_movie_key,
    episode_key,
    trakt_episode_key,
    simkl_episode_key,
)
from plex_utils import get_plex_history, update_plex
from trakt_utils import (
    load_trakt_tokens,
    save_trakt_tokens,
    exchange_code_for_tokens,
    refresh_trakt_token,
    trakt_request,
    get_trakt_history,
    update_trakt,
    sync_collection,
    sync_ratings,
    sync_liked_lists,
    sync_collections_to_trakt,
    sync_watchlist,
    fetch_trakt_history_full,
    fetch_trakt_ratings,
    fetch_trakt_watchlist,
    restore_backup,
    load_trakt_last_sync_date,
    save_trakt_last_sync_date,
    get_trakt_last_activity,
)
from simkl_utils import (
    load_simkl_tokens,
    save_simkl_token,
    exchange_code_for_simkl_tokens,
    simkl_request,
    get_simkl_history,
    update_simkl,
    load_last_sync_date,
    save_last_sync_date,
    get_last_activity,
)

# --------------------------------------------------------------------------- #
# LOGGING
# --------------------------------------------------------------------------- #
# Configure root logger with a single handler to prevent duplicates
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

# Remove any existing handlers to prevent duplicates
for handler in root_logger.handlers[:]:
    root_logger.removeHandler(handler)

# Add a single console handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
root_logger.addHandler(console_handler)

logger = logging.getLogger(__name__)

# Configure werkzeug (Flask's HTTP request logger) to reduce verbosity
werkzeug_logger = logging.getLogger('werkzeug')
werkzeug_logger.setLevel(logging.WARNING)

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
    """Convert a raw Plex GUID string to a known imdb/tmdb/tvdb/anidb prefix."""
    if raw.startswith("imdb://"):
        return raw.split("?", 1)[0]
    if raw.startswith("tmdb://"):
        return raw.split("?", 1)[0]
    if raw.startswith("tvdb://"):
        return raw.split("?", 1)[0]
    if raw.startswith("anidb://"):
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
    """Return an IMDb, TMDb, TVDb or AniDB GUID for a Plex item."""
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
        for g in getattr(item, "guids", []) or []:
            val = _parse_guid_value(g.id)
            if val and val.startswith("anidb://"):
                return val
        if getattr(item, "guid", None):
            val = _parse_guid_value(item.guid)
            if val and val.startswith("imdb://"):
                return val
            if val and val.startswith("tmdb://"):
                return val
            if val and val.startswith("tvdb://"):
                return val
            if val and val.startswith("anidb://"):
                return val
    except Exception as exc:
        logger.debug("Failed retrieving IMDb GUID: %s", exc)
    return None


def get_show_from_library(plex, title):
    """Return a show object from any library section."""
    for sec in plex.library.sections():
        if sec.type == "show":
            try:
                # Intento 1: coincidencia exacta (rápido)
                return sec.get(title)
            except NotFound:
                # Intento 2: búsqueda flexible
                try:
                    results = sec.search(title=title)
                    if results:
                        return results[0]
                except Exception:
                    pass
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


def guid_to_ids(guid: Union[str, Tuple[str, str]]) -> Dict[str, Union[str, int]]:
    """Convert a Plex GUID to a Trakt ids mapping."""
    # Handle tuple format (used for episodes: (guid, episode_info))
    if isinstance(guid, tuple):
        guid = guid[0] if guid else ""
    
    # Handle string format
    if not isinstance(guid, str):
        return {}
        
    if guid.startswith("imdb://"):
        return {"imdb": guid.split("imdb://", 1)[1]}
    if guid.startswith("tmdb://"):
        return {"tmdb": int(guid.split("tmdb://", 1)[1])}
    if guid.startswith("tvdb://"):
        return {"tvdb": int(guid.split("tvdb://", 1)[1])}
    if guid.startswith("anidb://"):
        return {"anidb": int(guid.split("anidb://", 1)[1])}
    return {}



def trakt_movie_key(m: dict) -> Union[str, Tuple[str, Optional[int]]]:
    """Return a unique key for a Trakt movie object using IMDb, TMDb, TVDb o AniDB."""
    ids = m.get("ids", {})
    if ids.get("imdb"):
        return f"imdb://{ids['imdb']}"
    if ids.get("tmdb"):
        return f"tmdb://{ids['tmdb']}"
    if ids.get("tvdb"):
        return f"tvdb://{ids['tvdb']}"
    if ids.get("anidb"):
        return f"anidb://{ids['anidb']}"
    return m["title"].lower()


def episode_key(
    show: str, code: str, guid: Optional[str]
) -> Union[str, Tuple[str, str]]:
    if guid and (guid.startswith(("imdb://", "tmdb://", "tvdb://", "anidb://"))):
        return guid
    return (show.lower(), code)


def trakt_episode_key(show: dict, e: dict) -> Union[str, Tuple[str, str]]:
    ids = e.get("ids", {})
    if ids.get("imdb"):
        return f"imdb://{ids['imdb']}"
    if ids.get("tmdb"):
        return f"tmdb://{ids['tmdb']}"
    if ids.get("tvdb"):
        return f"tvdb://{ids['tvdb']}"
    if ids.get("anidb"):
        return f"anidb://{ids['anidb']}"
    return (
        show["title"].lower(),
        f"S{e['season']:02d}E{e['number']:02d}",
    )


def simkl_episode_key(show: dict, e: dict) -> Optional[Union[str, Tuple[str, str]]]:
    """Return best key for a Simkl episode object.

    Siempre que exista un identificador único a nivel de episodio (IMDb/TMDb/TVDb/AniDB)
    se utiliza dicho GUID como clave. Cuando no hay identificadores a nivel de
    episodio —algo bastante habitual en Simkl— se genera una clave compuesta
    (GUID_serie, "SxxEyy") para evitar que distintos episodios de una misma
    serie colisionen entre sí.
    """
    ids = e.get("ids", {}) or {}
    if ids.get("imdb"):
        return f"imdb://{ids['imdb']}"
    if ids.get("tmdb"):
        return f"tmdb://{ids['tmdb']}"
    if ids.get("tvdb"):
        return f"tvdb://{ids['tvdb']}"
    if ids.get("anidb"):
        return f"anidb://{ids['anidb']}"

    # -------------------------- Fallback -------------------------- #
    # Sin IDs de episodio: combinamos GUID (de la serie) + código epi.
    show_ids = show.get("ids", {}) or {}
    base_guid: Optional[str] = None
    if show_ids.get("imdb"):
        base_guid = f"imdb://{show_ids['imdb']}"
    elif show_ids.get("tmdb"):
        base_guid = f"tmdb://{show_ids['tmdb']}"
    elif show_ids.get("tvdb"):
        base_guid = f"tvdb://{show_ids['tvdb']}"
    elif show_ids.get("anidb"):
        base_guid = f"anidb://{show_ids['anidb']}"

    # Componer código SxxEyy
    season_num = e.get("season", 0)
    episode_num = e.get("number", 0)
    code = f"S{season_num:02d}E{episode_num:02d}"

    # Si no hay GUID de serie, usamos (titulo, code) como último recurso.
    if base_guid:
        return (base_guid, code)
    return (show.get("title", "").lower(), code)



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


def simkl_request(
    method: str,
    endpoint: str,
    headers: dict,
    *,
    retries: int = 2,
    timeout: int = 30,
    **kwargs,
) -> requests.Response:
    """Realiza una petición HTTP a la API de Simkl con reintentos y timeout adaptable.

    • `retries`  – número de reintentos ante ReadTimeout (por defecto 2 → 3 intentos totales).
    • `timeout`  – timeout inicial en segundos (por defecto 30 s). Cada reintento duplica el timeout.
    """

    url = f"https://api.simkl.com{endpoint}"

    # Extraer un timeout personalizado si viene en **kwargs** para mantener compatibilidad.
    if "timeout" in kwargs:
        timeout = kwargs.pop("timeout")  # se usará como valor inicial

    attempt = 0
    while True:
        try:
            resp = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.exceptions.ReadTimeout as exc:
            if attempt >= retries:
                logger.error("Simkl ReadTimeout tras %d intentos (%d s).", attempt + 1, timeout)
                raise
            attempt += 1
            timeout *= 2  # back-off exponencial
            logger.warning(
                "Simkl request %s %s agotó el tiempo (%s). Reintentando (%d/%d) con timeout=%ds…",
                method.upper(), endpoint, exc, attempt, retries, timeout,
            )
        except requests.exceptions.RequestException:
            # Para otros errores de red no merece volver a intentar; relanzamos.
            raise


def simkl_search_ids(
    headers: dict,
    title: str,
    *,
    is_movie: bool = True,
    year: Optional[int] = None,
) -> Dict[str, Union[str, int]]:
    """Buscar en Simkl por *title* y devolver un mapping de IDs.

    Si no se encuentra un resultado claro se devuelve un dict vacío.
    """
    endpoint = "/search/movies" if is_movie else "/search/shows"
    params = {"q": title, "limit": 1}
    # Algunos endpoints aceptan el parámetro `year` únicamente para películas.
    if year and is_movie:
        params["year"] = year
    try:
        resp = simkl_request("GET", endpoint, headers, params=params)
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Simkl search failed for '%s': %s", title, exc)
        return {}

    if not isinstance(data, list) or not data:
        return {}

    ids = data[0].get("ids", {}) or {}
    # Normalizar integer IDs
    for k, v in list(ids.items()):
        try:
            ids[k] = int(v) if str(v).isdigit() else v
        except Exception:
            pass
    return ids


def trakt_search_ids(
    headers: dict,
    title: str,
    *,
    is_movie: bool = True,
    year: Optional[int] = None,
) -> Dict[str, Union[str, int]]:
    """Search Trakt by title and return a mapping of IDs.

    If no clear result is found, returns an empty dict.
    """
    # Use text search endpoint
    params = {"query": title, "type": "movie" if is_movie else "show", "limit": 1}
    # Trakt search supports year filtering
    if year and is_movie:
        params["year"] = year
    try:
        resp = trakt_request("GET", "/search/text", headers, params=params)
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Trakt search failed for '%s': %s", title, exc)
        return {}

    if not isinstance(data, list) or not data:
        return {}

    # Extract the media item from the search result
    result = data[0]
    media_type = "movie" if is_movie else "show"
    media_item = result.get(media_type, {})
    
    ids = media_item.get("ids", {}) or {}
    # Normalize integer IDs
    for k, v in list(ids.items()):
        try:
            ids[k] = int(v) if str(v).isdigit() else v
        except Exception:
            pass
    return ids


def simkl_movie_key(m: dict) -> Optional[str]:
    """Return best GUID for a Simkl movie object."""
    ids = m.get("ids", {})
    if ids.get("imdb"):
        return f"imdb://{ids['imdb']}"
    if ids.get("tmdb"):
        return f"tmdb://{ids['tmdb']}"
    if ids.get("tvdb"):
        return f"tvdb://{ids['tvdb']}"
    if ids.get("anidb"):
        return f"anidb://{ids['anidb']}"
    return None


def get_simkl_history(
    headers: dict,
    *,
    date_from: Optional[str] = None,
) -> Tuple[
    Dict[str, Tuple[str, Optional[int], Optional[str]]],
    Dict[str, Tuple[str, str, Optional[str]]],
]:
    """Return Simkl movie and episode history keyed by best GUID.
    
    Returns:
        Tuple containing:
        - Movies: Dict[guid, (title, year, watched_at)]
        - Episodes: Dict[guid, (show_title, episode_code, watched_at)]
    """
    movies: Dict[str, Tuple[str, Optional[int], Optional[str]]] = {}
    episodes: Dict[str, Tuple[str, str, Optional[str]]] = {}
    
    # First, get movies from sync/history (watched history)
    params = {"limit": 100, "type": "movies"}
    if date_from:
        params["date_from"] = date_from
    page = 1
    logger.info("Fetching Simkl watch history…")
    while True:
        params["page"] = page
        resp = simkl_request(
            "GET",
            "/sync/history",
            headers,
            params=params,
        )
        data = resp.json()
        if not isinstance(data, list) or not data:
            break
        for item in data:
            m = item.get("movie", {})
            guid = simkl_movie_key(m)
            if not guid:
                continue
            if guid not in movies:
                movies[guid] = (
                    m.get("title"),
                    normalize_year(m.get("year")),
                    item.get("watched_at"),
                )
        page += 1
    
    # Get episodes from sync/history
    params = {"limit": 100, "type": "episodes"}
    if date_from:
        params["date_from"] = date_from
    page = 1
    logger.info("Fetching Simkl episode history…")
    while True:
        params["page"] = page
        resp = simkl_request(
            "GET",
            "/sync/history",
            headers,
            params=params,
        )
        data = resp.json()
        if not isinstance(data, list) or not data:
            break
        for item in data:
            e = item.get("episode", {})
            show = item.get("show", {})
            guid = simkl_episode_key(show, e)
            if not guid:
                continue
            if guid not in episodes:
                episodes[guid] = (
                    show.get("title"),
                    f"S{e.get('season', 0):02d}E{e.get('number', 0):02d}",
                    item.get("watched_at"),
                )
        page += 1
    
    # Then, get movies from sync/all-items to include completed movies
    logger.info("Fetching Simkl all-items (full)…")
    resp = simkl_request(
        "GET",
        "/sync/all-items",
        headers,
        params={"extended": "full", "episode_watched_at": "yes"},
    )
    data = resp.json()
    if data and isinstance(data, dict):
        completed_movies = data.get("movies", [])
        for movie_item in completed_movies:
            m = movie_item.get("movie", {})
            guid = simkl_movie_key(m)
            if not guid:
                continue
            if guid not in movies:
                # For completed movies, use last_watched_at if available
                watched_at = movie_item.get("last_watched_at")
                movies[guid] = (
                    m.get("title"),
                    normalize_year(m.get("year")),
                    watched_at,
                )
        
        # Get completed episodes from shows in all-items
        completed_shows = data.get("shows", [])
        for show_item in completed_shows:
            show = show_item.get("show", {})
            seasons = show_item.get("seasons", [])
            for season in seasons:
                season_num = season.get("number", 0)
                season_episodes = season.get("episodes", [])
                for episode in season_episodes:
                    # Determinar si el episodio está visto:
                    # 1. Si viene `watched_at`, asumimos visto.
                    # 2. Si no, usamos la métrica `plays` (reproducido ≥1).
                    # 3. Si tampoco hay `plays`, comprobamos `watched` (bool).
                    if not (
                        episode.get("watched_at")
                        or episode.get("plays", 0) > 0
                        or episode.get("watched")
                    ):
                        # No hay indicios de reproducción → saltar
                        continue

                    episode_num = episode.get("number", 0)

                    # Crear un objeto "episode" compatible con simkl_episode_key
                    e = {
                        "season": season_num,
                        "number": episode_num,
                        "ids": episode.get("ids", {}),
                    }

                    guid = simkl_episode_key(show, e)
                    if not guid:
                        continue

                    if guid not in episodes:
                        episodes[guid] = (
                            show.get("title"),
                            f"S{season_num:02d}E{episode_num:02d}",
                            episode.get("watched_at"),
                        )
    
    return movies, episodes


def update_simkl(
    headers: dict,
    movies: List[Tuple[str, Optional[int], Optional[str], Optional[str]]],
    episodes: List[Tuple[str, str, Optional[str], Optional[str]]],
) -> None:
    """Add new items to Simkl history con búsqueda de IDs de respaldo."""
    payload = {}
    if movies:
        payload["movies"] = []
        for title, year, guid, watched_at in movies:
            item = {"title": title, "year": normalize_year(year)}
            ids = guid_to_ids(guid) if guid else {}
            if not ids:
                ids = simkl_search_ids(headers, title, is_movie=True, year=year)
                if ids:
                    logger.debug("IDs found in Simkl for movie '%s': %s", title, ids)
            if ids:
                item["ids"] = ids
            if watched_at:
                item["watched_at"] = watched_at
            payload["movies"].append(item)

    if episodes:
        shows: Dict[str, dict] = {}
        for show_title, code, guid, watched_at in episodes:
            # Intentar obtener IDs de la serie
            ids = guid_to_ids(guid) if guid else {}
            if not ids:
                ids = simkl_search_ids(headers, show_title, is_movie=False)
                if ids:
                    logger.debug("IDs found in Simkl for show '%s': %s", show_title, ids)
            if not ids:
                logger.warning(
                    "Skipping episode '%s - %s' - no IDs found", show_title, code
                )
                continue

            key = tuple(sorted(ids.items()))  # clave única para la serie
            if key not in shows:
                shows[key] = {
                    "title": show_title,
                    "ids": ids,
                    "seasons": [],
                }

            try:
                season_num, episode_num = map(int, code.upper().lstrip("S").split("E"))
            except ValueError:
                logger.warning("Invalid episode code format: %s", code)
                continue

            season_found = False
            for s in shows[key]["seasons"]:
                if s["number"] == season_num:
                    s["episodes"].append({"number": episode_num, "watched_at": watched_at})
                    season_found = True
                    break

            if not season_found:
                shows[key]["seasons"].append(
                    {
                        "number": season_num,
                        "episodes": [{"number": episode_num, "watched_at": watched_at}],
                    }
                )
        if shows:
            payload["shows"] = list(shows.values())

    if not payload:
        logger.info("Nothing new to sync with Simkl")
        return

    logger.info(
        "Adding %d movies and %d shows to Simkl history",
        len(payload.get("movies", [])),
        len(payload.get("shows", [])),
    )
    try:
        response = simkl_request(
            "post", "/sync/history", headers, json=payload
        )
        # Simkl puede devolver 429 incluso en éxito, comprobaremos el cuerpo
        if response.status_code == 429:
            try:
                data = response.json()
                if data.get("message") == "Success!":
                    logger.info("Simkl returned 429 but reported success.")
                    return
            except json.JSONDecodeError:
                pass  # no es JSON, continuar como error
        response.raise_for_status()
        logger.info("Simkl history updated successfully.")
    except requests.exceptions.RequestException as e:
        logger.error("Failed to update Simkl: %s", e)


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
    # Cache para no abrir la biblioteca en cada episodio cuando necesitamos el GUID de la serie
    show_guid_cache: Dict[str, Optional[str]] = {}

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

            # ------------------- Determinar clave normalizada ------------------- #

            # Preferimos usar GUID de serie + codigo para coincidir con Simkl.
            series_guid: Optional[str] = None

            # (1) GUID directo desde el episodio
            if item is not None:
                gp_guid_raw = getattr(item, "grandparentGuid", None)
                if gp_guid_raw:
                    series_guid = _parse_guid_value(gp_guid_raw)

            # (2) consulta a la cache
            if series_guid is None and show in show_guid_cache:
                series_guid = show_guid_cache[show]

            # (3) busqueda en biblioteca si todavia no tenemos GUID
            if series_guid is None and show:
                series_obj = get_show_from_library(plex, show)
                series_guid = imdb_guid(series_obj) if series_obj else None
                show_guid_cache[show] = series_guid

            if series_guid and valid_guid(series_guid):
                key = (series_guid, code)
            elif guid:
                key = guid
            else:
                key = (show.lower() if show else "", code)
            if key not in episodes:
                episodes[key] = {
                    "show": show,
                    "code": code,
                    "watched_at": watched_at,
                    "guid": guid,  # puede ser None cuando no hay GUID
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
                    show_title = getattr(ep, "grandparentTitle", None)

                    key: Union[str, Tuple[str, str]]
                    if guid:
                        key = guid
                    else:
                        series_guid: Optional[str] = None

                        # (1) From episode's grandparentGuid
                        gp_guid_raw = getattr(ep, "grandparentGuid", None)
                        if gp_guid_raw:
                            series_guid = _parse_guid_value(gp_guid_raw)

                        # (2) From cache
                        if series_guid is None and show_title and show_title in show_guid_cache:
                            series_guid = show_guid_cache[show_title]

                        # (3) From library search if still no GUID
                        if series_guid is None and show_title:
                            series_obj = get_show_from_library(plex, show_title)
                            series_guid = (
                                imdb_guid(series_obj) if series_obj else None
                            )
                            show_guid_cache[show_title] = series_guid

                        # Key composition
                        if series_guid and valid_guid(series_guid):
                            key = (series_guid, code)
                        else:
                            key = (show_title.lower() if show_title else "", code)

                    if key not in episodes:
                        episodes[key] = {
                            "show": show_title,
                            "code": code,
                            "watched_at": to_iso_z(getattr(ep, "lastViewedAt", None)),
                            "guid": guid,
                        }
        except Exception as exc:
            logger.debug(
                "Failed fetching watched items from section %s: %s", section.title, exc
            )

    return movies, episodes


def get_trakt_history_basic(
    headers: dict,
) -> Tuple[
    Dict[str, Tuple[str, Optional[int]]],
    Dict[str, Tuple[str, str]],
]:
    """Return Trakt history keyed by IMDb/TMDb GUID for movies and episodes."""
    movies: Dict[str, Tuple[str, Optional[int]]] = {}
    episodes: Dict[str, Tuple[str, str]] = {}

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
                    year = int(m["year"]) if m.get("year") else None
                    movies[guid] = (m["title"], year)
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
                    )
        page += 1

    return movies, episodes


def update_trakt(
    headers: dict,
    movies: List[Tuple[str, Optional[int], Optional[str], Optional[str]]],
    episodes: List[Tuple[str, str, Optional[str], Optional[str]]],
) -> None:
    """Send watched history to Trakt."""
    payload = {"movies": [], "episodes": []}

    # Movies
    for title, year, watched_at, guid in movies:
        movie_obj = {"title": title}
        if year is not None:
            movie_obj["year"] = year
        if guid:
            movie_obj["ids"] = guid_to_ids(guid)
        if watched_at:
            movie_obj["watched_at"] = watched_at
        payload["movies"].append(movie_obj)

    # Episodes
    for show, code, watched_at, guid in episodes:
        try:
            season = int(code[1:3])
            number = int(code[4:6])
        except (ValueError, IndexError):
            logger.warning("Invalid episode code format: %s", code)
            continue
            
        ep_obj = {"season": season, "number": number}
        if guid:
            ep_obj["ids"] = guid_to_ids(guid)
        if watched_at:
            ep_obj["watched_at"] = watched_at
        payload["episodes"].append(ep_obj)

    if not payload["movies"] and not payload["episodes"]:
        logger.info("Nothing new to send to Trakt.")
        return

    logger.info(
        "Sent %d movies and %d episodes to Trakt",
        len(payload["movies"]),
        len(payload["episodes"]),
    )
    try:
        trakt_request("POST", "/sync/history", headers, json=payload)
        logger.info("Trakt history updated successfully.")
    except requests.exceptions.RequestException as e:
        logger.error("Failed to update Trakt history: %s", e)


def update_simkl(
    headers: dict,
    movies: List[Tuple[str, Optional[int], Optional[str], Optional[str]]],
    episodes: List[Tuple[str, str, Optional[str], Optional[str]]],
) -> None:
    """Add new items to Simkl history con búsqueda de IDs de respaldo."""
    payload = {}
    if movies:
        payload["movies"] = []
        for title, year, guid, watched_at in movies:
            item = {"title": title, "year": normalize_year(year)}
            ids = guid_to_ids(guid) if guid else {}
            if not ids:
                ids = simkl_search_ids(headers, title, is_movie=True, year=year)
                if ids:
                    logger.debug("IDs found in Simkl for movie '%s': %s", title, ids)
            if ids:
                item["ids"] = ids
            if watched_at:
                item["watched_at"] = watched_at
            payload["movies"].append(item)

    if episodes:
        shows: Dict[str, dict] = {}
        for show_title, code, guid, watched_at in episodes:
            # Intentar obtener IDs de la serie
            ids = guid_to_ids(guid) if guid else {}
            if not ids:
                ids = simkl_search_ids(headers, show_title, is_movie=False)
                if ids:
                    logger.debug("IDs found in Simkl for show '%s': %s", show_title, ids)
            if not ids:
                logger.warning(
                    "Skipping episode '%s - %s' - no IDs found", show_title, code
                )
                continue

            key = tuple(sorted(ids.items()))  # clave única para la serie
            if key not in shows:
                shows[key] = {
                    "title": show_title,
                    "ids": ids,
                    "seasons": [],
                }

            try:
                season_num, episode_num = map(int, code.upper().lstrip("S").split("E"))
            except ValueError:
                logger.warning("Invalid episode code format: %s", code)
                continue

            season_found = False
            for s in shows[key]["seasons"]:
                if s["number"] == season_num:
                    s["episodes"].append({"number": episode_num, "watched_at": watched_at})
                    season_found = True
                    break

            if not season_found:
                shows[key]["seasons"].append(
                    {
                        "number": season_num,
                        "episodes": [{"number": episode_num, "watched_at": watched_at}],
                    }
                )
        if shows:
            payload["shows"] = list(shows.values())

    if not payload:
        logger.info("Nothing new to sync with Simkl")
        return

    logger.info(
        "Adding %d movies and %d shows to Simkl history",
        len(payload.get("movies", [])),
        len(payload.get("shows", [])),
    )
    try:
        response = simkl_request(
            "post", "/sync/history", headers, json=payload
        )
        # Simkl puede devolver 429 incluso en éxito, comprobaremos el cuerpo
        if response.status_code == 429:
            try:
                data = response.json()
                if data.get("message") == "Success!":
                    logger.info("Simkl returned 429 but reported success.")
                    return
            except json.JSONDecodeError:
                pass  # no es JSON, continuar como error
        response.raise_for_status()
        logger.info("Simkl history updated successfully.")
    except requests.exceptions.RequestException as e:
        logger.error("Failed to update Simkl: %s", e)


# --------------------------------------------------------------------------- #
# SCHEDULER TASK
# --------------------------------------------------------------------------- #
def sync():
    """Run the main synchronization logic."""
    if not test_connections():
        logger.error("Sync cancelled due to connection errors.")
        return

    logger.info("Starting sync...")
    trakt_enabled = os.path.exists(TOKEN_FILE)
    simkl_enabled = os.path.exists(SIMKL_TOKEN_FILE)

    headers = {}
    if SYNC_PROVIDER == "trakt" and trakt_enabled:
        if not refresh_trakt_token():
            logger.error("Failed to refresh Trakt token. Aborting sync.")
            return
        load_trakt_tokens()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ.get('TRAKT_ACCESS_TOKEN')}",
            "trakt-api-version": "2",
            "trakt-api-key": os.environ["TRAKT_CLIENT_ID"],
        }
    elif SYNC_PROVIDER == "simkl" and simkl_enabled:
        load_simkl_tokens()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ.get('SIMKL_ACCESS_TOKEN')}",
            "simkl-api-key": os.environ["SIMKL_CLIENT_ID"],
        }

    plex_movies, plex_episodes = get_plex_history(plex)
    logger.info(
        "Found %d movies and %d episodes in Plex history.",
        len(plex_movies),
        len(plex_episodes),
    )

    try:
        if SYNC_PROVIDER == "trakt":
            logger.info("Provider: Trakt")
            last_sync = load_trakt_last_sync_date()
            current_activity = get_trakt_last_activity(headers)
            if last_sync and current_activity and current_activity == last_sync:
                logger.info("No new Trakt activity since %s", last_sync)
                trakt_movies, trakt_episodes = {}, {}
            else:
                try:
                    trakt_movies, trakt_episodes = get_trakt_history(
                        headers, date_from=last_sync
                    )
                except Exception as exc:
                    logger.error("Failed to retrieve Trakt history: %s", exc)
                    trakt_movies, trakt_episodes = {}, {}
            plex_movie_guids = set(plex_movies.keys())
            plex_episode_guids = set(plex_episodes.keys())
            trakt_movie_guids = set(trakt_movies.keys())
            trakt_episode_guids = set(trakt_episodes.keys())

            logger.info(
                "Plex history:   %d movies, %d episodes",
                len(plex_movie_guids),
                len(plex_episode_guids),
            )
            logger.info(
                "Trakt history:  %d movies, %d episodes",
                len(trakt_movie_guids),
                len(trakt_episode_guids),
            )

            if last_sync:
                new_movies = [
                    (data["title"], data["year"], data.get("watched_at"), guid)
                    for guid, data in plex_movies.items()
                    if data.get("watched_at") and data["watched_at"] > last_sync
                ]
                new_episodes = [
                    (data["show"], data["code"], data.get("watched_at"), guid)
                    for guid, data in plex_episodes.items()
                    if data.get("watched_at") and data["watched_at"] > last_sync
                ]
            else:
                new_movies = [
                    (data["title"], data["year"], data.get("watched_at"), guid)
                    for guid, data in plex_movies.items()
                    if guid not in trakt_movie_guids
                ]
                new_episodes = [
                    (data["show"], data["code"], data.get("watched_at"), guid)
                    for guid, data in plex_episodes.items()
                    if guid not in trakt_episode_guids
                ]

            if SYNC_WATCHED:
                try:
                    update_trakt(headers, new_movies, new_episodes)
                except Exception as exc:
                    logger.error("Failed updating Trakt history: %s", exc)

            missing_movies = {
                (title, year, guid)
                for guid, (title, year) in trakt_movies.items()
                if guid not in plex_movie_guids
            }
            missing_episodes = {
                (show, code, guid)
                for guid, (show, code) in trakt_episodes.items()
                if guid not in plex_episode_guids
            }
            try:
                update_plex(plex, missing_movies, missing_episodes)
            except Exception as exc:
                logger.error("Failed updating Plex history: %s", exc)

            if SYNC_LIKED_LISTS:
                try:
                    sync_liked_lists(plex, headers)
                    sync_collections_to_trakt(plex, headers)
                except TraktAccountLimitError as exc:
                    logger.error("Liked-lists sync skipped: %s", exc)
                except Exception as exc:
                    logger.error("Liked-lists sync failed: %s", exc)
            if SYNC_WATCHLISTS:
                try:
                    sync_watchlist(
                        plex,
                        headers,
                        plex_movie_guids | plex_episode_guids,
                        trakt_movie_guids | trakt_episode_guids,
                    )
                except TraktAccountLimitError as exc:
                    logger.error("Watchlist sync skipped: %s", exc)
                except Exception as exc:
                    logger.error("Watchlist sync failed: %s", exc)

            if current_activity:
                save_trakt_last_sync_date(current_activity)

        elif SYNC_PROVIDER == "simkl":
            logger.info("Provider: Simkl")
            last_sync = load_last_sync_date()
            current_activity = get_last_activity(headers)
            if last_sync and current_activity and current_activity == last_sync:
                logger.info("No new Simkl activity since %s", last_sync)
                simkl_movies, simkl_episodes = {}, {}
            else:
                simkl_movies, simkl_episodes = get_simkl_history(
                    headers, date_from=last_sync
                )
                logger.info(
                    "Retrieved %d movies and %d episodes from Simkl",
                    len(simkl_movies),
                    len(simkl_episodes),
                )

            # Plex -> Simkl
            if last_sync:
                movies_to_add = {
                    k for k, v in plex_movies.items()
                    if v.get("watched_at") and v["watched_at"] > last_sync
                }
                episodes_to_add = {
                    k for k, v in plex_episodes.items()
                    if v.get("watched_at") and v["watched_at"] > last_sync
                }
            else:
                movies_to_add = set(plex_movies)
                episodes_to_add = set(plex_episodes)

            logger.info(
                "Found %d movies and %d episodes to add to Simkl",
                len(movies_to_add),
                len(episodes_to_add),
            )

            movies_to_add_fmt = [
                (
                    plex_movies[m]["title"],
                    plex_movies[m]["year"],
                    m,
                    plex_movies[m].get("watched_at"),
                )
                for m in movies_to_add
            ]
            # Para cada episodio necesitamos el GUID de la SERIE (no el del episodio) para que Simkl pueda identificarla correctamente.
            episodes_to_add_fmt = []
            for e in episodes_to_add:
                ep_info = plex_episodes[e]
                show_title = ep_info["show"]
                code = ep_info["code"]
                watched_at = ep_info.get("watched_at")

                # Intentamos obtener la serie desde la biblioteca de Plex para extraer un GUID válido (imdb/tmdb/tvdb) a nivel de serie.
                show_guid = None
                try:
                    show_obj = get_show_from_library(plex, show_title)
                    if show_obj:
                        show_guid = imdb_guid(show_obj) or best_guid(show_obj)
                except Exception as exc:
                    logger.debug("Failed to obtain GUID for show %s: %s", show_title, exc)

                # Si seguimos sin GUID de serie, recurrimos al GUID del episodio como último recurso.
                if show_guid is None:
                    show_guid = e

                episodes_to_add_fmt.append(
                    (
                        show_title,
                        code,
                        show_guid,
                        watched_at,
                    )
                )
            
            if movies_to_add_fmt or episodes_to_add_fmt:
                update_simkl(headers, movies_to_add_fmt, episodes_to_add_fmt)

            # Plex <- Simkl
            movies_to_add_plex = set(simkl_movies) - set(plex_movies)
            episodes_to_add_plex = set(simkl_episodes) - set(plex_episodes)
            logger.info(
                "Found %d movies and %d episodes to add to Plex",
                len(movies_to_add_plex),
                len(episodes_to_add_plex),
            )
            movies_to_add_plex_fmt = {
                (simkl_movies[m][0], simkl_movies[m][1], m)
                for m in movies_to_add_plex
            }
            episodes_to_add_plex_fmt = {
                (simkl_episodes[e][0], simkl_episodes[e][1], e)
                for e in episodes_to_add_plex
            }
            if movies_to_add_plex_fmt or episodes_to_add_plex_fmt:
                update_plex(plex, movies_to_add_plex_fmt, episodes_to_add_plex_fmt)

            if current_activity:
                save_last_sync_date(current_activity)

    except Exception as exc:  # noqa: BLE001
        logger.error("Error during sync: %s", exc)

    # Sincronizar valoraciones solo es compatible con Trakt por ahora
    if SYNC_RATINGS and SYNC_PROVIDER == "trakt":
        sync_ratings(plex, headers)
    elif SYNC_RATINGS and SYNC_PROVIDER == "simkl":
        logger.warning("Ratings sync with Simkl is not yet supported.")

    if SYNC_WATCHLISTS and SYNC_PROVIDER == "trakt":
        sync_watchlist(plex, headers, plex_movies, trakt_movies)
    elif SYNC_WATCHLISTS and SYNC_PROVIDER == "simkl":
        logger.warning("Watchlist sync with Simkl is not yet supported.")

    if SYNC_COLLECTION:
        sync_collection(plex, headers)

    if SYNC_LIKED_LISTS and SYNC_PROVIDER == "trakt":
        sync_liked_lists(plex, headers)
    elif SYNC_LIKED_LISTS and SYNC_PROVIDER == "simkl":
        logger.warning("Liked lists sync with Simkl is not yet supported.")

    if SYNC_PROVIDER == "trakt":
        sync_collections_to_trakt(plex, headers)
    elif SYNC_PROVIDER == "simkl":
        logger.warning("Plex Collections sync to Simkl is not yet supported.")

    logger.info("Sync finished.")


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

    # Change interval and start sync when requested
    if request.method == "POST":
        minutes = int(request.form.get("minutes", 60))
        SYNC_INTERVAL_MINUTES = minutes
        SYNC_COLLECTION = request.form.get("collection") is not None
        SYNC_RATINGS = request.form.get("ratings") is not None
        SYNC_WATCHED = request.form.get("watched") is not None
        SYNC_LIKED_LISTS = request.form.get("liked_lists") is not None
        SYNC_WATCHLISTS = request.form.get("watchlists") is not None
        LIVE_SYNC = request.form.get("live_sync") is not None

        if SYNC_PROVIDER == "simkl":
            SYNC_COLLECTION = False
            SYNC_RATINGS = False
            SYNC_LIKED_LISTS = False
            SYNC_WATCHLISTS = False
            LIVE_SYNC = False
        
        # Only start scheduler when manually requested from sync tab
        start_scheduler()
        
        # Trigger an immediate sync by updating the next run time
        job = scheduler.get_job("sync_job")
        if job:
            scheduler.modify_job("sync_job", next_run_time=datetime.now())
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
        provider=SYNC_PROVIDER,
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
        # Removed automatic scheduler start - only manual start from sync tab
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
            # Removed automatic scheduler start - only manual start from sync tab
            return redirect(url_for("config_page"))
        if service == "simkl" and code and exchange_code_for_simkl_tokens(code):
            if SYNC_PROVIDER == "none":
                save_provider("simkl")
            # Removed automatic scheduler start - only manual start from sync tab
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
    global plex
    plex_baseurl = os.environ.get("PLEX_BASEURL")
    plex_token = os.environ.get("PLEX_TOKEN")
    trakt_token = os.environ.get("TRAKT_ACCESS_TOKEN")
    trakt_client_id = os.environ.get("TRAKT_CLIENT_ID")
    trakt_enabled = SYNC_PROVIDER == "trakt" and bool(trakt_token and trakt_client_id)
    simkl_token = os.environ.get("SIMKL_ACCESS_TOKEN")
    simkl_client_id = os.environ.get("SIMKL_CLIENT_ID")
    simkl_enabled = SYNC_PROVIDER == "simkl" and bool(simkl_token and simkl_client_id)

    if not all([plex_baseurl, plex_token]):
        logger.error("Missing environment variables for Plex.")
        return False
    if not trakt_enabled and not simkl_enabled:
        logger.error("Missing environment variables for selected provider.")
        return False

    try:
        plex = PlexServer(plex_baseurl, plex_token)
        plex.account()
        logger.info("Successfully connected to Plex.")
    except Exception as exc:
        logger.error("Failed to connect to Plex: %s", exc)
        plex = None
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
        headers = {
            "Authorization": f"Bearer {simkl_token}",
            "Content-Type": "application/json",
            "User-Agent": "PlexyTrackt/1.0.0",
            "simkl-api-key": simkl_client_id,
        }
        try:
            simkl_request("GET", "/sync/history", headers, params={"limit": 1})
            logger.info("Successfully connected to Simkl.")
        except Exception as exc:
            logger.error("Failed to connect to Simkl: %s", exc)
            return False


    return True


def start_scheduler():
    """Arranca o reinicia el scheduler garantizando **un único** job activo.

    1. Si el scheduler no está corriendo se hace un *start* tras validar
       conexiones.
    2. Antes de añadir el nuevo trabajo se eliminan TODOS los jobs existentes
       para evitar duplicados.
    """
    # Iniciamos el scheduler si no está corriendo todavía
    if not scheduler.running:
        if not test_connections():
            logger.error("Connection test failed. Scheduler will not start.")
            return
        scheduler.start()
        logger.info("Scheduler started")

    # Eliminamos cualquier job existente para garantizar que sólo haya uno
    for job in scheduler.get_jobs():
        scheduler.remove_job(job.id)
    logger.info("Removed existing scheduled job(s)")

    # Añadimos el nuevo trabajo periódico
    scheduler.add_job(
        sync,
        "interval",
        minutes=SYNC_INTERVAL_MINUTES,
        id="sync_job",
        replace_existing=True,
    )
    logger.info("Sync job scheduled with interval %d minutes", SYNC_INTERVAL_MINUTES)


def stop_scheduler():
    """Detiene y elimina el job de sincronización dejando el scheduler limpio."""
    for job in scheduler.get_jobs():
        scheduler.remove_job(job.id)
    if scheduler.running and not scheduler.get_jobs():
        # Si no quedan trabajos activos podemos apagar el scheduler
        scheduler.shutdown(wait=False)
    logger.info("Synchronization job(s) stopped")


# --------------------------------------------------------------------------- #
# MAIN
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    logger.info("Starting PlexyTrackt application")
    load_trakt_tokens()
    load_simkl_tokens()
    load_provider()
    # Removed automatic scheduler start - only manual start from sync tab
    # start_scheduler()
    # Disable Flask's auto-reloader to avoid duplicate logs
    app.run(host="0.0.0.0", port=5030, use_reloader=False)
