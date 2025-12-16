from collections import defaultdict
from functools import cache

from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.auth.providers.google import GoogleProvider

from src.auth.constants import THIRD_PARTY_OAUTH_PROVIDER_NAME
from src.settings import settings


class ThirdPartyOAuth:
    @classmethod
    def get_provider(
        cls, server_origin: str, provider_name: THIRD_PARTY_OAUTH_PROVIDER_NAME
    ) -> OAuthProxy | None:
        providers = cls.get_available_providers()
        return providers.get(server_origin, {}).get(provider_name)

    @classmethod
    def get_providers_for_origin(
        cls,
        server_origin: str,
    ) -> dict[THIRD_PARTY_OAUTH_PROVIDER_NAME, OAuthProxy]:
        providers = cls.get_available_providers()
        return providers.get(server_origin, {})

    @classmethod
    def get_provider_for_name(
        cls, provider_name: THIRD_PARTY_OAUTH_PROVIDER_NAME
    ) -> OAuthProxy | None:
        providers = cls.get_available_providers()
        for _, providers in providers.items():
            for name, provider in providers.items():
                if name == provider_name:
                    return provider
        return None

    @classmethod
    def get_scopes(
        cls,
        provider_name: THIRD_PARTY_OAUTH_PROVIDER_NAME | None = None,
    ) -> list[str]:
        github_scopes = ["user"]
        google_scopes = [
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/userinfo.profile",
        ]
        if not provider_name:
            return github_scopes + google_scopes

        if provider_name == "github":
            return github_scopes
        elif provider_name == "google":
            return google_scopes
        else:
            raise ValueError(f"Invalid provider: {provider}")

    @classmethod
    def has_providers(cls, server_origin: str) -> bool:
        return server_origin in cls.get_available_providers()

    @classmethod
    @cache
    def get_available_providers(
        cls,
    ) -> dict[str, dict[THIRD_PARTY_OAUTH_PROVIDER_NAME, OAuthProxy]]:
        """
        Return all available OAuth providers, in the format of:
        {
            <server_origin>: {
                <provider_name>: provider,
            }
        }
        """
        providers: dict[str, dict[THIRD_PARTY_OAUTH_PROVIDER_NAME, OAuthProxy]] = defaultdict(dict)

        for server_config in settings.SERVER_CONFIGS:
            if not server_config.oauth_providers:
                continue

            for provider_config in server_config.oauth_providers:
                if provider_config.name == "github":
                    providers[server_config.origin]["github"] = GitHubProvider(
                        client_id=provider_config.client_id,
                        client_secret=provider_config.client_secret,
                        base_url=server_config.origin,
                        required_scopes=cls.get_scopes("github"),
                    )
                elif provider_config.name == "google":
                    providers[server_config.origin]["google"] = GoogleProvider(
                        client_id=provider_config.client_id,
                        client_secret=provider_config.client_secret,
                        base_url=server_config.origin,
                        required_scopes=cls.get_scopes("google"),
                    )
                else:
                    raise ValueError(f"Invalid provider: {provider_config.name}")

        return providers
