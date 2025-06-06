import os
import logging
from datetime import datetime
import requests
from flask import Flask, render_template, request, redirect, url_for
from apscheduler.schedulers.background import BackgroundScheduler
from plexapi.server import PlexServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# default sync frequency in minutes
SYNC_INTERVAL_MINUTES = 60
scheduler = BackgroundScheduler()

TRAKT_REDIRECT_URI = 'urn:ietf:wg:oauth:2.0:oob'


def refresh_trakt_token():
    """Refresh the Trakt access token using the stored refresh token."""
    refresh_token = os.environ.get('TRAKT_REFRESH_TOKEN')
    client_id = os.environ.get('TRAKT_CLIENT_ID')
    client_secret = os.environ.get('TRAKT_CLIENT_SECRET')
    if not all([refresh_token, client_id, client_secret]):
        logger.error('Missing Trakt OAuth environment variables.')
        return None
    payload = {
        'refresh_token': refresh_token,
        'client_id': client_id,
        'client_secret': client_secret,
        'redirect_uri': TRAKT_REDIRECT_URI,
        'grant_type': 'refresh_token',
    }
    try:
        resp = requests.post('https://api.trakt.tv/oauth/token', json=payload)
        resp.raise_for_status()
    except Exception as exc:
        logger.error('Failed to refresh Trakt token: %s', exc)
        return None
    data = resp.json()
    os.environ['TRAKT_ACCESS_TOKEN'] = data['access_token']
    os.environ['TRAKT_REFRESH_TOKEN'] = data.get('refresh_token', refresh_token)
    logger.info('Trakt access token refreshed')
    return data['access_token']


def trakt_request(method, endpoint, headers, **kwargs):
    """Make a request to Trakt, refreshing the token if needed."""
    url = f'https://api.trakt.tv{endpoint}'
    resp = requests.request(method, url, headers=headers, **kwargs)
    if resp.status_code == 401:
        new_token = refresh_trakt_token()
        if new_token:
            headers['Authorization'] = f'Bearer {new_token}'
            resp = requests.request(method, url, headers=headers, **kwargs)
    resp.raise_for_status()
    return resp


def get_plex_history(plex):
    """Return mapping of watched movies and episodes from Plex with timestamps."""
    movies = {}
    episodes = {}
    for entry in plex.history():
        # PlexAPI >= 4.13 no longer exposes ``item`` on history entries. When
        # key fields like title/year are missing we reload the entry using the
        # ``source`` helper (falls back to ``fetchItem``) so we don't raise an
        # AttributeError.
        watched_ts = getattr(entry, "viewedAt", None)
        watched_at = None
        if watched_ts:
            watched_at = datetime.utcfromtimestamp(int(watched_ts)).isoformat() + "Z"
        if entry.type == 'movie':
            title = getattr(entry, 'title', None)
            year = getattr(entry, 'year', None)
            if title is None or year is None:
                try:
                    item = entry.source() or plex.fetchItem(entry.ratingKey)
                    title = item.title
                    year = item.year
                except Exception as exc:
                    logger.debug(
                        "Failed to fetch movie %s from Plex: %s", entry.ratingKey, exc
                    )
                    continue
            movies[(title, year)] = watched_at
        elif entry.type == 'episode':
            season = getattr(entry, 'parentIndex', None)
            number = getattr(entry, 'index', None)
            show = getattr(entry, 'grandparentTitle', None)
            if None in (season, number, show):
                try:
                    item = entry.source() or plex.fetchItem(entry.ratingKey)
                    season = item.seasonNumber
                    number = item.index
                    show = item.grandparentTitle
                except Exception as exc:
                    logger.debug(
                        "Failed to fetch episode %s from Plex: %s", entry.ratingKey, exc
                    )
                    continue
            code = f"S{int(season):02d}E{int(number):02d}"
            episodes[(show, code)] = watched_at
    return movies, episodes


def get_trakt_history(headers):
    """Return sets of watched movies and episodes from Trakt."""
    movies = set()
    episodes = set()
    page = 1
    while True:
        resp = trakt_request(
            'GET',
            '/sync/history',
            headers,
            params={'page': page, 'limit': 100},
        )
        data = resp.json()
        if not data:
            break
        for item in data:
            if item['type'] == 'movie':
                m = item['movie']
                movies.add((m['title'], m['year']))
            elif item['type'] == 'episode':
                e = item['episode']
                show = item['show']
                episodes.add((show['title'], f"S{e['season']:02d}E{e['number']:02d}"))
        page += 1
    return movies, episodes


def update_trakt(headers, movies, episodes):
    """Send watched history to Trakt."""
    payload = {'movies': [], 'episodes': []}
    for title, year, watched_at in movies:
        movie_obj = {'title': title, 'year': year}
        if watched_at:
            movie_obj['watched_at'] = watched_at
        payload['movies'].append(movie_obj)
    for show, code, watched_at in episodes:
        season = int(code[1:3])
        number = int(code[4:6])
        ep_obj = {'title': show, 'season': season, 'number': number}
        if watched_at:
            ep_obj['watched_at'] = watched_at
        payload['episodes'].append(ep_obj)
    if not payload['movies'] and not payload['episodes']:
        return
    trakt_request('POST', '/sync/history', headers, json=payload)


def update_plex(plex, movies, episodes):
    """Mark items as watched on Plex."""
    for title, year in movies:
        results = plex.library.search(title=title, year=year, libtype='movie')
        for item in results:
            item.markWatched()
    for show, code in episodes:
        season = int(code[1:3])
        number = int(code[4:6])
        results = plex.library.searchShows(title=show)
        for show_obj in results:
            ep = show_obj.get_episode(season=season, episode=number)
            if ep:
                ep.markWatched()


def sync():
    logger.info('Starting synchronization job')
    plex_baseurl = os.environ.get('PLEX_BASEURL')
    plex_token = os.environ.get('PLEX_TOKEN')
    trakt_token = os.environ.get('TRAKT_ACCESS_TOKEN')
    trakt_client_id = os.environ.get('TRAKT_CLIENT_ID')
    if not all([plex_baseurl, plex_token, trakt_token, trakt_client_id]):
        logger.error('Missing environment variables for Plex or Trakt.')
        return

    plex = PlexServer(plex_baseurl, plex_token)
    headers = {
        'Authorization': f'Bearer {trakt_token}',
        'Content-Type': 'application/json',
        'User-Agent': 'PlexyTrackt/1.0.0',
        'trakt-api-version': '2',
        'trakt-api-key': trakt_client_id,
    }

    plex_movies, plex_episodes = get_plex_history(plex)
    trakt_movies, trakt_episodes = get_trakt_history(headers)

    plex_movie_keys = set(plex_movies.keys())
    plex_episode_keys = set(plex_episodes.keys())

    logger.info(
        'Plex history contains %d movies and %d episodes',
        len(plex_movies),
        len(plex_episodes),
    )
    logger.info(
        'Trakt history contains %d movies and %d episodes',
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
    logger.info('Synchronization job finished')


@app.route('/', methods=['GET', 'POST'])
def index():
    global SYNC_INTERVAL_MINUTES
    if request.method == 'POST':
        minutes = int(request.form.get('minutes', 60))
        SYNC_INTERVAL_MINUTES = minutes
        scheduler.remove_all_jobs()
        scheduler.add_job(sync, 'interval', minutes=minutes, id='sync_job')
        return redirect(url_for('index'))
    return render_template('index.html', minutes=SYNC_INTERVAL_MINUTES)


def test_connections():
    """Verify connectivity to Plex and Trakt before starting."""
    plex_baseurl = os.environ.get('PLEX_BASEURL')
    plex_token = os.environ.get('PLEX_TOKEN')
    trakt_token = os.environ.get('TRAKT_ACCESS_TOKEN')
    trakt_client_id = os.environ.get('TRAKT_CLIENT_ID')

    if not all([plex_baseurl, plex_token, trakt_token, trakt_client_id]):
        logger.error('Missing environment variables for Plex or Trakt.')
        return False

    try:
        PlexServer(plex_baseurl, plex_token).account()
        logger.info('Successfully connected to Plex.')
    except Exception as exc:
        logger.error('Failed to connect to Plex: %s', exc)
        return False

    headers = {
        'Authorization': f'Bearer {trakt_token}',
        'Content-Type': 'application/json',
        'User-Agent': 'PlexyTrackt/1.0.0',
        'trakt-api-version': '2',
        'trakt-api-key': trakt_client_id,
    }
    try:
        trakt_request('GET', '/users/settings', headers)
        logger.info('Successfully connected to Trakt.')
    except Exception as exc:
        logger.error('Failed to connect to Trakt: %s', exc)
        return False

    return True


def start_scheduler():
    if not test_connections():
        logger.error('Connection test failed. Scheduler will not start.')
        return
    scheduler.add_job(sync, 'interval', minutes=SYNC_INTERVAL_MINUTES, id='sync_job')
    scheduler.start()
    logger.info('Scheduler started with interval %d minutes', SYNC_INTERVAL_MINUTES)


if __name__ == '__main__':
    logger.info('Starting PlexyTrackt application')
    start_scheduler()
    app.run(host='0.0.0.0', port=5000)
