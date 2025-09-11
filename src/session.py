import random
from collections import OrderedDict

from nanoid import generate

FRIENDLY_CHARS: str = "23456789abcdefghijkmnpqrstuvwxyz"

_store: OrderedDict[str, str] = OrderedDict()  # session_id -> server_host
STORE_CAPACITY: int = 1000


class SessionManager:
    def _new_session_id(self) -> str:
        return generate(size=6, alphabet=FRIENDLY_CHARS)

    def create(self, server_host: str) -> str:
        session_id = self._new_session_id()
        _store[session_id] = server_host
        if len(_store) > STORE_CAPACITY:
            _store.popitem(last=False)
        return session_id

    def get(self, session_id: str) -> str:
        return _store[session_id]

    def pick_random(self) -> str:
        return random.choice(list(_store.values()))


session_manager = SessionManager()
