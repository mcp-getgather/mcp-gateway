import random
from string import Template

import requests
from nanoid import generate

from src.settings import settings

# map github [username]@github to mcp server origin, example:
# SERVERS = {
#     "bin-ario@github": "localhost:23456",  # for local
#     # "bin-ario@github": "host.docker.internal:23456",  # for docker
# }

HEADERS = {"Authorization": f"Bearer {settings.FLY_TOKEN}", "Content-Type": "application/json"}

SERVERS = {
    f"{name}@github": f"mcp-{name}.flycast"
    for name in ["bin-ario", "yuxicreate", "ariya", "kpprasa", "scoutcallens"]
}


# TODO: use a database to manage the server mapping
class ServerManager:
    @classmethod
    def get_or_create_server(cls, user_login: str) -> str:
        server_key = f"{user_login}@github"
        if server_key not in SERVERS:
            # Create new fly.io app for this user
            app_info = bootstrap_user_app()
            SERVERS[server_key] = app_info["flycast_host"]
            return app_info["flycast_host"]
        return SERVERS[server_key]

    @classmethod
    def get_random_server(cls) -> str:
        return random.choice(list(SERVERS.values()))

    @classmethod
    def get_server_from_name(cls, name: str) -> str:
        server_host = Template(settings.SERVER_HOST_TEMPLATE).substitute(name=name)
        if server_host not in SERVERS.values():
            raise ValueError(f"Invalid server name: {name}")
        return server_host


def create_new_app_name(prefix: str = "mcp", attempts: int = 10) -> str:
    ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"  # fly app names: [a-z0-9-]

    candidate = f"{prefix}-{generate(ALPHABET, 10)}"
    if candidate in SERVERS.values():  # low chance, but just in case
        return create_new_app_name(prefix, attempts - 1)
    return candidate


def create_app(app_name, network=None):
    """Create a new Fly.io app."""
    body = {"app_name": app_name, "org_slug": settings.FLY_ORG}
    if network:
        body["network"] = network
    r = requests.post(f"{settings.FLY_MACHINES_API}/v1/apps", headers=HEADERS, json=body)
    r.raise_for_status()
    return app_name


def create_volume_for_app(app_name, size_gb=10):
    """Create a volume for the app."""
    r = requests.post(
        f"{settings.FLY_MACHINES_API}/v1/apps/{app_name}/volumes",
        headers=HEADERS,
        json={"name": settings.FLY_VOLUME_NAME, "region": settings.FLY_REGION, "size_gb": size_gb},
    )
    r.raise_for_status()
    return r.json()["id"]


def create_machine_in_app(app_name, volume_id):
    cfg = {
        "image": settings.FLY_IMAGE,
        "restart": {"policy": "always"},
        "guest": {"cpu_kind": "performance", "cpus": 4, "memory_mb": 8192},
        "services": [
            {
                "protocol": "tcp",
                "internal_port": settings.FLY_INTERNAL_PORT,
                "ports": [{"port": 80, "handlers": ["http"]}],
            }
        ],
        "mounts": [{"volume": volume_id, "path": settings.FLY_MOUNT_PATH}],
    }
    r = requests.post(
        f"{settings.FLY_MACHINES_API}/v1/apps/{app_name}/machines",
        headers=HEADERS,
        json={"region": settings.FLY_REGION, "config": cfg},
    )
    r.raise_for_status()
    return r.json()["id"]


def allocate_flycast(app_name):
    """Allocate a private IPv6 (Flycast) to the app via GraphQL."""
    mutation = """
    mutation($input: AllocateIPAddressInput!) {
      allocateIpAddress(input: $input) {
        ipAddress { address type region }
      }
    }"""
    variables = {"input": {"appId": app_name, "type": "private_v6"}}
    r = requests.post(
        settings.FLY_GRAPHQL_API, headers=HEADERS, json={"query": mutation, "variables": variables}
    )
    r.raise_for_status()
    data = r.json()
    ip = data["data"]["allocateIpAddress"]["ipAddress"]
    if ip["type"] != "private_v6":
        raise RuntimeError(f"Unexpected IP type: {ip}")
    return ip["address"]


def list_app_ips(app_name):
    query = """
    query($name: String!) {
      app(name: $name) { ipAddresses { nodes { address type region } } }
    }"""
    r = requests.post(
        settings.FLY_GRAPHQL_API,
        headers=HEADERS,
        json={"query": query, "variables": {"name": app_name}},
    )
    r.raise_for_status()
    return r.json()["data"]["app"]["ipAddresses"]["nodes"]


def bootstrap_user_app(network=None):
    """Bootstrap a complete user app with app, volume, machine, and networking."""
    app = create_new_app_name("mcp")
    create_app(app, network=network)
    flycast_ip = allocate_flycast(app)
    vol_id = create_volume_for_app(app, size_gb=10)
    m_id = create_machine_in_app(app, vol_id)
    return {
        "app": app,
        "machine_id": m_id,
        "flycast_host": f"{app}.flycast",
        "flycast_ip": flycast_ip,
        "internal_host": f"{m_id}.vm.{app}.internal",
        "ips": list_app_ips(app),
    }
