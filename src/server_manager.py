import contextvars
import random
from functools import cached_property
from string import Template

from pydantic import BaseModel, Field

from src.settings import PROJECT_DIR, settings

# map github [username]@github to mcp server origin, example:
# SERVERS = {
#     "bin-ario@github": "localhost:23456",  # for local
#     # "bin-ario@github": "host.docker.internal:23456",  # for docker
# }
# SERVERS = {
#     f"{name}@github": f"mcp-{name}.flycast"
#     for name in ["bin-ario", "yuxicreate", "ariya", "kpprasa", "scoutcallens"]
# }


class UserServer(BaseModel):
    user_login: str  # [github username]@github
    server_host: str


SERVER_CONFIG = contextvars.ContextVar("SERVER_CONFIG")


class ServerConfig(BaseModel):
    server_list: list[UserServer] = Field(default_factory=list)

    @cached_property
    def servers(self) -> dict[str, str]:
        return {server.user_login: server.server_host for server in self.server_list}

    @classmethod
    def load(cls):
        with open(PROJECT_DIR / settings.SERVER_CONFIG_PATH, "r") as f:
            SERVER_CONFIG.set(ServerConfig.model_validate_json(f.read()))
        return cls.get()

    @classmethod
    def get(cls):
        return SERVER_CONFIG.get()


# TODO: use a database to manage the server mapping
class ServerManager:
    @classmethod
    def get_server_from_user(cls, user_login: str) -> str:
        return ServerConfig.get().servers[user_login]

    @classmethod
    def get_random_server(cls) -> str:
        return random.choice(list(ServerConfig.get().servers.values()))

    @classmethod
    def get_server_from_name(cls, name: str) -> str:
        server_host = Template(settings.SERVER_HOST_TEMPLATE).substitute(name=name)
        if server_host not in ServerConfig.get().servers.values():
            raise ValueError(f"Invalid server name: {name}")
        return server_host
