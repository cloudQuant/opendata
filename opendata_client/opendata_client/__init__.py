"""Minimal Python client for the opendata REST API (design §10.4)."""

from opendata_client.client import (
    DEFAULT_PAGE_SIZE,
    AuthenticationError,
    InvalidQueryError,
    NotFoundError,
    OpendataClient,
    OpendataClientError,
    Page,
    PermissionDeniedError,
    UnsupportedQueryError,
)

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "AuthenticationError",
    "InvalidQueryError",
    "NotFoundError",
    "OpendataClient",
    "OpendataClientError",
    "Page",
    "PermissionDeniedError",
    "UnsupportedQueryError",
]
