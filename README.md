# mcp-gateway

`mcp-gateway` manages a group of [mcp-getgather](https://github.com/mcp-getgather/mcp-getgather) containers. It authenticates user and routes to the user's personal `mcp-getgather` container.

Below are instructions to run `mcp-gateway` on MacOS for development.

## Prerequisite

### Container Engine

Both Docker and Podman are supported. Install [Docker](https://docs.docker.com/engine/install/) or [Podman](https://podman.io/docs/installation).

Make sure to allocate enough CPUs and Memories to the engine.

### Tailscale Auth Key

Tailscale is used to create a subnet so that host and containers can access each other.

Create an auth key at [Tailscale Admin Console](https://login.tailscale.com/admin/settings/keys).

### Github OAuth App and Google OAuth App

1. Create a Github OAuth app at [Developer Settings](https://github.com/settings/developers).

2. Create a Google OAuth app at [Google Cloud Console](https://console.cloud.google.com/auth/clients).

3. For both apps, set the Authorization callback URL to be `[PROTOCOL]//[HOST]/auth/callback`

- `PROTOCOL` is `http:` or `https:`. `https:` is required in some cases, e.g., Claude Desktop Connectors.
- `HOST` is the hostname of your service, including the port.

### Docker Compose

Docker compose is used to set up subnet and tailscale router. It should be installed if Docker Desktop is installed, otherwise, install it [here](https://docs.docker.com/compose/install/). It works for both Docker and Podman.

## Run locally

1. Create an `.env` file from `env.template`. If using a proxy service follow the format outlined in the env.template which uses toml for the proxy configuration.

```
CONTAINER_ENGINE=docker # or podman
CONTAINER_PROJECT_NAME=getgather
CONTAINER_SUBNET_PREFIX=172.20.0
TS_AUTHKEY=

HOST_DATA_DIR=

GATEWAY_ORIGIN=http://localhost:9000
OAUTH_GITHUB_CLIENT_ID=
OAUTH_GITHUB_CLIENT_SECRET=
OAUTH_GOOGLE_CLIENT_ID=
OAUTH_GOOGLE_CLIENT_SECRET=
```

Make sure `CONTAINER_SUBNET_PREFIX` doesn't conflict with existing ones. You can look up existing subnets by

```bash
# Install tailscale cli if not already
brew install tailscale

tailscale status --json | jq -r '
  .Peer
  | to_entries[]
  | .value as $peer
  | $peer.HostName as $host
  | ($peer.AllowedIPs? // $peer.PrimaryRoutes? // [])
  | map(select(test("^(100\\.|fd7a:115c:a1e0:)") | not))[]
  | "\(.)\t\($host)"
' | sort -u
```

2. Download the latest [mcp-getgather](https://github.com/mcp-getgather/mcp-getgather) image

```bash
docker image pull ghcr.io/mcp-getgather/mcp-getgather
docker tag ghcr.io/mcp-getgather/mcp-getgather ${CONTAINER_PROJECT_NAME}_mcp-getgather
# or
podman image pull ghcr.io/mcp-getgather/mcp-getgather
podman tag ghcr.io/mcp-getgather/mcp-getgather ${CONTAINER_PROJECT_NAME}_mcp-getgather
```

You can also build `mcp-getgather` image locally

```bash
cd /path/to/mcp-getgather
docker build -t ${CONTAINER_PROJECT_NAME}_mcp-getgather .
```

3. Run `docker compose` to set up `tailscale` for networking

```bash
docker compose up -d
# or podman
podmam compose up -d
```

4. Approve "Subnet routes" for the tailscale router hostname `${CONTAINER_PROJECT_NAME}-router` at [Tailscale Admin Console](https://login.tailscale.com/admin/machines)

5. Install dependencies

```bash
uv sync
npm install
```

6. Start the `fastapi` server

```bash
npm run dev
```
