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

import requests
from flask import Flask, render_template, request, redirect, url_for
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

SYNC_INTERVAL_MINUTES = 60           # default frequency
SYNC_COLLECTION = True
SYNC_RATINGS = True
SYNC_WATCHED = True              # ahora sí se respeta este flag
SYNC_LIKED_LISTS = False
SYNC_WATCHLISTS = False
scheduler = BackgroundScheduler()
plex = None  # will hold PlexServer instance

# --------------------------------------------------------------------------- #
# TRAKT OAUTH CONSTANTS
# --------------------------------------------------------------------------- #
TRAKT_REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"
TOKEN_FILE = "trakt_tokens.json"

# --------------------------------------------------------------------------- #
# UTILITIES
# --------------------------------------------------------------------------- #
def to_iso_z(value) -> Optional[str]:
    """Convert any ``viewedAt`` variant to ISO-8601 UTC ("...Z")."""
    if value is None:
        return None

    if isinstance(value, datetime):                       # datetime / pendulum / arrow
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    if isinstance(value, Number):                        # int / float
        return datetime.utcfromtimestamp(value).isoformat() + "Z"

    if isinstance(value, str):                           # str
        try:                                             # integer as text
            return datetime.utcfromtimestamp(int(value)).isoformat() + "Z"
        except (TypeError, ValueError):
            pass
        try:                                             # ISO string
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
    """Return the IMDb GUID for a Plex item if available, otherwise TMDb."""
    try:
        for g in getattr(item, "guids", []) or []:
            val = _parse_guid_value(g.id)
            if val and val.startswith("imdb://"):
                return val
        for g in getattr(item, "guids", []) or []:
            val = _parse_guid_value(g.id)
            if val and val.startswith("tmdb://"):
                return val
        if getattr(item, "guid", None):
            val = _parse_guid_value(item.guid)
            if val and val.startswith("imdb://"):
                return val
            if val and val.startswith("tmdb://"):
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


def movie_key(title: str, year: Optional[int], guid: Optional[str]) -> Union[str, Tuple[str, Optional[int]]]:
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


def trakt_movie_key(m: dict) -> Union[str, Tuple[str, Optional[int]]]:
    """Return a unique key for a Trakt movie object using IMDb or TMDb."""
    ids = m.get("ids", {})
    if ids.get("imdb"):
        return f"imdb://{ids['imdb']}"
    if ids.get("tmdb"):
        return f"tmdb://{ids['tmdb']}"
    return m["title"].lower()


def episode_key(show: str, code: str, guid: Optional[str]) -> Union[str, Tuple[str, str]]:
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
                {"access_token": access_token, "refresh_token": refresh_token}, f, indent=2
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
        "redirect_uri": TRAKT_REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    try:
        resp = requests.post("https://api.trakt.tv/oauth/token", json=payload, timeout=30)
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
        "redirect_uri": TRAKT_REDIRECT_URI,
        "grant_type": "refresh_token",
    }
    try:
        resp = requests.post("https://api.trakt.tv/oauth/token", json=payload, timeout=30)
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


def trakt_request(method: str, endpoint: str, headers: dict, **kwargs) -> requests.Response:
    url = f"https://api.trakt.tv{endpoint}"
    resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)
    if resp.status_code == 401:                       # token expired
        new_token = refresh_trakt_token()
        if new_token:
            headers["Authorization"] = f"Bearer {new_token}"
            resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)
    resp.raise_for_status()
    return resp


# --------------------------------------------------------------------------- #
# PLEX ↔ TRAKT
# --------------------------------------------------------------------------- #
def get_plex_history(
    plex
) -> Tuple[
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
                logger.debug("Failed to fetch movie %s from Plex: %s", entry.ratingKey, exc)
                continue

            title = item.title
            year = normalize_year(getattr(item, "year", None))
            guid = imdb_guid(item)
            if not guid:
                continue
            if guid not in movies:
                movies[guid] = {"title": title, "year": year, "watched_at": watched_at, "guid": guid}

        # Episodes
        elif entry.type == "episode":
            season = getattr(entry, "parentIndex", None)
            number = getattr(entry, "index", None)
            show = getattr(entry, "grandparentTitle", None)
            try:
                item = entry.source() or plex.fetchItem(entry.ratingKey)
            except Exception as exc:
                logger.debug("Failed to fetch episode %s from Plex: %s", entry.ratingKey, exc)
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
                episodes[guid] = {"show": show, "code": code, "watched_at": watched_at, "guid": guid}

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
            logger.debug("Failed fetching watched items from section %s: %s", section.title, exc)

    return movies, episodes


def get_trakt_history(
    headers: dict,
) -> Tuple[
        Dict[str, Tuple[str, Optional[int]]],
        Dict[str, Tuple[str, str]],
    ]:
    """Return Trakt history keyed by IMDb or TMDb GUID for movies and episodes."""
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
                    movies[guid] = (m["title"], normalize_year(m.get("year")))
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
    payload = {"movies": [], "episodes": []}

    for title, year, watched_at, guid in movies:
        movie_obj = {"title": title}
        if year is not None:
            movie_obj["year"] = year
        if guid:
            movie_obj["ids"] = guid_to_ids(guid)
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
                ep_obj["title"] = show        # último recurso
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


def update_plex(
    plex,
    movies: Set[Tuple[str, Optional[int]]],
    episodes: Set[Tuple[str, str]],
) -> None:
    """Mark items as watched in Plex when they appear in Trakt but not in Plex."""
    movie_count = 0
    episode_count = 0

    # Movies
    for title, year in movies:
        try:
            if year:
                results = plex.library.search(title=title, year=year, libtype="movie")
            else:
                results = plex.library.search(title=title, libtype="movie")
            for item in results:
                item.markWatched()
                movie_count += 1
        except BadRequest as exc:
            logger.warning("Plex search error for %s (%s): %s", title, year, exc)

    # Episodes
    for show, code in episodes:
        season = int(code[1:3])
        number = int(code[4:6])
        try:
            # searchShows removed → use generic show search
            series = plex.library.search(title=show, libtype="show")
            for show_obj in series:
                try:
                    ep = show_obj.episode(season=season, episode=number)
                except NotFound:
                    continue
                if ep:
                    ep.markWatched()
                    episode_count += 1
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


def sync_liked_lists(plex, headers, plex_guids):
    """Update liked Trakt lists with movies available in Plex."""
    try:
        user_data = trakt_request("GET", "/users/settings", headers).json()
        username = user_data.get("user", {}).get("ids", {}).get("slug") or user_data.get("user", {}).get("username")
        likes = trakt_request("GET", "/users/likes/lists", headers).json()
    except Exception as exc:
        logger.error("Failed to fetch liked lists: %s", exc)
        return
    for like in likes:
        lst = like.get("list", {})
        owner = lst.get("user", {}).get("ids", {}).get("slug") or lst.get("user", {}).get("username")
        slug = lst.get("ids", {}).get("slug")
        if not owner or not slug:
            continue
        try:
            items = trakt_request("GET", f"/users/{owner}/lists/{slug}/items/movies", headers).json()
        except Exception as exc:
            logger.error("Failed to fetch list %s/%s: %s", owner, slug, exc)
            continue
        movies = []
        for it in items:
            ids = it.get("movie", {}).get("ids", {})
            if ids.get("imdb") and f"imdb://{ids['imdb']}" in plex_guids:
                movies.append({"ids": {"imdb": ids["imdb"]}})
            elif ids.get("tmdb") and f"tmdb://{ids['tmdb']}" in plex_guids:
                movies.append({"ids": {"tmdb": ids["tmdb"]}})
            elif ids.get("tvdb") and f"tvdb://{ids['tvdb']}" in plex_guids:
                movies.append({"ids": {"tvdb": ids["tvdb"]}})
        if movies:
            try:
                trakt_request(
                    "POST",
                    f"/users/{username}/lists/{slug}/items",
                    headers,
                    json={"movies": movies},
                )
                logger.info("Updated liked list %s with %d movies", slug, len(movies))
            except Exception as exc:
                logger.error("Failed updating list %s: %s", slug, exc)


def sync_watchlist(headers, plex_guids):
    """Remove movies from Trakt watchlist that already exist in Plex."""
    try:
        items = trakt_request("GET", "/sync/watchlist/movies", headers).json()
    except Exception as exc:
        logger.error("Failed to fetch Trakt watchlist: %s", exc)
        return
    remove = []
    for it in items:
        ids = it.get("movie", {}).get("ids", {})
        if ids.get("imdb") and f"imdb://{ids['imdb']}" in plex_guids:
            remove.append({"ids": {"imdb": ids["imdb"]}})
        elif ids.get("tmdb") and f"tmdb://{ids['tmdb']}" in plex_guids:
            remove.append({"ids": {"tmdb": ids["tmdb"]}})
        elif ids.get("tvdb") and f"tvdb://{ids['tvdb']}" in plex_guids:
            remove.append({"ids": {"tvdb": ids["tvdb"]}})
    if remove:
        trakt_request("POST", "/sync/watchlist/remove", headers, json={"movies": remove})
        logger.info("Removed %d movies from Trakt watchlist", len(remove))



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

    if not all([plex_baseurl, plex_token, trakt_token, trakt_client_id]):
        logger.error("Missing environment variables for Plex or Trakt.")
        return

    plex = PlexServer(plex_baseurl, plex_token)

    headers = {
        "Authorization": f"Bearer {trakt_token}",
        "Content-Type": "application/json",
        "User-Agent": "PlexyTrackt/1.0.0",
        "trakt-api-version": "2",
        "trakt-api-key": trakt_client_id,
    }

    if SYNC_COLLECTION:
        sync_collection(plex, headers)
    if SYNC_RATINGS:
        sync_ratings(plex, headers)

    plex_movies, plex_episodes = get_plex_history(plex)
    plex_movie_guids = set(plex_movies.keys())
    plex_episode_guids = set(plex_episodes.keys())

    trakt_movies, trakt_episodes = get_trakt_history(headers)
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

    new_movies = [
        (data["title"], data["year"], data["watched_at"], guid)
        for guid, data in plex_movies.items()
        if guid not in trakt_movie_guids
    ]
    new_episodes = [
        (data["show"], data["code"], data["watched_at"], guid)
        for guid, data in plex_episodes.items()
        if guid not in trakt_episode_guids
    ]

    # Permite desactivar la sync de vistos desde la interfaz
    if SYNC_WATCHED:
        update_trakt(headers, new_movies, new_episodes)
    missing_movies = {trakt_movies[g] for g in (trakt_movie_guids - plex_movie_guids)}
    missing_episodes = {trakt_episodes[g] for g in (trakt_episode_guids - plex_episode_guids)}
    update_plex(plex, missing_movies, missing_episodes)

    if SYNC_LIKED_LISTS:
        sync_liked_lists(plex, headers, plex_movie_guids)
    if SYNC_WATCHLISTS:
        sync_watchlist(headers, plex_movie_guids)

    logger.info("Synchronization job finished")


# --------------------------------------------------------------------------- #
# FLASK ROUTES
# --------------------------------------------------------------------------- #
@app.route("/", methods=["GET", "POST"])
def index():
    global SYNC_INTERVAL_MINUTES, SYNC_COLLECTION, SYNC_RATINGS, SYNC_WATCHED, SYNC_LIKED_LISTS, SYNC_WATCHLISTS

    load_trakt_tokens()

    # 1) Initial authorization
    if not os.environ.get("TRAKT_ACCESS_TOKEN") or not os.environ.get("TRAKT_REFRESH_TOKEN"):
        if request.method == "POST":
            code = request.form.get("code", "").strip()
            if code and exchange_code_for_tokens(code):
                start_scheduler()
                return redirect(url_for("index"))
        auth_url = (
            "https://trakt.tv/oauth/authorize"
            f"?response_type=code&client_id={os.environ.get('TRAKT_CLIENT_ID')}"
            f"&redirect_uri={TRAKT_REDIRECT_URI}"
        )
        return render_template("authorize.html", auth_url=auth_url)

    # 2) Change interval
    if request.method == "POST":
        minutes = int(request.form.get("minutes", 60))
        SYNC_INTERVAL_MINUTES = minutes
        SYNC_COLLECTION = request.form.get("collection") is not None
        SYNC_RATINGS = request.form.get("ratings") is not None
        SYNC_WATCHED = request.form.get("watched") is not None
        SYNC_LIKED_LISTS = request.form.get("liked_lists") is not None
        SYNC_WATCHLISTS = request.form.get("watchlists") is not None
        scheduler.remove_all_jobs()
        scheduler.add_job(sync, "interval", minutes=minutes, id="sync_job")
        return redirect(url_for("index"))

    return render_template(
        "index.html",
        minutes=SYNC_INTERVAL_MINUTES,
        collection=SYNC_COLLECTION,
        ratings=SYNC_RATINGS,
        watched=SYNC_WATCHED,
        liked_lists=SYNC_LIKED_LISTS,
        watchlists=SYNC_WATCHLISTS,
    )


@app.route("/stop", methods=["POST"])
def stop():
    stop_scheduler()
    return redirect(url_for("index"))


# --------------------------------------------------------------------------- #
# SCHEDULER STARTUP
# --------------------------------------------------------------------------- #
def test_connections() -> bool:
    plex_baseurl = os.environ.get("PLEX_BASEURL")
    plex_token = os.environ.get("PLEX_TOKEN")
    trakt_token = os.environ.get("TRAKT_ACCESS_TOKEN")
    trakt_client_id = os.environ.get("TRAKT_CLIENT_ID")

    if not all([plex_baseurl, plex_token, trakt_token, trakt_client_id]):
        logger.error("Missing environment variables for Plex or Trakt.")
        return False

    try:
        PlexServer(plex_baseurl, plex_token).account()
        logger.info("Successfully connected to Plex.")
    except Exception as exc:
        logger.error("Failed to connect to Plex: %s", exc)
        return False

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

    return True


def start_scheduler():
    if scheduler.running:
        if not scheduler.get_job("sync_job"):
            scheduler.add_job(sync, "interval", minutes=SYNC_INTERVAL_MINUTES, id="sync_job")
            logger.info("Sync job added with interval %d minutes", SYNC_INTERVAL_MINUTES)
        return
    if not test_connections():
        logger.error("Connection test failed. Scheduler will not start.")
        return
    scheduler.add_job(sync, "interval", minutes=SYNC_INTERVAL_MINUTES, id="sync_job")
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
    start_scheduler()
    app.run(host="0.0.0.0", port=5000)
