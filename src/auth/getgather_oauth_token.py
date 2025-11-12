from fastmcp.server.auth import TokenVerifier
from fastmcp.server.auth.auth import AccessToken

from src.logs import logger
from src.settings import OAUTH_SCOPES, settings

GETGATHER_OATUH_TOKEN_PREFIX = "getgather"


class GetgatherAuthTokenVerifier(TokenVerifier):
    def __init__(self):
        super().__init__(required_scopes=["user"])

    async def verify_token(self, token: str) -> AccessToken | None:
        """
        Valid token format X_Y_Z, where
        - X is GETGATHER_OAUTH_TOKEN_PREFIX
        - Y is one of settings.GETGATHER_CLIENT_IDS
        - Z is not empty
        """
        parts = token.split("_")
        if (
            len(parts) < 3
            or parts[0] != GETGATHER_OATUH_TOKEN_PREFIX
            or parts[1] not in settings.GETGATHER_CLIENT_IDS
        ):
            logger.warning(f"Invalid getgather token: {token}")
            return None

        sub = "_".join(parts[2:])

        return AccessToken(
            token=token,
            client_id=parts[1],
            scopes=OAUTH_SCOPES,
            claims={"sub": sub, "auth_provider": "getgather"},
        )
