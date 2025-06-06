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
from typing import Dict, List, Optional, Set, Tuple

import requests
from flask import Flask, render_template, request, redirect, url_for
from apscheduler.schedulers.background import BackgroundScheduler
from plexapi.server import PlexServer
from plexapi.exceptions import BadRequest

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
scheduler = BackgroundScheduler()

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
) -> Tuple[Dict[Tuple[str, Optional[int]], Optional[str]],
           Dict[Tuple[str, str], Optional[str]]]:
    movies: Dict[Tuple[str, Optional[int]], Optional[str]] = {}
    episodes: Dict[Tuple[str, str], Optional[str]] = {}

    logger.info("Fetching Plex history…")
    for entry in plex.history():
        watched_at = to_iso_z(getattr(entry, "viewedAt", None))

        # Movies
        if entry.type == "movie":
            title = getattr(entry, "title", None)
            year = getattr(entry, "year", None)
            if title is None or year is None:
                try:
                    item = entry.source() or plex.fetchItem(entry.ratingKey)
                    title = item.title
                    year = item.year
                except Exception as exc:
                    logger.debug("Failed to fetch movie %s from Plex: %s", entry.ratingKey, exc)
                    continue
            movies[(title, year)] = watched_at

        # Episodes
        elif entry.type == "episode":
            season = getattr(entry, "parentIndex", None)
            number = getattr(entry, "index", None)
            show = getattr(entry, "grandparentTitle", None)
            if None in (season, number, show):
                try:
                    item = entry.source() or plex.fetchItem(entry.ratingKey)
                    season = item.seasonNumber
                    number = item.index
                    show = item.grandparentTitle
                except Exception as exc:
                    logger.debug("Failed to fetch episode %s from Plex: %s", entry.ratingKey, exc)
                    continue
            code = f"S{int(season):02d}E{int(number):02d}"
            episodes[(show, code)] = watched_at

    return movies, episodes


def get_trakt_history(
    headers: dict,
) -> Tuple[Set[Tuple[str, Optional[int]]], Set[Tuple[str, str]]]:
    movies: Set[Tuple[str, Optional[int]]] = set()
    episodes: Set[Tuple[str, str]] = set()

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
                movies.add((m["title"], m["year"]))
            elif item["type"] == "episode":
                e = item["episode"]
                show = item["show"]
                episodes.add((show["title"], f"S{e['season']:02d}E{e['number']:02d}"))
        page += 1

    return movies, episodes


def update_trakt(
    headers: dict,
    movies: List[Tuple[str, Optional[int], Optional[str]]],
    episodes: List[Tuple[str, str, Optional[str]]],
) -> None:
    payload = {"movies": [], "episodes": []}

    for title, year, watched_at in movies:
        movie_obj = {"title": title}
        if year is not None:
            movie_obj["year"] = year
        if watched_at:
            movie_obj["watched_at"] = watched_at
        payload["movies"].append(movie_obj)

    for show, code, watched_at in episodes:
        season = int(code[1:3])
        number = int(code[4:6])
        ep_obj = {"title": show, "season": season, "number": number}
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
    # Movies
    for title, year in movies:
        try:
            if year:
                results = plex.library.search(title=title, year=year, libtype="movie")
            else:
                results = plex.library.search(title=title, libtype="movie")
            for item in results:
                item.markWatched()
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
                ep = show_obj.episode(season=season, episode=number)
                if ep:
                    ep.markWatched()
        except BadRequest as exc:
            logger.warning("Plex search error for %s %s: %s", show, code, exc)


# --------------------------------------------------------------------------- #
# SCHEDULER TASK
# --------------------------------------------------------------------------- #
def sync():
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

    plex_movies, plex_episodes = get_plex_history(plex)
    trakt_movies, trakt_episodes = get_trakt_history(headers)

    plex_movie_keys = set(plex_movies.keys())
    plex_episode_keys = set(plex_episodes.keys())

    logger.info(
        "Plex history:   %d movies, %d episodes",
        len(plex_movies),
        len(plex_episodes),
    )
    logger.info(
        "Trakt history:  %d movies, %d episodes",
        len(trakt_movies),
        len(trakt_episodes),
    )

    new_movies = [
        (title, year, plex_movies[(title, year)])
        for (title, year) in plex_movie_keys - trakt_movies
    ]
    new_episodes = [
        (show, code, plex_episodes[(show, code)])
        for (show, code) in plex_episode_keys - trakt_episodes
    ]

    update_trakt(headers, new_movies, new_episodes)
    update_plex(plex, trakt_movies - plex_movie_keys, trakt_episodes - plex_episode_keys)

    logger.info("Synchronization job finished")


# --------------------------------------------------------------------------- #
# FLASK ROUTES
# --------------------------------------------------------------------------- #
@app.route("/", methods=["GET", "POST"])
def index():
    global SYNC_INTERVAL_MINUTES

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
        scheduler.remove_all_jobs()
        scheduler.add_job(sync, "interval", minutes=minutes, id="sync_job")
        return redirect(url_for("index"))

    return render_template("index.html", minutes=SYNC_INTERVAL_MINUTES)


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
        return
    if not test_connections():
        logger.error("Connection test failed. Scheduler will not start.")
        return
    scheduler.add_job(sync, "interval", minutes=SYNC_INTERVAL_MINUTES, id="sync_job")
    scheduler.start()
    logger.info("Scheduler started with interval %d minutes", SYNC_INTERVAL_MINUTES)


# --------------------------------------------------------------------------- #
# MAIN
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    logger.info("Starting PlexyTrackt application")
    load_trakt_tokens()
    start_scheduler()
    app.run(host="0.0.0.0", port=5000)
