import logging
from typing import Dict, Optional, Set, Tuple, Union

from .utils import (
    _parse_guid_value,
    get_show_from_library,
    imdb_guid,
    normalize_year,
    to_iso_z,
    valid_guid,
)

logger = logging.getLogger(__name__)


def get_plex_history(plex) -> Tuple[
    Dict[str, Dict[str, Optional[str]]],
    Dict[str, Dict[str, Optional[str]]],
]:
    """Return watched movies and episodes from Plex keyed by GUID."""
    movies: Dict[str, Dict[str, Optional[str]]] = {}
    episodes: Dict[str, Dict[str, Optional[str]]] = {}
    show_guid_cache: Dict[str, Optional[str]] = {}

    logger.info("Fetching Plex history…")
    for entry in plex.history():
        watched_at = to_iso_z(getattr(entry, "viewedAt", None))

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

            series_guid: Optional[str] = None
            if item is not None:
                gp_guid_raw = getattr(item, "grandparentGuid", None)
                if gp_guid_raw:
                    series_guid = _parse_guid_value(gp_guid_raw)
            if series_guid is None and show in show_guid_cache:
                series_guid = show_guid_cache[show]
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
                    show_title = getattr(ep, "grandparentTitle", None)
                    if guid:
                        key: Union[str, Tuple[str, str]] = guid
                    else:
                        series_guid: Optional[str] = None
                        gp_guid_raw = getattr(ep, "grandparentGuid", None)
                        if gp_guid_raw:
                            series_guid = _parse_guid_value(gp_guid_raw)
                        if series_guid is None and show_title and show_title in show_guid_cache:
                            series_guid = show_guid_cache[show_title]
                        if series_guid is None and show_title:
                            series_obj = get_show_from_library(plex, show_title)
                            series_guid = imdb_guid(series_obj) if series_obj else None
                            show_guid_cache[show_title] = series_guid
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
            logger.debug("Failed fetching watched items from section %s: %s", section.title, exc)

    return movies, episodes


def update_plex(
    plex,
    movies: Set[Tuple[str, Optional[int], Optional[str]]],
    episodes: Set[Tuple[str, str, Optional[Union[str, Tuple[str, str]]]]],
) -> None:
    """Mark items as watched in Plex when missing."""
    movie_count = 0
    episode_count = 0

    for title, year, guid in movies:
        if guid and valid_guid(guid):
            try:
                item = plex.fetchItem(guid)
                if getattr(item, "isWatched", lambda: bool(getattr(item, "viewCount", 0)))():
                    continue
                item.markWatched()
                movie_count += 1
                continue
            except Exception as exc:
                logger.debug("GUID fetch failed for %s: %s", guid, exc)

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
            if getattr(found, "isWatched", lambda: bool(getattr(found, "viewCount", 0)))():
                continue
            found.markWatched()
            movie_count += 1
        except Exception as exc:
            logger.debug("Failed to mark movie '%s' as watched: %s", found.title, exc)

    for show_title, code, key in episodes:
        guid: Optional[str] = None
        if isinstance(key, str):
            guid = key if valid_guid(key) else None
        elif isinstance(key, tuple) and key and isinstance(key[0], str):
            guid = key[0] if valid_guid(key[0]) else None

        if guid:
            try:
                item = plex.fetchItem(guid)
                if getattr(item, "isWatched", lambda: bool(getattr(item, "viewCount", 0)))():
                    continue
                item.markWatched()
                episode_count += 1
                continue
            except Exception as exc:
                logger.debug("GUID fetch failed for %s: %s", guid, exc)

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
            season_obj = show_obj.season(season_num)
            ep_obj = season_obj.episode(episode_num)
            if getattr(ep_obj, "isWatched", lambda: bool(getattr(ep_obj, "viewCount", 0)))():
                continue
            ep_obj.markWatched()
            episode_count += 1
        except Exception as exc:
            logger.debug("Failed marking episode %s - %s as watched: %s", show_title, code, exc)

    if movie_count or episode_count:
        logger.info("Marked %d movies and %d episodes as watched in Plex", movie_count, episode_count)
    else:
        logger.info("Nothing new to send to Plex.")
