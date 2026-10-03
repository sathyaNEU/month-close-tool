"""Where the Postgres password comes from.

Locally it is a static password from ``PGPASSWORD``. On Databricks, Lakebase uses short-lived OAuth
tokens (about one hour, checked only at login), so :class:`LakebaseOAuthProvider` keeps a cached token
and refreshes it in the background well before it expires. A refresh never blocks a request, and an
already-open connection keeps working after its token expires.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


class CredentialError(RuntimeError):
    """Raised when no usable Postgres credential is available."""


@runtime_checkable
class PgCredentialProvider(Protocol):
    def password(self) -> str:
        """Current password or token (cached; never blocks on a network call once warmed up)."""
        ...

    def refresh_if_needed(self) -> None:
        """Refresh the credential if it expires soon (called from a background thread)."""
        ...


def running_in_databricks_app(environ: Mapping[str, str] = os.environ) -> bool:
    return "DATABRICKS_APP_NAME" in environ


@dataclass(frozen=True)
class StaticCredentialProvider:
    """A fixed password for local development. Refuses to run inside a Databricks App."""

    secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if running_in_databricks_app():
            raise CredentialError("static Postgres credentials are for local development only")
        if not self.secret:
            raise CredentialError("empty Postgres password")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ, var: str = "PGPASSWORD") -> StaticCredentialProvider:
        try:
            return cls(environ[var])
        except KeyError:
            raise CredentialError(f"{var} is not set") from None

    def password(self) -> str:
        return self.secret

    def refresh_if_needed(self) -> None:
        return None


TokenMinter = Callable[[], tuple[str, datetime]]
"""Returns (token, expires_at). Stage 10 supplies the real Lakebase minting call."""


class LakebaseOAuthProvider:
    """Caches a Lakebase OAuth token and refreshes it before expiry."""

    def __init__(
        self,
        mint: TokenMinter,
        *,
        refresh_margin: timedelta = timedelta(minutes=10),
        clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        self._mint = mint
        self._margin = refresh_margin
        self._clock = clock
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: datetime | None = None

    def _mint_locked(self) -> None:
        token, expires_at = self._mint()
        if not token:
            raise CredentialError("token minter returned an empty token")
        if expires_at.tzinfo is None:
            raise CredentialError("token expiry must be timezone-aware")
        self._token, self._expires_at = token, expires_at

    def password(self) -> str:
        with self._lock:
            if self._token is None:
                self._mint_locked()
            if self._token is None:
                raise CredentialError("token mint returned nothing")
            return self._token

    def refresh_if_needed(self) -> None:
        with self._lock:
            if self._token is None or self._expires_at is None or self._expires_at - self._clock() <= self._margin:
                self._mint_locked()

    @property
    def expires_at(self) -> datetime | None:
        return self._expires_at
