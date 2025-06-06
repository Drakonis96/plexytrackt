def get_plex_history(plex):
    """Return lists of movie and episode history entries from Plex."""
    history = plex.history()
    plex_movies = []
    plex_episodes = []

    for entry in history:
        if entry.type == "movie":
            plex_movies.append(entry)
        elif entry.type == "episode":
            plex_episodes.append(entry)

    return plex_movies, plex_episodes

