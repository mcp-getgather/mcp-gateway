from typing import Literal, get_args

# first party oauth providers: getgather, getgather-persistent
GETGATHER_OAUTH_PROVIDER_NAME = "getgather"
GETGATHER_PERSISTENT_OAUTH_PROVIDER_NAME = "getgather-persistent"

# third party oauth providers: github, google
THIRD_PARTY_OAUTH_PROVIDER_NAME = Literal["github", "google"]

OAUTH_PROVIDER_NAME = (
    THIRD_PARTY_OAUTH_PROVIDER_NAME | Literal["getgather"] | Literal["getgather-persistent"]
)
OAUTH_PROVIDERS = list(get_args(OAUTH_PROVIDER_NAME))

OAUTH_SCOPES = ["getgather_user_scope"]  # dummy scope to make scope validation work
