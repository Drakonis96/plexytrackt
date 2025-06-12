import json
import logging
import os
from typing import Dict, List, Optional, Tuple, Union

import requests

from utils import guid_to_ids, normalize_year, simkl_episode_key, to_iso_z

logger = logging.getLogger(__name__)

SIMKL_TOKEN_FILE = "simkl_tokens.json"


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


def exchange_code_for_simkl_tokens(code: str, redirect_uri: str) -> Optional[dict]:
    client_id = os.environ.get("SIMKL_CLIENT_ID")
    client_secret = os.environ.get("SIMKL_CLIENT_SECRET")
    if not all([code, client_id, client_secret]):
        logger.error("Missing code or Simkl client credentials.")
        return None

    payload = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    try:
        resp = requests.post("https://api.simkl.com/oauth/token", json=payload, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        logger.error("Failed to obtain Simkl token: %s", exc)
        return None

    data = resp.json()
    os.environ["SIMKL_ACCESS_TOKEN"] = data["access_token"]
    save_simkl_token(data["access_token"])
    logger.info("Simkl token obtained via authorization code")
    return data


def simkl_request(method: str, endpoint: str, headers: dict, *, retries: int = 2, timeout: int = 30, **kwargs) -> requests.Response:
    url = f"https://api.simkl.com{endpoint}"
    if "timeout" in kwargs:
        timeout = kwargs.pop("timeout")
    attempt = 0
    while True:
        try:
            resp = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.exceptions.ReadTimeout as exc:
            if attempt >= retries:
                logger.error("Simkl ReadTimeout after %d attempts (%d s).", attempt + 1, timeout)
                raise
            attempt += 1
            timeout *= 2
            logger.warning(
                "Simkl request %s %s timed out (%s). Retrying (%d/%d) with timeout=%ds…",
                method.upper(), endpoint, exc, attempt, retries, timeout,
            )
        except requests.exceptions.RequestException:
            raise


def simkl_search_ids(headers: dict, title: str, *, is_movie: bool = True, year: Optional[int] = None) -> Dict[str, Union[str, int]]:
    endpoint = "/search/movies" if is_movie else "/search/shows"
    params = {"q": title, "limit": 1}
    if year and is_movie:
        params["year"] = year
    try:
        resp = simkl_request("GET", endpoint, headers, params=params)
        data = resp.json()
    except Exception as exc:
        logger.debug("Simkl search failed for '%s': %s", title, exc)
        return {}
    if not isinstance(data, list) or not data:
        return {}
    ids = data[0].get("ids", {}) or {}
    for k, v in list(ids.items()):
        try:
            ids[k] = int(v) if str(v).isdigit() else v
        except Exception:
            pass
    return ids


def simkl_movie_key(m: dict) -> Optional[str]:
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


def get_simkl_history(headers: dict) -> Tuple[Dict[str, Tuple[str, Optional[int], Optional[str]]], Dict[str, Tuple[str, str, Optional[str]]]]:
    movies: Dict[str, Tuple[str, Optional[int], Optional[str]]] = {}
    episodes: Dict[str, Tuple[str, str, Optional[str]]] = {}

    page = 1
    logger.info("Fetching Simkl watch history…")
    while True:
        resp = simkl_request("GET", "/sync/history", headers, params={"page": page, "limit": 100, "type": "movies"})
        data = resp.json()
        if not isinstance(data, list) or not data:
            break
        for item in data:
            m = item.get("movie", {})
            guid = simkl_movie_key(m)
            if not guid:
                continue
            if guid not in movies:
                movies[guid] = (m.get("title"), normalize_year(m.get("year")), item.get("watched_at"))
        page += 1

    page = 1
    logger.info("Fetching Simkl episode history…")
    while True:
        resp = simkl_request("GET", "/sync/history", headers, params={"page": page, "limit": 100, "type": "episodes"})
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
                episodes[guid] = (show.get("title"), f"S{e.get('season', 0):02d}E{e.get('number', 0):02d}", item.get("watched_at"))
        page += 1

    return movies, episodes


def update_simkl(headers: dict, movies: List[Tuple[str, Optional[int], Optional[str], Optional[str]]], episodes: List[Tuple[str, str, Optional[str], Optional[str]]]) -> None:
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
            ids = guid_to_ids(guid) if guid else {}
            if not ids:
                ids = simkl_search_ids(headers, show_title, is_movie=False)
                if ids:
                    logger.debug("IDs found in Simkl for show '%s': %s", show_title, ids)
            if not ids:
                logger.warning("Skipping episode '%s - %s' - no IDs found", show_title, code)
                continue
            key = tuple(sorted(ids.items()))
            if key not in shows:
                shows[key] = {"title": show_title, "ids": ids, "seasons": []}
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
                shows[key]["seasons"].append({"number": season_num, "episodes": [{"number": episode_num, "watched_at": watched_at}]})
        if shows:
            payload["shows"] = list(shows.values())

    if not payload:
        logger.info("Nothing new to sync with Simkl")
        return

    logger.info("Adding %d movies and %d shows to Simkl history", len(payload.get("movies", [])), len(payload.get("shows", [])))
    try:
        simkl_request("post", "/sync/history", headers, json=payload)
        logger.info("Simkl history updated successfully.")
    except requests.exceptions.RequestException as e:
        logger.error("Failed to update Simkl: %s", e)
