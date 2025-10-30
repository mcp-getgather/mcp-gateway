import asyncio
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import HTTPException
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyUrl, BaseModel

from src.auth import AuthUser
from src.logs import logger
from src.server_manager import Container, ServerManager
from src.settings import settings


class MCPDataResponse(BaseModel):
    user: AuthUser
    container: Container


class MCPAuthResponse(BaseModel):
    auth_url: str


_oauth_states: dict[str, "OAuthData"] = {}


class InMemoryTokenStorage(TokenStorage):
    def __init__(self):
        self.tokens: OAuthToken | None = None
        self.client_info: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client_info = client_info


@dataclass
class OAuthData:
    mcp_name: str
    auth_url: str | None = None
    auth_url_ready: asyncio.Event = field(default_factory=asyncio.Event)
    state: str | None = None
    code: str | None = None
    code_ready: asyncio.Event = field(default_factory=asyncio.Event)
    auth_completed: bool = False
    data: MCPDataResponse | None = None
    data_ready: asyncio.Event = field(default_factory=asyncio.Event)

    # one-time token storage for the oauth flow to avoid accidentally leaking tokens
    token_storage: InMemoryTokenStorage = field(default_factory=InMemoryTokenStorage)

    @classmethod
    def get(cls, state: str) -> "OAuthData | None":
        return _oauth_states.get(state, None)

    @classmethod
    def clear(cls, state: str):
        _oauth_states.pop(state, None)


async def auth_and_connect(
    mcp_name: str, state: str | None = None
) -> MCPDataResponse | MCPAuthResponse:
    try:
        return await _auth_and_connect(mcp_name, state)
    finally:
        # auth flow has 2 passes. 1st pass initializes, i.e., state == None.
        # 2nd pass handles callback with the same state, at the end, we will clear it
        if state:
            OAuthData.clear(state)


async def _auth_and_connect(
    mcp_name: str, state: str | None = None
) -> MCPDataResponse | MCPAuthResponse:
    """Create a one-time mcp client with to fetch the user and container info."""
    if state:
        logger.info(f"Resuming auth for {mcp_name} with state {state}")
        oauth_data = OAuthData.get(state)
        if not oauth_data:
            raise HTTPException(status_code=400, detail="Invalid state")
    else:
        logger.info(f"Starting new auth for {mcp_name}")
        oauth_data = OAuthData(mcp_name=mcp_name)

    if oauth_data.auth_completed:
        await oauth_data.data_ready.wait()
        if not oauth_data.data:
            raise HTTPException(status_code=500, detail="Failed to get user info")
        return oauth_data.data

    async def handle_redirect(auth_url: str):
        """
        If auth is needed, this will be called.
        Then /account/{mcp_name} will redirect the browser to the auth_url.
        """
        params = parse_qs(urlparse(auth_url).query)
        state = params.get("state", [None])[0]
        if state is None:
            raise ValueError("Missing state in redirect URL")

        _oauth_states[state] = oauth_data

        oauth_data.state = state
        oauth_data.auth_url = auth_url
        oauth_data.auth_url_ready.set()

    async def handle_callback() -> tuple[str, str | None]:
        """
        This will be called after the user is authenticated, i.e., code_ready is set,
        and the auth code is returned to OAuthClientProvider.redirect_uris.
        """
        await asyncio.wait_for(oauth_data.code_ready.wait(), timeout=60 * 10)  # 10 minutes

        if not oauth_data.code:
            raise ValueError("No code received")

        return oauth_data.code, oauth_data.state

    server_url = f"{settings.GATEWAY_ORIGIN}/{mcp_name}"
    oauth_auth = OAuthClientProvider(
        server_url=server_url,
        client_metadata=OAuthClientMetadata(
            client_name="GetGather MCP Client",
            redirect_uris=[AnyUrl(f"{settings.GATEWAY_ORIGIN}/client/auth/callback")],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope="user",
        ),
        storage=oauth_data.token_storage,
        redirect_handler=handle_redirect,
        callback_handler=handle_callback,
    )

    asyncio.create_task(_connect(server_url, oauth_auth, oauth_data))

    await oauth_data.auth_url_ready.wait()

    if not oauth_data.auth_url:
        raise HTTPException(status_code=500, detail="Failed to get auth URL")

    return MCPAuthResponse(auth_url=oauth_data.auth_url)


async def _connect(server_url: str, oauth_auth: httpx.Auth, oauth_data: OAuthData):
    """Connect to the MCP server and get the user and container info."""
    async with streamablehttp_client(server_url, auth=oauth_auth) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            logger.info(f"Connected to {server_url}")

            oauth_data.auth_completed = True
            oauth_data.code_ready.set()
            oauth_data.auth_url_ready.set()

            result = await session.call_tool("get_user_info")
            if not result.structuredContent:
                raise ValueError("Failed to get user info")

            user = AuthUser(**result.structuredContent)
            container = await ServerManager.get_user_container(user)
            oauth_data.data = MCPDataResponse(user=user, container=container)
            oauth_data.data_ready.set()


async def handle_auth_code(*, state: str, code: str):
    """Process the received auth code. Then wait for the data to be ready."""
    logger.info(f"Handling auth code for state {state}")

    oauth_data = OAuthData.get(state)
    if not oauth_data:
        raise HTTPException(status_code=400, detail="Invalid state")
    oauth_data.code = code
    oauth_data.code_ready.set()

    # wait a bit for oauth async flow to complete
    # await asyncio.sleep(5)
    await oauth_data.data_ready.wait()

    return oauth_data
