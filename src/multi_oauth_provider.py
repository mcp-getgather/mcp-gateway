from __future__ import annotations

from typing import get_args
from urllib.parse import quote

from fastmcp.server.auth import TokenVerifier
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.auth.providers.google import GoogleProvider
from fastmcp.utilities.storage import KVStorage
from mcp.server.auth.provider import AuthorizationCode, AuthorizationParams, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import RedirectResponse

from src.getgather_oauth_token import GETGATHER_OATUH_TOKEN_PREFIX, GetgatherAuthTokenVerifier
from src.settings import OAUTH_PROVIDER_TYPE, settings

OAUTH_PROVIDERS = list(get_args(OAUTH_PROVIDER_TYPE))

getgather_auth_provider = GetgatherAuthTokenVerifier()

github_auth_provider = GitHubProvider(
    client_id=settings.OAUTH_GITHUB_CLIENT_ID,
    client_secret=settings.OAUTH_GITHUB_CLIENT_SECRET,
    base_url=settings.GATEWAY_ORIGIN,
    required_scopes=["user"],
)

google_auth_provider = GoogleProvider(
    client_id=settings.OAUTH_GOOGLE_CLIENT_ID,
    client_secret=settings.OAUTH_GOOGLE_CLIENT_SECRET,
    base_url=settings.GATEWAY_ORIGIN,
    required_scopes=[
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
    ],
)


class MultiOAuthTokenVerifier(TokenVerifier):
    def __init__(self):
        super().__init__(required_scopes=["user"])

    async def verify_token(self, token: str) -> AccessToken | None:
        if token.startswith(GETGATHER_OATUH_TOKEN_PREFIX + "_"):
            return await getgather_auth_provider.verify_token(token)
        elif token.startswith("gho_"):
            result = await github_auth_provider.verify_token(token)
            if result:
                result.claims["auth_provider"] = "github"
        else:
            result = await google_auth_provider.verify_token(token)
            if result:
                result.claims["auth_provider"] = "google"
        if result:  # reset scopes to use default scopes
            result.scopes = ["user"]
        return result


class MultiOAuthProvider(OAuthProxy):
    def __init__(
        self,
        *,
        allowed_client_redirect_uris: list[str] = [],
        client_storage: KVStorage | None = None,
    ):
        # no upstream endpoints since multi auth provider proxies to sub auth providers, like github and google
        super().__init__(
            upstream_authorization_endpoint="",
            upstream_token_endpoint="",
            upstream_client_id="",
            upstream_client_secret="",
            token_verifier=MultiOAuthTokenVerifier(),
            base_url=settings.GATEWAY_ORIGIN,
            issuer_url=settings.GATEWAY_ORIGIN,
            allowed_client_redirect_uris=allowed_client_redirect_uris,
            client_storage=client_storage,
        )

        self._auth_providers: dict[str, OAuthProxy] = {}  # client_id -> auth provider

    def _get_auth_provider(self, client_id: str) -> OAuthProxy:
        provider = self._auth_providers.get(client_id)
        if not provider:
            raise ValueError(f"Invalid client ID: {client_id}")
        return provider

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        client_info.scope = None  # strip scopes to use default scopes
        return await super().register_client(client_info)

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        # strip scopes to use default scopes
        client.scope = None
        params.scopes = []

        github_url = await github_auth_provider.authorize(client, params)
        google_url = await google_auth_provider.authorize(client, params)
        return "/signin?github_url=" + quote(github_url) + "&google_url=" + quote(google_url)

    async def _handle_idp_callback(self, request: Request) -> RedirectResponse:
        txn_id = request.query_params.get("state")
        if not txn_id:
            raise ValueError("IdP callback missing transaction ID")

        txn = None
        provider = None
        if google_transaction := google_auth_provider._oauth_transactions.get(txn_id):
            txn = google_transaction
            provider = google_auth_provider
        elif github_transaction := github_auth_provider._oauth_transactions.get(txn_id):
            txn = github_transaction
            provider = github_auth_provider

        if not txn or not provider:
            raise ValueError("Transaction not found")

        client_id = txn.get("client_id")
        if not client_id:
            raise ValueError("Client ID not found")

        self._auth_providers[client_id] = provider

        return await provider._handle_idp_callback(request)

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        return await self._auth_providers[client.client_id].load_authorization_code(
            client, authorization_code
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        return await self._auth_providers[client.client_id].exchange_authorization_code(
            client, authorization_code
        )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        return await self._auth_providers[client.client_id].load_refresh_token(
            client, refresh_token
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        return await self._auth_providers[client.client_id].exchange_refresh_token(
            client, refresh_token, scopes
        )
