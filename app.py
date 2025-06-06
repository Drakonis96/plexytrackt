import os
import logging
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


def get_plex_history(plex):
    """Return sets of watched movies and episodes from Plex."""
    movies = set()
    episodes = set()
    for entry in plex.history():
        # Some versions of plexapi return partial objects without an ``item``
        # attribute. Fall back to fetching the full item by ``ratingKey`` when
        # required so we don't raise an AttributeError.
        if entry.type == 'movie':
            title = getattr(entry, 'title', None)
            year = getattr(entry, 'year', None)
            if title is None or year is None:
                try:
                    item = plex.fetchItem(entry.ratingKey)
                    title = item.title
                    year = item.year
                except Exception as exc:
                    logger.debug(
                        "Failed to fetch movie %s from Plex: %s", entry.ratingKey, exc
                    )
                    continue
            movies.add((title, year))
        elif entry.type == 'episode':
            season = getattr(entry, 'parentIndex', None)
            number = getattr(entry, 'index', None)
            show = getattr(entry, 'grandparentTitle', None)
            if None in (season, number, show):
                try:
                    item = plex.fetchItem(entry.ratingKey)
                    season = item.seasonNumber
                    number = item.index
                    show = item.grandparentTitle
                except Exception as exc:
                    logger.debug(
                        "Failed to fetch episode %s from Plex: %s", entry.ratingKey, exc
                    )
                    continue
            code = f"S{int(season):02d}E{int(number):02d}"
            episodes.add((show, code))
    return movies, episodes


def get_trakt_history(headers):
    """Return sets of watched movies and episodes from Trakt."""
    movies = set()
    episodes = set()
    page = 1
    while True:
        resp = requests.get(
            f'https://api.trakt.tv/sync/history',
            headers=headers,
            params={'page': page, 'limit': 100}
        )
        resp.raise_for_status()
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
    for title, year in movies:
        payload['movies'].append({'title': title, 'year': year})
    for show, code in episodes:
        season = int(code[1:3])
        number = int(code[4:6])
        payload['episodes'].append({'title': show, 'season': season, 'number': number})
    if not payload['movies'] and not payload['episodes']:
        return
    resp = requests.post('https://api.trakt.tv/sync/history', json=payload, headers=headers)
    resp.raise_for_status()


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
        'trakt-api-version': '2',
        'trakt-api-key': trakt_client_id,
    }

    plex_movies, plex_episodes = get_plex_history(plex)
    trakt_movies, trakt_episodes = get_trakt_history(headers)

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

    update_trakt(headers, plex_movies - trakt_movies, plex_episodes - trakt_episodes)
    update_plex(plex, trakt_movies - plex_movies, trakt_episodes - plex_episodes)
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
        'trakt-api-version': '2',
        'trakt-api-key': trakt_client_id,
    }
    try:
        resp = requests.get('https://api.trakt.tv/users/settings', headers=headers)
        resp.raise_for_status()
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
