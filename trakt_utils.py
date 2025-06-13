import json
import logging
import os
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Union

import requests

from utils import guid_to_ids, normalize_year, to_iso_z, valid_guid, best_guid, imdb_guid, get_show_from_library, ensure_collection, find_item_by_guid

logger = logging.getLogger(__name__)

TOKEN_FILE = "trakt_tokens.json"
TRAKT_LAST_SYNC_FILE = "trakt_last_sync.txt"


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
            json.dump({"access_token": access_token, "refresh_token": refresh_token}, f, indent=2)
        logger.info("Saved Trakt tokens to %s", TOKEN_FILE)
    except Exception as exc:
        logger.error("Failed to save Trakt tokens: %s", exc)


def load_trakt_last_sync_date() -> Optional[str]:
    """Return the stored last sync date for Trakt."""
    if os.path.exists(TRAKT_LAST_SYNC_FILE):
        try:
            with open(TRAKT_LAST_SYNC_FILE, "r", encoding="utf-8") as f:
                return f.read().strip() or None
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load Trakt last sync date: %s", exc)
    return None


def save_trakt_last_sync_date(date_str: str) -> None:
    """Persist ``date_str`` to :data:`TRAKT_LAST_SYNC_FILE`."""
    try:
        with open(TRAKT_LAST_SYNC_FILE, "w", encoding="utf-8") as f:
            f.write(date_str)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to save Trakt last sync date: %s", exc)


def get_trakt_last_activity(headers: dict) -> Optional[str]:
    """Return the latest ``all`` activity timestamp from Trakt."""
    try:
        resp = trakt_request("GET", "/sync/last_activities", headers)
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to retrieve Trakt last activities: %s", exc)
        return None
    if isinstance(data, dict):
        return data.get("all")
    return None


def get_trakt_redirect_uri() -> str:
    global TRAKT_REDIRECT_URI
    if 'TRAKT_REDIRECT_URI' in globals() and TRAKT_REDIRECT_URI:
        return TRAKT_REDIRECT_URI
    TRAKT_REDIRECT_URI = os.environ.get("TRAKT_REDIRECT_URI")
    if TRAKT_REDIRECT_URI:
        return TRAKT_REDIRECT_URI
    try:
        try:
            from flask import has_request_context, request
            if has_request_context():
                TRAKT_REDIRECT_URI = request.url_root.rstrip("/") + "/oauth/trakt"
                return TRAKT_REDIRECT_URI
        except (ImportError, AttributeError):
            pass
    except Exception:
        pass
    return "http://localhost:5030/oauth/trakt"


def exchange_code_for_tokens(code: str, redirect_uri: Optional[str] = None) -> Optional[dict]:
    if redirect_uri is None:
        redirect_uri = get_trakt_redirect_uri()
    client_id = os.environ.get("TRAKT_CLIENT_ID")
    client_secret = os.environ.get("TRAKT_CLIENT_SECRET")
    if not all([code, client_id, client_secret]):
        logger.error("Missing code or Trakt client credentials.")
        return None

    payload = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
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


def refresh_trakt_token(redirect_uri: str) -> Optional[str]:
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
        "redirect_uri": redirect_uri,
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
    if resp.status_code == 401:
        new_token = refresh_trakt_token(headers.get("redirect_uri", ""))
        if new_token:
            headers["Authorization"] = f"Bearer {new_token}"
            resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)

    if resp.status_code == 420:
        msg = (
            "Trakt API returned 420 – account limit exceeded. "
            "Upgrade to VIP or reduce the size of your collection/watchlist."
        )
        logger.warning(msg)
        raise Exception(msg)

    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", "1"))
        logger.warning("Trakt API rate limit reached. Retrying in %s seconds", retry_after)
        time.sleep(retry_after)
        resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)

    resp.raise_for_status()
    return resp


def get_trakt_history(
    headers: dict,
    *,
    date_from: Optional[str] = None,
) -> Tuple[
    Dict[str, Tuple[str, Optional[int], Optional[str]]],
    Dict[str, Tuple[str, str, Optional[str]]],
]:
    movies: Dict[str, Tuple[str, Optional[int], Optional[str]]] = {}
    episodes: Dict[str, Tuple[str, str, Optional[str]]] = {}

    page = 1
    logger.info("Fetching Trakt history…")
    params = {"page": page, "limit": 100}
    if date_from:
        params["start_at"] = date_from
    while True:
        params["page"] = page
        resp = trakt_request("GET", "/sync/history", headers, params=params)
        data = resp.json()
        if not isinstance(data, list) or not data:
            break
        for item in data:
            watched_at = item.get("watched_at")
            if item["type"] == "movie":
                m = item["movie"]
                ids = m.get("ids", {})
                guid = None
                if ids.get("imdb"):
                    guid = f"imdb://{ids['imdb']}"
                elif ids.get("tmdb"):
                    guid = f"tmdb://{ids['tmdb']}"
                if guid and guid not in movies:
                    movies[guid] = (m["title"], normalize_year(m.get("year")), watched_at)
            elif item["type"] == "episode":
                e = item["episode"]
                show = item["show"]
                ids = e.get("ids", {})
                guid = None
                if ids.get("imdb"):
                    guid = f"imdb://{ids['imdb']}"
                elif ids.get("tmdb"):
                    guid = f"tmdb://{ids['tmdb']}"
                if guid and guid not in episodes:
                    episodes[guid] = (show["title"], f"S{e['season']:02d}E{e['number']:02d}", watched_at)
        page += 1

    return movies, episodes


def update_trakt(headers: dict, movies: list, episodes: list) -> None:
    """Add new items to Trakt history with fallback ID search when GUIDs are not available."""
    payload = {"movies": [], "episodes": []}

    # Movies: Try GUID first, then fallback to search if needed
    for title, year, watched_at, guid in movies:
        ids = None
        
        # Try to get IDs from GUID first
        if guid and isinstance(guid, str) and valid_guid(guid):
            ids = guid_to_ids(guid)
            
        # Fallback: search Trakt by title if no valid GUID
        if not ids:
            ids = trakt_search_ids(headers, title, is_movie=True, year=year)
            if ids:
                logger.debug("IDs found in Trakt search for movie '%s': %s", title, ids)
        
        if ids:
            item = {"title": title, "ids": ids}
            if year is not None:
                item["year"] = year
            if watched_at:
                item["watched_at"] = watched_at
            payload["movies"].append(item)
            logger.debug("Added movie '%s' to Trakt payload", title)
        else:
            logger.warning("Skipping movie '%s' (%s) - no IDs found", title, year)

    # Episodes: Use flexible approach - individual episode IDs if available, otherwise group by show
    episodes_added_individually = set()
    shows = {}
    
    for show_title, code, watched_at, guid in episodes:
        # Extract season and episode numbers from code (format: S01E01)
        try:
            season = int(code[1:3])
            number = int(code[4:6])
        except (ValueError, IndexError):
            logger.warning("Invalid episode code format: %s", code)
            continue
        
        # Try individual episode approach first (if we have a valid episode GUID)
        if guid and isinstance(guid, str) and valid_guid(guid):
            episode_ids = guid_to_ids(guid)
            if episode_ids:
                episode_obj = {
                    "season": season,
                    "number": number,
                    "ids": episode_ids
                }
                if watched_at:
                    episode_obj["watched_at"] = watched_at
                payload["episodes"].append(episode_obj)
                episodes_added_individually.add((show_title, code))
                logger.debug("Added individual episode '%s - %s' (S%02dE%02d) with episode GUID '%s' to Trakt payload", show_title, code, season, number, guid)
                continue
        
        # Fallback: Group episodes by show (using show GUID or search)
        show_ids = None
        
        # Try to get show IDs from episode GUID first (might be a show GUID)
        if guid and isinstance(guid, str) and valid_guid(guid):
            show_ids = guid_to_ids(guid)
        
        # If no IDs from GUID, try searching Trakt by show title
        if not show_ids:
            show_ids = trakt_search_ids(headers, show_title, is_movie=False)
            if show_ids:
                logger.debug("IDs found in Trakt search for show '%s': %s", show_title, show_ids)
        
        if show_ids:
            # Group episodes by show
            key = tuple(sorted(show_ids.items()))
            if key not in shows:
                shows[key] = {
                    "title": show_title,
                    "ids": show_ids,
                    "seasons": []
                }
            
            # Add episode to the appropriate season
            season_found = False
            for s in shows[key]["seasons"]:
                if s["number"] == season:
                    s["episodes"].append({"number": number, "watched_at": watched_at})
                    season_found = True
                    break
            
            if not season_found:
                shows[key]["seasons"].append({
                    "number": season,
                    "episodes": [{"number": number, "watched_at": watched_at}]
                })
            
            logger.debug("Added episode '%s - %s' (S%02dE%02d) to show group in Trakt payload", show_title, code, season, number)
        else:
            logger.warning("Skipping episode '%s - %s' - no IDs found (no episode GUID or show lookup failed)", show_title, code)
    
    # Add grouped show episodes to payload
    if shows:
        if "shows" not in payload:
            payload["shows"] = []
        payload["shows"].extend(list(shows.values()))

    if not payload["movies"] and not payload["episodes"] and not payload.get("shows"):
        logger.info("Nothing new to send to Trakt.")
        return

    logger.info(
        "Adding %d movies, %d individual episodes, and %d shows (with episodes) to Trakt history",
        len(payload["movies"]),
        len(payload["episodes"]),
        len(payload.get("shows", [])),
    )
    try:
        trakt_request("POST", "/sync/history", headers, json=payload)
        logger.info("Trakt history updated successfully.")
    except requests.exceptions.RequestException as e:
        logger.error("Failed to update Trakt history: %s", e)


def sync_collection(plex, headers):
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
                        seasons_list.append({"number": int(season.index), "rating": int(round(float(season_rating))), "rated_at": rated_now})
                        has_show_data = True
                    for ep in season.episodes():
                        ep_rating = getattr(ep, "userRating", None)
                        if ep_rating is not None:
                            ep_obj = {"season": int(season.index), "number": int(ep.index), "rating": int(round(float(ep_rating))), "rated_at": rated_now}
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
    try:
        likes = trakt_request("GET", "/users/likes/lists", headers).json()
    except Exception as exc:
        logger.error("Failed to fetch liked lists: %s", exc)
        return
    for like in likes:
        lst = like.get("list", {})
        owner = lst.get("user", {}).get("ids", {}).get("slug") or lst.get("user", {}).get("username")
        slug = lst.get("ids", {}).get("slug")
        name = lst.get("name", slug)
        if not owner or not slug:
            continue
        try:
            items = trakt_request("GET", f"/users/{owner}/lists/{slug}/items", headers).json()
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
            elif ids.get("tvdb"):
                guid = f"tvdb://{ids['tvdb']}"
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
    try:
        user_data = trakt_request("GET", "/users/settings", headers).json()
        username = user_data.get("user", {}).get("ids", {}).get("slug") or user_data.get("user", {}).get("username")
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
                    resp = trakt_request("POST", f"/users/{username}/lists", headers, json={"name": coll.title})
                    slug = resp.json().get("ids", {}).get("slug")
                    slug_by_name[coll.title] = slug
                except Exception as exc:
                    logger.error("Failed creating list %s: %s", coll.title, exc)
                    continue
            try:
                items = trakt_request("GET", f"/users/{username}/lists/{slug}/items", headers).json()
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
                elif ids.get("tvdb"):
                    trakt_guids.add(f"tvdb://{ids['tvdb']}")
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
                    trakt_request("POST", f"/users/{username}/lists/{slug}/items", headers, json=payload)
                    logger.info("Updated Trakt list %s with %d items", slug, len(movies) + len(shows))
                except Exception as exc:
                    logger.error("Failed updating list %s: %s", slug, exc)


def sync_watchlist(plex, headers, plex_history, trakt_history):
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
            elif ids.get("tvdb"):
                trakt_guids.add(f"tvdb://{ids['tvdb']}")

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
        logger.info("Added %d items to Trakt watchlist", len(movies_to_add) + len(shows_to_add))

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
            elif ids.get("tvdb"):
                guid = f"tvdb://{ids['tvdb']}"
            if guid and (guid in plex_history or guid in trakt_history) and guid not in plex_guids:
                remove.append({"ids": guid_to_ids(guid)})
    if remove:
        trakt_request("POST", "/sync/watchlist/remove", headers, json={"movies": remove, "shows": remove})
        logger.info("Removed %d items from Trakt watchlist", len(remove))


def fetch_trakt_history_full(headers) -> list:
    all_items = []
    page = 1
    while True:
        resp = trakt_request("GET", "/sync/history", headers, params={"page": page, "limit": 100})
        data = resp.json()
        if not data:
            break
        all_items.extend(data)
        page += 1
    return all_items


def fetch_trakt_ratings(headers) -> list:
    all_items = []
    page = 1
    while True:
        resp = trakt_request("GET", "/sync/ratings", headers, params={"page": page, "limit": 100})
        data = resp.json()
        if not data:
            break
        all_items.extend(data)
        page += 1
    return all_items


def fetch_trakt_watchlist(headers) -> dict:
    movies = trakt_request("GET", "/sync/watchlist/movies", headers).json()
    shows = trakt_request("GET", "/sync/watchlist/shows", headers).json()
    return {"movies": movies, "shows": shows}


def restore_backup(headers, data: dict) -> None:
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
                obj = {"ids": ids, "season": ep.get("season"), "number": ep.get("number")}
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
