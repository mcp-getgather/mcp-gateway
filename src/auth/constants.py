from typing import Literal, get_args

THIRD_PARTY_OAUTH_PROVIDER_NAME = Literal["github", "google"]

OAUTH_PROVIDER_NAME = THIRD_PARTY_OAUTH_PROVIDER_NAME | Literal["getgather"]
OAUTH_PROVIDERS = list(get_args(OAUTH_PROVIDER_NAME))

OAUTH_SCOPES = ["getgather_user_scope"]  # dummy scope to make scope validation work
