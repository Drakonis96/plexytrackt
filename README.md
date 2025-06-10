# PlexyTrack

<p align="center">
  <img src="static/logo.png" alt="PlexyTrack Logo" width="200" />
</p>

This project synchronizes your Plex library with Trakt. Besides watched history it can optionally add items to your Trakt collection, sync ratings and watchlists, and now mirrors Trakt lists you like as Plex collections. Collections created in Plex will in turn appear as Trakt lists. A small Flask web interface lets you choose which features to enable and configure the sync interval. Items that are manually marked as watched in Plex are detected as well. The interface also includes a tool for creating backups of your history, watchlist and ratings.

## Disclaimer

This application is currently in testing and is provided **as is**. I take no responsibility for any data loss that may occur when using it. Before you begin, you should use the backup tool to export your history, watchlist and ratings so they can be restored easily from the UI.

## Requirements

The application expects the following API credentials:

- `PLEX_BASEURL` – URL of your Plex server, e.g. `http://localhost:32400`.
- `PLEX_TOKEN` – your Plex authentication token.
- `TRAKT_CLIENT_ID` – client ID for your Trakt application.
- `TRAKT_CLIENT_SECRET` – client secret from your Trakt application.
- `SIMKL_CLIENT_ID` – client ID for your Simkl application (optional).
- `SIMKL_CLIENT_SECRET` – client secret for your Simkl application (optional).
- `SIMKL_SYNC_ACTIVATED` – set to `true` to enable Simkl synchronization.
- `TRAKT_REDIRECT_URI` – OAuth redirect URI for Trakt (defaults to
  `urn:ietf:wg:oauth:2.0:oob`).
- `SIMKL_REDIRECT_URI` – OAuth redirect URI for Simkl (defaults to
  `urn:ietf:wg:oauth:2.0:oob`).
- `TZ` – timezone for log timestamps, defaults to `Europe/Madrid`.

You do **not** need to provide a Trakt access token or refresh token. The web
interface will guide you through authorizing the app and will store the tokens
for you.

If `SIMKL_SYNC_ACTIVATED` is set to `true`, the application will prompt for
authorization with Simkl right after authorizing Trakt.

When specifying a custom redirect URI for either service, make sure the same
value is configured in the corresponding Trakt or Simkl application
settings and in the environment variables `TRAKT_REDIRECT_URI` and
`SIMKL_REDIRECT_URI`.

The application uses `plexapi` version 4.15 or newer (but below 5).

If you don't already have the Trakt credentials, please see the next sections on how to obtain them.

### Collections and watchlists

If you mark a list as liked on Trakt, PlexyTrack will create a Plex collection with the same name and add the matching movies or shows found in your library. Likewise, any collection created in Plex will be mirrored as a list on Trakt. Watchlists are also kept in sync both ways, so adding or removing an item in one platform is reflected in the other.

### Backup and restore

From the web interface you can download a backup file containing your Trakt history, watchlist and ratings. This JSON file can be uploaded again to restore your data at any time. Creating a backup is recommended before starting synchronization for the first time.

### Live sync

When **Live Sync** is enabled on the main page, PlexyTrack will start a
webhook endpoint at `/webhook`. Configure a Plex Webhook to call this URL and
the application will trigger an immediate sync whenever an event is received.

## Getting a Plex token

1. Open the Plex Web application and sign in.
2. Pick any movie or show and click the three dots in the lower right corner to
   open **Get Info**.
3. In the information panel choose **View XML**. A new tab will display the XML
   data for that item.
4. Copy the value of `X-Plex-Token` from the URL bar – that's your token.
5. If you need more details, Plex offers instructions at <https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/>.

## Getting Trakt API credentials

1. Log in to your Trakt account and open <https://trakt.tv/oauth/applications>.
2. Create a new application. Any name will work and you can use `urn:ietf:wg:oauth:2.0:oob` as the redirect URL. If you want the authorization code to be filled automatically, set the redirect URL to something like `http://localhost:5000/trakt` and use the same value in the `TRAKT_REDIRECT_URI` environment variable.
3. After saving the app you will see a **Client ID** and **Client Secret**. Keep them handy.
4. Start PlexyTrack and open `http://localhost:5000` in your browser. The page will provide a link to authorize the application on Trakt. After authorizing, paste the code shown by Trakt into the form. The app will handle exchanging the code for tokens automatically.


## Running with Docker Compose

1. Clone this repository.
2. Create a `.env` file in the project root and define the variables listed above. Example:

```
PLEX_BASEURL=http://localhost:32400
PLEX_TOKEN=YOUR_PLEX_TOKEN
TRAKT_CLIENT_ID=YOUR_TRAKT_CLIENT_ID
TRAKT_CLIENT_SECRET=YOUR_TRAKT_CLIENT_SECRET
SIMKL_CLIENT_ID=YOUR_SIMKL_CLIENT_ID
SIMKL_CLIENT_SECRET=YOUR_SIMKL_CLIENT_SECRET
SIMKL_SYNC_ACTIVATED=false
TRAKT_REDIRECT_URI=urn:ietf:wg:oauth:2.0:oob
SIMKL_REDIRECT_URI=urn:ietf:wg:oauth:2.0:oob
TZ=Europe/Madrid
```

3. Start the application using Docker Compose. The default `docker-compose.yml`
   pulls the image from Docker Hub:

```
docker-compose up -d
```

If you prefer to build the image locally use the `docker-compose-local.yml`
file:

```
docker-compose -f docker-compose-local.yml up --build
```

4. Visit `http://localhost:5000` in your browser. You can adjust the sync interval on the page and also stop the synchronization job at any time using the **Stop Sync** button. The background job starts automatically when the container is running.

That's it! The container will continue to sync your Plex and Trakt accounts according to the interval you set.


