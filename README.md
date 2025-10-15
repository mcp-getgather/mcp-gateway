# mcp-gateway

A gateway that authenticates user and routes to the user's personal [mcp-getgather](https://github.com/mcp-getgather/mcp-getgather) container.

## Prerequisite

### Docker Engine

Install from [Docker website](https://docs.docker.com/engine/install/).

### Tailscale Auth Key

Tailscale is used to create a subnet so that host and containers can access each other.

Create an auth key at [Tailscale Admin Console](https://login.tailscale.com/admin/settings/keys).

### Github OAuth App and Google OAuth App

1. Create a Github OAuth app at [Developer Settings](https://github.com/settings/developers).

2. Create a Google OAuth app at [Google Cloud Console](https://console.cloud.google.com/auth/clients).

3. For both apps, set the Authorization callback URL to be `[PROTOCOL]//[HOST]/auth/callback`

- `PROTOCOL` is `http:` or `https:`. `https:` is required in some cases, e.g., Claude Desktop Connectors.
- `HOST` is the hostname of your service, including the port.

## Run locally

1. Download an docker image for [mcp-getgather](https://github.com/mcp-getgather/mcp-getgather)

```bash
docker image pull ghcr.io/mcp-getgather/mcp-getgather
```

You can also build `mcp-getgather` image locally

```bash
cd /path/to/mcp-getgather
docker build -t mcp-getgather .
```

2. Create an `.env` file from `env.template`

```
DOCKER_PROJECT_NAME=getgather
DOCKER_NETWORK_NAME=internal-net # keep this consistent with docker-compose.yml
DOCKER_SUBNET_PREFIX=172.16.0
DOCKER_DOMAIN=docker
TS_AUTHKEY=

SERVER_IMAGE=ghcr.io/mcp-getgather/mcp-getgather # or mcp-getgather if built locally
HOST_DATA_DIR=

GATEWAY_ORIGIN=http://localhost:9000
OAUTH_GITHUB_CLIENT_ID=
OAUTH_GITHUB_CLIENT_SECRET=
OAUTH_GOOGLE_CLIENT_ID=
OAUTH_GOOGLE_CLIENT_SECRET=
```

2. Run `docker compose` to set up `tailscale` and `coredns` for networking

```bash
docker compose up -d
```

3. Approve "Subnet routes" for the tailscale router hostname
`${DOCKER_DOMAIN}-router` at [Tailscale Admin Console](https://login.tailscale.com/admin/machines)

4. Start the `fastapi` server

```bash
uvicorn src.main:app --port 9000 --reload
```
