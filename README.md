# mcp-gateway

## Run locally

Run

```
uvicorn src.main:app --port 9000 --reload
```

You will need the following in `.env` file

```
GATEWAY_ORIGIN=http://localhost:9000
SERVER_HOST_TEMPLATE=$name
OAUTH_GITHUB_CLIENT_ID=
OAUTH_GITHUB_CLIENT_SECRET=
```

`name` in `SERVER_HOST_TEMPLATE` will be filled as `localhost:23456`, which is the local [mcp-getgather](https://github.com/mcp-getgather/mcp-getgather) service.

## Deploy to Fly.io

Run

```bash
fly deploy
```

You will need to set the following `fly secrets` if they have not been set before

```
# Run `fly secrets set` for the following

GATEWAY_ORIGIN=https://mcp-gateway.fly.dev
SERVER_HOST_TEMPLATE=$name.flycast
OAUTH_GITHUB_CLIENT_ID=
OAUTH_GITHUB_CLIENT_SECRET=
```

`name` in `SERVER_HOST_TEMPLATE` will be filled as the Fly app name of the [mcp-getgather](https://github.com/mcp-getgather/mcp-getgather) service.

The app can be accessed at `https://mcp-gateway.fly.dev` publicly.
