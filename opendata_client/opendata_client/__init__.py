"""Minimal Python client for the opendata REST API (design §10.4)."""

from opendata_client.client import (
    DEFAULT_PAGE_SIZE,
    DEFAULT_WS_TIMEOUT,
    AuthenticationError,
    DataUpdate,
    InvalidQueryError,
    NotFoundError,
    OpendataClient,
    OpendataClientError,
    Page,
    PermissionDeniedError,
    SubscriptionError,
    SubscriptionSession,
    UnsupportedQueryError,
)

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_WS_TIMEOUT",
    "AuthenticationError",
    "DataUpdate",
    "InvalidQueryError",
    "NotFoundError",
    "OpendataClient",
    "OpendataClientError",
    "Page",
    "PermissionDeniedError",
    "SubscriptionError",
    "SubscriptionSession",
    "UnsupportedQueryError",
]
