# PlexyTrackt

This project syncs watched history between Plex and Trakt. It runs a small Flask web interface that allows you to configure the sync interval. The actual synchronization happens in the background using the APIs of both services.

## Requirements

The application expects the following API credentials:

- `PLEX_BASEURL` – URL of your Plex server, e.g. `http://localhost:32400`.
- `PLEX_TOKEN` – your Plex authentication token.
- `TRAKT_ACCESS_TOKEN` – access token for Trakt.
- `TRAKT_CLIENT_ID` – client ID for your Trakt application.

If you don't already have the tokens, please see the next sections on how to obtain them.

## Getting a Plex token

1. Log in to <https://plex.tv> with your account.
2. While still signed in, open <https://plex.tv/api/resources?includeHttps=1>.
   The page shows an XML listing of your devices. Look for a `token` attribute on
   one of the `<Device>` entries and copy its value.

## Getting Trakt API credentials

1. Log in to your Trakt account and open <https://trakt.tv/oauth/applications>.
2. Create a new application. Any name will work and you can use `urn:ietf:wg:oauth:2.0:oob` as the redirect URL.
3. After saving the app you will see a **Client ID** and **Client Secret**. Keep them handy.
4. Visit the following URL in your browser, replacing `YOUR_CLIENT_ID` with the value from step 3:

   ```
   https://trakt.tv/oauth/authorize?response_type=code&client_id=YOUR_CLIENT_ID&redirect_uri=urn:ietf:wg:oauth:2.0:oob
   ```

   Authorize the application and copy the code shown on the page.
5. Exchange the code for an access token:

   ```bash
   curl -X POST https://api.trakt.tv/oauth/token \
     -H "Content-Type: application/json" \
     -d '{
       "code": "YOUR_CODE",
       "client_id": "YOUR_CLIENT_ID",
       "client_secret": "YOUR_CLIENT_SECRET",
       "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
       "grant_type": "authorization_code"
     }'
   ```

   The response contains an `access_token` that you can use together with the client ID.

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


