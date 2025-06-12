import json
import logging
import os
import time
from typing import Dict, List, Optional, Tuple

import requests

from .utils import guid_to_ids, normalize_year, to_iso_z, valid_guid, best_guid, imdb_guid, get_show_from_library, ensure_collection

logger = logging.getLogger(__name__)

TOKEN_FILE = "trakt_tokens.json"


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


def exchange_code_for_tokens(code: str, redirect_uri: str) -> Optional[dict]:
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


def get_trakt_history(headers: dict) -> Tuple[
    Dict[str, Tuple[str, Optional[int], Optional[str]]],
    Dict[str, Tuple[str, str, Optional[str]]],
]:
    movies: Dict[str, Tuple[str, Optional[int], Optional[str]]] = {}
    episodes: Dict[str, Tuple[str, str, Optional[str]]] = {}

    page = 1
    logger.info("Fetching Trakt history…")
    while True:
        resp = trakt_request("GET", "/sync/history", headers, params={"page": page, "limit": 100})
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


def update_trakt(headers: dict, movies: List[Tuple[str, Optional[int], Optional[str], Optional[str]]], episodes: List[Tuple[str, str, Optional[str], Optional[str]]]) -> None:
    payload = {"movies": [], "episodes": []}

    for title, year, watched_at, guid in movies:
        if not valid_guid(guid):
            continue
        movie_obj = {"title": title}
        if year is not None:
            movie_obj["year"] = year
        movie_ids = guid_to_ids(guid)
        if movie_ids:
            movie_obj["ids"] = movie_ids
        if watched_at:
            movie_obj["watched_at"] = watched_at
        payload["movies"].append(movie_obj)

    for show, code, watched_at, guid in episodes:
        if not valid_guid(guid):
            continue
        season = int(code[1:3])
        number = int(code[4:6])
        ep_obj = {"season": season, "number": number}
        ep_ids = guid_to_ids(guid)
        if ep_ids:
            ep_obj["ids"] = ep_ids
        if watched_at:
            ep_obj["watched_at"] = watched_at
        payload["episodes"].append(ep_obj)

    if not payload["movies"] and not payload["episodes"]:
        logger.info("Nothing new to send to Trakt.")
        return

    trakt_request("POST", "/sync/history", headers, json=payload)
    logger.info("Sent %d movies and %d episodes to Trakt", len(payload["movies"]), len(payload["episodes"]))


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
