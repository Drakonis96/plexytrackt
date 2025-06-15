import logging
import os
import xml.etree.ElementTree as ET
from typing import Dict, Optional, Set, Tuple, List

import requests

from plexapi.server import PlexServer

from utils import (
    _parse_guid_value,
    get_show_from_library,
    imdb_guid,
    normalize_year,
    to_iso_z,
    valid_guid,
    find_item_by_guid,
)

logger = logging.getLogger(__name__)


def _get_managed_user_token(admin_token: str, identifier: str) -> Optional[str]:
    """Return an access token for a managed Plex user via plex.tv."""
    try:
        users_url = f"https://plex.tv/api/users?X-Plex-Token={admin_token}"
        resp = requests.get(users_url, timeout=10)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        user_id = None
        machine_id = None
        for user in root.findall(".//User"):
            if identifier in (
                user.attrib.get("id"),
                user.attrib.get("title"),
                user.attrib.get("username"),
            ):
                user_id = user.attrib.get("id")
                server = user.find("Server")
                if server is not None:
                    machine_id = server.attrib.get("machineIdentifier")
                break
        if not (user_id and machine_id):
            return None

        shared_url = (
            f"https://plex.tv/api/servers/{machine_id}/shared_servers?X-Plex-Token={admin_token}"
        )
        resp = requests.get(shared_url, timeout=10)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        for srv in root.findall(".//SharedServer"):
            if srv.attrib.get("userID") == str(user_id):
                return srv.attrib.get("accessToken")
    except Exception as exc:  # noqa: BLE001
        logger.debug("Managed user token lookup failed: %s", exc)
    return None


def get_user_token(plex, identifier: str) -> Optional[str]:
    """Return an access token for the given Plex user identifier."""
    try:
        account = plex.myPlexAccount()
        if identifier in (str(account.id), getattr(account, "username", None)):
            return plex._token
        for user in account.users():
            if identifier in (str(user.id), user.title, getattr(user, "username", None)):
                token = None
                try:
                    token = user.get_token(plex.machineIdentifier)
                except Exception:
                    logger.debug("PlexAPI token lookup failed for %s", identifier)
                if not token:
                    token = _get_managed_user_token(plex._token, str(user.id))
                return token
        # Try direct lookup when user wasn't found above
        return _get_managed_user_token(plex._token, identifier)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to get token for Plex user %s: %s", identifier, exc)
    return None


def list_users_with_tokens(plex) -> List[Tuple[str, str, bool, Optional[str]]]:
    """Return Plex users with their tokens.

    Each tuple contains ``id``, ``name``, ``is_main`` and ``token``. ``is_main``
    is ``True`` for the primary account and ``False`` for managed users.
    """
    users_info: List[Tuple[str, str, bool, Optional[str]]] = []
    try:
        account = plex.myPlexAccount()
        main_id = str(account.id)
        main_name = account.username or "unknown"
        main_token = get_user_token(plex, main_id) or plex._token
        users_info.append((main_id, main_name, True, main_token))
        for user in account.users():
            uid = str(user.id)
            name = user.title
            token = get_user_token(plex, uid)
            users_info.append((uid, name, False, token))
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to list Plex users with tokens: %s", exc)
    return users_info


def get_plex_history(plex, accounts: Optional[List[str]] = None) -> Tuple[
    Dict[str, Dict[str, Optional[str]]],
    Dict[str, Dict[str, Optional[str]]],
]:
    """Return watched movies and episodes from Plex keyed by GUID."""

    def _extract(server) -> Tuple[
        Dict[str, Dict[str, Optional[str]]],
        Dict[str, Dict[str, Optional[str]]],
    ]:
        movies: Dict[str, Dict[str, Optional[str]]] = {}
        episodes: Dict[str, Dict[str, Optional[str]]] = {}
        show_guid_cache: Dict[str, Optional[str]] = {}

        for entry in server.history():
            watched_at = to_iso_z(getattr(entry, "viewedAt", None))

            if entry.type == "movie":
                try:
                    item = entry.source() or server.fetchItem(entry.ratingKey)
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
            elif entry.type == "episode":
                season = getattr(entry, "parentIndex", None)
                number = getattr(entry, "index", None)
                show = getattr(entry, "grandparentTitle", None)
                try:
                    item = entry.source() or server.fetchItem(entry.ratingKey)
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

                series_guid: Optional[str] = None
                if item is not None:
                    gp_guid_raw = getattr(item, "grandparentGuid", None)
                    if gp_guid_raw:
                        series_guid = _parse_guid_value(gp_guid_raw)
                if series_guid is None and show in show_guid_cache:
                    series_guid = show_guid_cache[show]
                if series_guid is None and show:
                    series_obj = get_show_from_library(server, show)
                    series_guid = imdb_guid(series_obj) if series_obj else None
                    show_guid_cache[show] = series_guid

                if guid and valid_guid(guid) and guid not in episodes:
                    episodes[guid] = {
                        "show": show,
                        "code": code,
                        "watched_at": watched_at,
                        "guid": guid,
                    }

        logger.info("Fetching watched flags from Plex library…")
        for section in server.library.sections():
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
                        if guid and guid not in episodes:
                            episodes[guid] = {
                                "show": show_title,
                                "code": code,
                                "watched_at": to_iso_z(getattr(ep, "lastViewedAt", None)),
                                "guid": guid,
                            }
            except Exception as exc:
                logger.debug(
                    "Failed fetching watched items from section %s: %s",
                    section.title,
                    exc,
                )

        return movies, episodes

    logger.info("Fetching Plex history…")
    plex_baseurl = os.environ.get("PLEX_BASEURL")
    if accounts:
        all_movies: Dict[str, Dict[str, Optional[str]]] = {}
        all_episodes: Dict[str, Dict[str, Optional[str]]] = {}
        for acc in accounts:
            token = get_user_token(plex, acc)
            if not token:
                logger.warning("Token for Plex user %s not found; skipping", acc)
                continue
            try:
                user_server = PlexServer(plex_baseurl, token)
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to connect to Plex as %s: %s", acc, exc)
                continue
            movies, episodes = _extract(user_server)
            for k, v in movies.items():
                all_movies.setdefault(k, v)
            for k, v in episodes.items():
                all_episodes.setdefault(k, v)
        return all_movies, all_episodes

    return _extract(plex)


def update_plex(
    plex,
    movies: Set[Tuple[str, Optional[int], Optional[str]]],
    episodes: Set[Tuple[str, str, Optional[str]]],  # Only allow str for key, not Tuple fallback
    token: Optional[str] = None,
) -> None:
    """Mark items as watched in Plex when missing."""
    movie_count = 0
    episode_count = 0

    for title, year, guid in movies:
        if guid and valid_guid(guid):
            try:
                item = find_item_by_guid(plex, guid)
                if item and getattr(item, "isWatched", lambda: bool(getattr(item, "viewCount", 0)))():
                    continue
                if item:
                    params = {"key": item.ratingKey, "identifier": "com.plexapp.plugins.library"}
                    if token:
                        plex.query("/:/scrobble", params=params, headers={"X-Plex-Token": token})
                    else:
                        plex.query("/:/scrobble", params=params)
                    movie_count += 1
                    continue
            except Exception as exc:
                logger.debug("GUID search failed for %s: %s", guid, exc)

        found = None
        for section in plex.library.sections():
            if section.type != "movie":
                continue
            try:
                results = section.search(title=title)
                for candidate in results:
                    if year is None or normalize_year(getattr(candidate, "year", None)) == normalize_year(year):
                        found = candidate
                        break
                if found:
                    break
            except Exception as exc:
                logger.debug("Search failed in section %s: %s", section.title, exc)

        if not found:
            logger.debug("Movie not found in Plex library: %s (%s)", title, year)
            continue

        try:
            # Check if already watched using isWatched property or viewCount
            is_watched = getattr(found, "isWatched", False) or bool(getattr(found, "viewCount", 0))
            if is_watched:
                continue
            params = {"key": found.ratingKey, "identifier": "com.plexapp.plugins.library"}
            if token:
                plex.query("/:/scrobble", params=params, headers={"X-Plex-Token": token})
            else:
                plex.query("/:/scrobble", params=params)
            movie_count += 1
        except Exception as exc:
            logger.debug("Failed to mark movie '%s' as watched: %s", found.title, exc)

    for show_title, code, key in episodes:
        guid: Optional[str] = None
        if isinstance(key, str):
            guid = key if valid_guid(key) else None
        # Remove tuple fallback for Trakt, only allow for Simkl (not present here)

        if guid:
            try:
                item = find_item_by_guid(plex, guid)
                if item:
                    # Check if already watched using isWatched property or viewCount
                    is_watched = getattr(item, "isWatched", False) or bool(getattr(item, "viewCount", 0))
                    if is_watched:
                        continue
                    params = {"key": item.ratingKey, "identifier": "com.plexapp.plugins.library"}
                    if token:
                        plex.query("/:/scrobble", params=params, headers={"X-Plex-Token": token})
                    else:
                        plex.query("/:/scrobble", params=params)
                    episode_count += 1
                    continue
            except Exception as exc:
                logger.debug("GUID search failed for %s: %s", guid, exc)

        try:
            season_num, episode_num = map(int, code.upper().lstrip("S").split("E"))
        except ValueError:
            logger.debug("Invalid episode code format: %s", code)
            continue

        show_obj = get_show_from_library(plex, show_title)
        if not show_obj:
            logger.debug("Show not found in Plex library: %s", show_title)
            continue

        try:
            # Try to find the episode using the show's episode method
            ep_obj = show_obj.episode(season=season_num, episode=episode_num)
            # Check if already watched using isWatched property or viewCount
            is_watched = getattr(ep_obj, "isWatched", False) or bool(getattr(ep_obj, "viewCount", 0))
            if is_watched:
                continue
            params = {"key": ep_obj.ratingKey, "identifier": "com.plexapp.plugins.library"}
            if token:
                plex.query("/:/scrobble", params=params, headers={"X-Plex-Token": token})
            else:
                plex.query("/:/scrobble", params=params)
            episode_count += 1
        except Exception as exc:
            logger.debug("Failed marking episode %s - %s as watched: %s", show_title, code, exc)

    if movie_count or episode_count:
        logger.info("Marked %d movies and %d episodes as watched in Plex", movie_count, episode_count)
    else:
        logger.info("Nothing new to send to Plex.")
