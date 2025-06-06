import os
import requests
from flask import Flask, render_template, request, redirect, url_for
from apscheduler.schedulers.background import BackgroundScheduler
from plexapi.server import PlexServer

app = Flask(__name__)

# default sync frequency in minutes
SYNC_INTERVAL_MINUTES = 60
scheduler = BackgroundScheduler()


def get_plex_history(plex):
    """Return sets of watched movies and episodes from Plex."""
    movies = set()
    episodes = set()
    for entry in plex.history():
        # Debugging: inspect available attributes on each history entry
        try:
            print(vars(entry))
        except Exception as exc:
            print(f"Failed to inspect entry: {exc}")
        if entry.type == 'movie':
            try:
                movies.add((entry.title, entry.year))
            except AttributeError:
                item = plex.fetchItem(entry.ratingKey)
                movies.add((item.title, item.year))
        elif entry.type == 'episode':
            try:
                season = int(entry.parentIndex)
                number = int(entry.index)
                show = entry.grandparentTitle
            except AttributeError:
                item = plex.fetchItem(entry.ratingKey)
                season = item.seasonNumber
                number = item.index
                show = item.grandparentTitle
            code = f"S{season:02d}E{number:02d}"
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
    plex_baseurl = os.environ.get('PLEX_BASEURL')
    plex_token = os.environ.get('PLEX_TOKEN')
    trakt_token = os.environ.get('TRAKT_ACCESS_TOKEN')
    trakt_client_id = os.environ.get('TRAKT_CLIENT_ID')
    if not all([plex_baseurl, plex_token, trakt_token, trakt_client_id]):
        print('Missing environment variables for Plex or Trakt.')
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

    update_trakt(headers, plex_movies - trakt_movies, plex_episodes - trakt_episodes)
    update_plex(plex, trakt_movies - plex_movies, trakt_episodes - plex_episodes)


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


def start_scheduler():
    scheduler.add_job(sync, 'interval', minutes=SYNC_INTERVAL_MINUTES, id='sync_job')
    scheduler.start()


if __name__ == '__main__':
    start_scheduler()
    app.run(host='0.0.0.0', port=5000)
