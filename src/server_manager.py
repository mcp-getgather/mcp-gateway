import random
from string import Template

from src.settings import settings

# map github [username]@github to mcp server origin, example:
# SERVERS = {
#     "bin-ario@github": "localhost:23456",  # for local
#     # "bin-ario@github": "host.docker.internal:23456",  # for docker
# }
SERVERS = {
    f"{name}@github": f"mcp-{name}.flycast"
    for name in ["bin-ario", "yuxicreate", "ariya", "kpprasa", "scoutcallens"]
}


# TODO: use a database to manage the server mapping
class ServerManager:
    @classmethod
    def get_server(cls, user_login: str) -> str:
        return SERVERS[user_login]

    @classmethod
    def get_random_server(cls) -> str:
        return random.choice(list(SERVERS.values()))

    @classmethod
    def get_server_from_name(cls, name: str) -> str:
        server_host = Template(settings.SERVER_HOST_TEMPLATE).substitute(name=name)
        if server_host not in SERVERS.values():
            raise ValueError(f"Invalid server name: {name}")
        return server_host
