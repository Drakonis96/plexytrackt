import logging
from datetime import datetime, timezone
from numbers import Number
from typing import Dict, Optional, Tuple, Union
from plexapi.exceptions import NotFound

logger = logging.getLogger(__name__)


def to_iso_z(value) -> Optional[str]:
    """Convert any ``viewedAt`` variant to ISO-8601 UTC ("...Z")."""
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    if isinstance(value, Number):
        return datetime.utcfromtimestamp(value).isoformat() + "Z"

    if isinstance(value, str):
        try:
            return datetime.utcfromtimestamp(int(value)).isoformat() + "Z"
        except (TypeError, ValueError):
            pass
        try:
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
    try:
        # Handle URL formats with no separator between server address and GUID
        if "http" in raw and "://" in raw:
            # Extract potential GUIDs after removing the http/https part
            if "imdb://" in raw:
                raw = "imdb://" + raw.split("imdb://", 1)[1]
            elif "themoviedb://" in raw:
                raw = "themoviedb://" + raw.split("themoviedb://", 1)[1]
            elif "thetvdb://" in raw:
                raw = "thetvdb://" + raw.split("thetvdb://", 1)[1]
            elif "tmdb://" in raw:
                raw = "tmdb://" + raw.split("tmdb://", 1)[1]
            elif "tvdb://" in raw:
                raw = "tvdb://" + raw.split("tvdb://", 1)[1]
            elif "anidb://" in raw:
                raw = "anidb://" + raw.split("anidb://", 1)[1]
    except Exception as exc:
        logger.debug("Failed to extract GUID from malformed URL: %s - %s", raw, exc)
    
    # Process cleaned GUID strings
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
        # Log the raw GUIDs for debugging
        guids = getattr(item, "guids", []) or []
        if guids:
            logger.debug("Raw GUIDs from Plex: %s", [g.id for g in guids])
            
        for g in guids:
            val = _parse_guid_value(g.id)
            if val and val.startswith("imdb://"):
                return val
        for g in guids:
            val = _parse_guid_value(g.id)
            if val and val.startswith("tmdb://"):
                return val
        for g in guids:
            if val and val.startswith("tvdb://"):
                return val
        for g in getattr(item, "guids", []) or []:
            val = _parse_guid_value(g.id)
            if val and val.startswith("anidb://"):
                return val
        if getattr(item, "guid", None):
            # Log the raw primary GUID for debugging
            raw_guid = item.guid
            logger.debug("Raw primary GUID from Plex: %s", raw_guid)
            
            val = _parse_guid_value(raw_guid)
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
                return sec.get(title)
            except NotFound:
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


def movie_key(title: str, year: Optional[int], guid: Optional[str]) -> Union[str, Tuple[str, Optional[int]]]:
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


def valid_guid(guid: Optional[Union[str, Tuple]]) -> bool:
    """Return True if ``guid`` is a valid IMDb, TMDb, TVDb or AniDB identifier."""
    if not guid:
        return False
    
    # Handle tuple format (used for episodes: (guid, episode_info))
    if isinstance(guid, tuple):
        guid = guid[0] if guid else ""
    
    # Handle string format
    if isinstance(guid, str):
        return guid.startswith(("imdb://", "tmdb://", "tvdb://", "anidb://"))
    
    return False


def trakt_movie_key(m: dict) -> Union[str, Tuple[str, Optional[int]]]:
    """Return a unique key for a Trakt movie object."""
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


def episode_key(show: str, code: str, guid: Optional[str]) -> Union[str, Tuple[str, str]]:
    if guid and guid.startswith(("imdb://", "tmdb://", "tvdb://", "anidb://")):
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
    return (show["title"].lower(), f"S{e['season']:02d}E{e['number']:02d}")


def simkl_episode_key(show: dict, e: dict) -> Optional[Union[str, Tuple[str, str]]]:
    """Return best key for a Simkl episode object."""
    ids = e.get("ids", {}) or {}
    if ids.get("imdb"):
        return f"imdb://{ids['imdb']}"
    if ids.get("tmdb"):
        return f"tmdb://{ids['tmdb']}"
    if ids.get("tvdb"):
        return f"tvdb://{ids['tvdb']}"
    if ids.get("anidb"):
        return f"anidb://{ids['anidb']}"

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

    season_num = e.get("season", 0)
    episode_num = e.get("number", 0)
    code = f"S{season_num:02d}E{episode_num:02d}"

    if base_guid:
        return (base_guid, code)
    return (show.get("title", "").lower(), code)
