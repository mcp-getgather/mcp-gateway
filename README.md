# mcp-gateway

## Prerequisite

### Github OAuth app

Create a Github OAuth app at [Developer Settings](https://github.com/settings/developers).

Set the Authorization callback URL to be `[PROTOCOL]//[HOST]/[CALLBACK_PATH]`.

- `PROTOCOL` is `http:` or `https:`. `https:` is required in some cases, e.g., Claude Desktop Connectors.
- `HOST` is the hostname of your service, including the port.
- `CALLBACK_PATH` is default to `/auth/github/callback`, see below for more details.

## Run locally

Make sure Authorization callback URL for your Github OAuth app is `http://localhost:9000/auth/github/callback`.

- Change port `9000` to match your port if you use a different one.
- You can change `CALLBACK_PATH` via env variable `OAUTH_GITHUB_REDIRECT_PATH`.

You will need the additional env variables in `.env` file

```
# .env file

GATEWAY_ORIGIN=http://localhost:9000
SERVER_HOST_TEMPLATE=$name
OAUTH_GITHUB_CLIENT_ID=
OAUTH_GITHUB_CLIENT_SECRET=
OAUTH_GITHUB_REDIRECT_PATH=/auth/github/callback
```

`name` in `SERVER_HOST_TEMPLATE` will be filled as `localhost:23456` at run time, which is the hostname of the local [mcp-getgather](https://github.com/mcp-getgather/mcp-getgather) service.

Run

```bash
uvicorn src.main:app --port 9000 --reload
```

## Deploy to Fly.io

Make sure Authorization callback URL for your Github OAuth app is `https://[FLY_APP_NAME].fly.dev/auth/github/callback`.

- `FLY_APP_NAME` is the name of your fly app.
- You can change `CALLBACK_PATH` via env variable `OAUTH_GITHUB_REDIRECT_PATH`.

Run `fly secrets set` for the following env variables if they have not been set before

```
GATEWAY_ORIGIN=https://[FLY_APP_NAME].fly.dev
OAUTH_GITHUB_CLIENT_ID=
OAUTH_GITHUB_CLIENT_SECRET=
OAUTH_GITHUB_REDIRECT_PATH=/auth/github/callback
```

Run

```bash
fly deploy
```
