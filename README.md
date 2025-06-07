# PlexyTrackt

This project synchronizes your Plex library with Trakt. Besides watched history it can optionally add items to your Trakt collection, sync ratings, keep watchlists up to date and import movies from lists you like on Trakt. A small Flask web interface lets you choose which features to enable and configure the sync interval. Items that are manually marked as watched in Plex are detected as well.

## Requirements

The application expects the following API credentials:

- `PLEX_BASEURL` – URL of your Plex server, e.g. `http://localhost:32400`.
- `PLEX_TOKEN` – your Plex authentication token.
- `TRAKT_CLIENT_ID` – client ID for your Trakt application.
- `TRAKT_CLIENT_SECRET` – client secret from your Trakt application.

You do **not** need to provide a Trakt access token or refresh token. The web
interface will guide you through authorizing the app and will store the tokens
for you.

The application uses `plexapi` version 4.15 or newer (but below 5).

If you don't already have the Trakt credentials, please see the next sections on how to obtain them.

## Getting a Plex token

1. Log in to <https://plex.tv> with your account.
2. While still signed in, open <https://plex.tv/api/resources?includeHttps=1>.
   The page shows an XML listing of your devices. Look for a `token` attribute on
   one of the `<Device>` entries and copy its value.

## Getting Trakt API credentials

1. Log in to your Trakt account and open <https://trakt.tv/oauth/applications>.
2. Create a new application. Any name will work and you can use `urn:ietf:wg:oauth:2.0:oob` as the redirect URL.
3. After saving the app you will see a **Client ID** and **Client Secret**. Keep them handy.
4. Start PlexyTrackt and open `http://localhost:5000` in your browser. The page will provide a link to authorize the application on Trakt. After authorizing, paste the code shown by Trakt into the form. The app will handle exchanging the code for tokens automatically.


## Running with Docker Compose

1. Clone this repository.
2. Create a `.env` file in the project root and define the variables listed above. Example:

```
PLEX_BASEURL=http://localhost:32400
PLEX_TOKEN=YOUR_PLEX_TOKEN
TRAKT_CLIENT_ID=YOUR_TRAKT_CLIENT_ID
TRAKT_CLIENT_SECRET=YOUR_TRAKT_CLIENT_SECRET
```

3. Build and start the application using Docker Compose:

```
docker-compose up --build
```

4. Visit `http://localhost:5000` in your browser. You can adjust the sync interval on the page and also stop the synchronization job at any time using the **Stop Sync** button. The background job starts automatically when the container is running.

That's it! The container will continue to sync your Plex and Trakt accounts according to the interval you set.


