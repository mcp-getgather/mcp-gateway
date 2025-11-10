from fastmcp.server.auth import TokenVerifier
from fastmcp.server.auth.auth import AccessToken

from src.logs import logger
from src.settings import settings

GETGATHER_OATUH_TOKEN_PREFIX = "getgather"


class GetgatherAuthTokenVerifier(TokenVerifier):
    def __init__(self):
        super().__init__(required_scopes=["user"])

    async def verify_token(self, token: str) -> AccessToken | None:
        parts = token.split("_")
        if (
            len(parts) != 3
            or parts[0] != GETGATHER_OATUH_TOKEN_PREFIX
            or parts[1] not in settings.GETGATHER_CLIENT_IDS
        ):
            logger.warning(f"Invalid getgather token: {token}")
            return None

        return AccessToken(
            token=token,
            client_id=parts[1],
            scopes=["user"],
            claims={"sub": parts[2], "auth_provider": "getgather"},
        )
