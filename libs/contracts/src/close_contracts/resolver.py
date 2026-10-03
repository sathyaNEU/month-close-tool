"""Resolve logical tables to physical identifiers for the current environment.

Two implementations share one interface:

* :class:`PathTableResolver` (local default): Delta tables addressed by storage path, for example
  ``delta.`s3a://close-lake/local/match/items``` on the S3-compatible store. No catalog server needed.
* :class:`CatalogTableResolver` (Databricks, or the local ``+uc`` profile): Unity Catalog three-level
  names such as ```close_prod`.`match`.`items````.

The returned string is always passed to Spark as the value of an ``IDENTIFIER(:t)`` parameter, never
concatenated into SQL text, and every component is validated here so a resolver can never emit
anything but a well-formed identifier.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable

from close_contracts.tables import LogicalTable

_ALLOWED_SCHEMES: Final = ("s3a://", "s3://", "file:/")
_ROOT_RE: Final = re.compile(r"^[A-Za-z0-9_\-./:]+$")
_CATALOG_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_SOURCE_KEY_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_PART_RE: Final = re.compile(r"^[A-Za-z0-9_\-.=]+$")


class ResolverError(ValueError):
    """Raised for an invalid root, catalog, source key or path component."""


@runtime_checkable
class TableResolver(Protocol):
    """Maps logical tables (and configured landing sources) to physical identifiers."""

    def table(self, t: LogicalTable) -> str:
        """Identifier to bind to ``IDENTIFIER(:t)`` for a platform table."""
        ...

    def landing(self, source_key: str) -> str:
        """Identifier of a configured, source-owned landing table."""
        ...

    def evidence_uri(self, *parts: str) -> str:
        """Storage URI for run evidence (config snapshots, command batches, reports)."""
        ...


def _check_root(root: str, what: str) -> str:
    if not root.startswith(_ALLOWED_SCHEMES) or not _ROOT_RE.fullmatch(root) or "`" in root:
        raise ResolverError(f"invalid {what} {root!r}: expected s3a://, s3:// or file:/ URI of safe characters")
    return root.rstrip("/")


def _check_parts(parts: tuple[str, ...]) -> tuple[str, ...]:
    if not parts:
        raise ResolverError("evidence_uri needs at least one path component")
    for p in parts:
        if not _PART_RE.fullmatch(p) or p in {".", ".."}:
            raise ResolverError(f"invalid path component {p!r}")
    return parts


@dataclass(frozen=True)
class PathTableResolver:
    """Delta tables addressed by path under ``lake_root`` (local default)."""

    lake_root: str
    landing_tables: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    evidence_root: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "lake_root", _check_root(self.lake_root, "lake root"))
        if self.evidence_root is not None:
            object.__setattr__(self, "evidence_root", _check_root(self.evidence_root, "evidence root"))
        for key, location in self.landing_tables.items():
            if not _SOURCE_KEY_RE.fullmatch(key):
                raise ResolverError(f"invalid landing source key {key!r}")
            _check_root(location, f"landing location for {key!r}")
        object.__setattr__(self, "landing_tables", MappingProxyType(dict(self.landing_tables)))

    def location(self, t: LogicalTable) -> str:
        """Storage location of a platform table (used by DataFrame readers and writers)."""
        return f"{self.lake_root}/{t.schema.value}/{t.name}"

    def table(self, t: LogicalTable) -> str:
        return f"delta.`{self.location(t)}`"

    def landing(self, source_key: str) -> str:
        try:
            location = self.landing_tables[source_key]
        except KeyError:
            raise ResolverError(f"landing source {source_key!r} is not configured") from None
        return f"delta.`{location.rstrip('/')}`"

    def evidence_uri(self, *parts: str) -> str:
        root = self.evidence_root or f"{self.lake_root}/_evidence"
        return "/".join((root, *_check_parts(parts)))


@dataclass(frozen=True)
class CatalogTableResolver:
    """Unity Catalog three-level names (Databricks, or the local ``+uc`` profile)."""

    catalog: str
    evidence_root: str
    landing_tables: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not _CATALOG_RE.fullmatch(self.catalog):
            raise ResolverError(f"invalid catalog name {self.catalog!r}")
        object.__setattr__(self, "evidence_root", _check_root(self.evidence_root, "evidence root"))
        fqn_re = re.compile(r"^[a-z][a-z0-9_]{0,62}\.[a-z][a-z0-9_]{0,62}\.[a-z][a-z0-9_]{0,62}$")
        for key, fqn in self.landing_tables.items():
            if not _SOURCE_KEY_RE.fullmatch(key) or not fqn_re.fullmatch(fqn):
                raise ResolverError(f"invalid landing mapping {key!r} -> {fqn!r}")
        object.__setattr__(self, "landing_tables", MappingProxyType(dict(self.landing_tables)))

    def table(self, t: LogicalTable) -> str:
        return f"`{self.catalog}`.`{t.schema.value}`.`{t.name}`"

    def landing(self, source_key: str) -> str:
        try:
            catalog, schema, name = self.landing_tables[source_key].split(".")
        except KeyError:
            raise ResolverError(f"landing source {source_key!r} is not configured") from None
        return f"`{catalog}`.`{schema}`.`{name}`"

    def evidence_uri(self, *parts: str) -> str:
        return "/".join((self.evidence_root, *_check_parts(parts)))
