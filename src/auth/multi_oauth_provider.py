from __future__ import annotations

from urllib.parse import quote

from fastmcp.server.auth import TokenVerifier
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.utilities.storage import KVStorage
from mcp.server.auth.provider import AuthorizationCode, AuthorizationParams, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import RedirectResponse

from src.auth.constants import (
    GETGATHER_OAUTH_PROVIDER_NAME,
    GETGATHER_PERSISTENT_OAUTH_PROVIDER_NAME,
    OAUTH_SCOPES,
)
from src.auth.getgather_oauth_token import GetgatherAuthTokenVerifier
from src.auth.third_party_providers import ThirdPartyOAuth
from src.http_utils import get_server_origin
from src.settings import settings

getgather_auth_provider = GetgatherAuthTokenVerifier()


def auth_enabled(server_origin: str) -> bool:
    return ThirdPartyOAuth.has_providers(server_origin) or bool(settings.GETGATHER_APPS)


class MultiOAuthTokenVerifier(TokenVerifier):
    def __init__(self):
        super().__init__(required_scopes=OAUTH_SCOPES)

    async def verify_token(self, token: str) -> AccessToken | None:
        """
        Choose the appropriate OAuth provider based on the token prefix.
        For the same provider name (github or google), if there are multiple providers configured for
        multiple domains, it's ok to use any of them for verification.
        """
        if token.startswith(GETGATHER_OAUTH_PROVIDER_NAME) or token.startswith(
            GETGATHER_PERSISTENT_OAUTH_PROVIDER_NAME
        ):
            return await getgather_auth_provider.verify_token(token)
        elif token.startswith("gho_") or token.startswith("ghp_") or token.startswith("ghu_"):
            # gho_ is for oauth apps, ghp_ is for personal access tokens, ghu_ is for github apps
            github_provider = ThirdPartyOAuth.get_provider_for_name("github")
            if not github_provider:
                raise ValueError("GitHub OAuth provider not configured")

            result = await github_provider.verify_token(token)
            if result:
                result.claims["auth_provider"] = "github"
        else:
            google_provider = ThirdPartyOAuth.get_provider_for_name("google")
            if not google_provider:
                raise ValueError("Google OAuth provider not configured")

            result = await google_provider.verify_token(token)
            if result:
                result.claims["auth_provider"] = "google"

        if result:  # reset scopes to use default scopes
            result.scopes = OAUTH_SCOPES
        return result


class MultiOAuthProvider(OAuthProxy):
    """
    Coordinator for multiple OAuth providers, including third party providers (github and google)
    and getgather provider, which uses predefined tokens in settings.GETGATHER_APPS.
    """

    def __init__(
        self,
        *,
        base_url: str,
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
            base_url=base_url,
            allowed_client_redirect_uris=allowed_client_redirect_uris,
            client_storage=client_storage,
            valid_scopes=ThirdPartyOAuth.get_scopes(),
        )

        self._auth_providers: dict[str, OAuthProxy] = {}  # client_id -> auth provider

    def _get_auth_provider(self, client_id: str) -> OAuthProxy:
        provider = self._auth_providers.get(client_id)
        if not provider:
            raise ValueError(f"Invalid client ID: {client_id}")
        return provider

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        client_info.scope = " ".join(ThirdPartyOAuth.get_scopes())
        return await super().register_client(client_info)

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        if not ThirdPartyOAuth.has_providers(get_server_origin()):
            raise ValueError("No third party OAuth providers configured")

        # strip scopes to use default scopes
        client.scope = None
        params.scopes = []

        provider_urls: list[str] = []
        for provider_name, provider in ThirdPartyOAuth.get_providers_for_origin(
            get_server_origin()
        ).items():
            url = await provider.authorize(client, params)
            provider_urls.append(f"{provider_name}_url={quote(url)}")

        return f"/signin?{'&'.join(provider_urls)}"

    async def _handle_idp_callback(self, request: Request) -> RedirectResponse:
        txn_id = request.query_params.get("state")
        if not txn_id:
            raise ValueError("IdP callback missing transaction ID")

        txn = None
        provider = None

        # Check all providers for the transaction
        for _provider in ThirdPartyOAuth.get_providers_for_origin(get_server_origin()).values():
            if transaction := _provider._oauth_transactions.get(txn_id):
                txn = transaction
                provider = _provider
                break

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
