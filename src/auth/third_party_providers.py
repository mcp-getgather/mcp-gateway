from functools import cache

from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.auth.providers.google import GoogleProvider

from src.auth.constants import THIRD_PARTY_OAUTH_PROVIDER_NAME
from src.settings import settings


@cache
def get_available_providers() -> dict[THIRD_PARTY_OAUTH_PROVIDER_NAME, OAuthProxy]:
    providers: dict[THIRD_PARTY_OAUTH_PROVIDER_NAME, OAuthProxy] = {}

    if settings.OAUTH_GITHUB_CLIENT_ID and settings.OAUTH_GITHUB_CLIENT_SECRET:
        providers["github"] = GitHubProvider(
            client_id=settings.OAUTH_GITHUB_CLIENT_ID,
            client_secret=settings.OAUTH_GITHUB_CLIENT_SECRET,
            base_url=settings.GATEWAY_ORIGIN,
            required_scopes=get_provider_scopes("github"),
        )

    if settings.OAUTH_GOOGLE_CLIENT_ID and settings.OAUTH_GOOGLE_CLIENT_SECRET:
        providers["google"] = GoogleProvider(
            client_id=settings.OAUTH_GOOGLE_CLIENT_ID,
            client_secret=settings.OAUTH_GOOGLE_CLIENT_SECRET,
            base_url=settings.GATEWAY_ORIGIN,
            required_scopes=get_provider_scopes("google"),
        )

    return providers


def get_provider_scopes(
    provider: THIRD_PARTY_OAUTH_PROVIDER_NAME | None = None,
) -> list[str]:
    github_scopes = ["user"]
    google_scopes = [
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
    ]
    if not provider:
        return github_scopes + google_scopes

    if provider == "github":
        return github_scopes
    elif provider == "google":
        return google_scopes
    else:
        raise ValueError(f"Invalid provider: {provider}")
