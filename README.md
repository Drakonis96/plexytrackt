# PlexyTrackt

This project syncs watched history between Plex and Trakt. It runs a small Flask web interface that allows you to configure the sync interval. The actual synchronization happens in the background using the APIs of both services.

## Requirements

The application expects the following API credentials:

- `PLEX_BASEURL` – URL of your Plex server, e.g. `http://localhost:32400`.
- `PLEX_TOKEN` – your Plex authentication token.
- `TRAKT_ACCESS_TOKEN` – access token for Trakt.
- `TRAKT_CLIENT_ID` – client ID for your Trakt application.

If you don't already have the tokens, please see the official Plex and Trakt documentation for generating them.

## Running with Docker Compose

1. Clone this repository.
2. Create a `.env` file in the project root and define the four variables listed above. Example:

```
PLEX_BASEURL=http://localhost:32400
PLEX_TOKEN=YOUR_PLEX_TOKEN
TRAKT_ACCESS_TOKEN=YOUR_TRAKT_TOKEN
TRAKT_CLIENT_ID=YOUR_TRAKT_CLIENT_ID
```

3. Build and start the application using Docker Compose:

```
docker-compose up --build
```

4. Visit `http://localhost:5000` in your browser. You can adjust the sync interval on the page. The background job starts automatically when the container is running.

That's it! The container will continue to sync your Plex and Trakt accounts according to the interval you set.


