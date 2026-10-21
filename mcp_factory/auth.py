"""
Authentication Manager for MCPFactory.

Handles:
1. Outbound authentication to backend services:
   - API Key (Header or Query Param)
   - Bearer Token
   - HTTP Basic Auth
   - OAuth2 Client Credentials (with automatic in-memory token caching & refresh)
   - GCP OIDC ID Token (for calling private Cloud Run / Cloud Functions services)
   - GCP IAM Access Token (for calling Google Cloud APIs like BigQuery, Vertex AI, GCS directly via REST)
2. Inbound authentication guard for securing the MCP server on Cloud Run.
"""

from __future__ import annotations

import base64
import hmac
import logging
import os
import time
from typing import Optional, Tuple

import httpx

from mcp_factory.schema import AuthType, BackendAuthConfig

logger = logging.getLogger("mcp_factory.auth")


class BackendAuthenticator:
    """Manages outbound authentication headers and query parameters for a configured backend."""

    def __init__(self, auth_config: BackendAuthConfig, base_url: Optional[str] = None) -> None:
        self.config = auth_config
        self.base_url = base_url
        self._cached_token: Optional[str] = None
        self._token_expiry: float = 0.0

    async def apply_auth(
        self,
        headers: dict[str, str],
        query_params: dict[str, str],
    ) -> Tuple[dict[str, str], dict[str, str]]:
        """
        Mutates and returns `(headers, query_params)` with the configured authentication applied.
        """
        auth_type = self.config.type

        if auth_type == AuthType.NONE:
            return headers, query_params

        elif auth_type == AuthType.API_KEY:
            token = self.config.token or ""
            if not token:
                logger.warning("API_KEY auth configured but token is empty.")
            elif self.config.query_param:
                query_params[self.config.query_param] = token
            else:
                headers[self.config.header_name] = token

        elif auth_type == AuthType.BEARER:
            token = self.config.token or ""
            if not token:
                logger.warning("BEARER auth configured but token is empty.")
            else:
                headers["Authorization"] = f"Bearer {token}"

        elif auth_type == AuthType.BASIC:
            username = self.config.username or ""
            password = self.config.password or ""
            raw = f"{username}:{password}".encode("utf-8")
            encoded = base64.b64encode(raw).decode("ascii")
            headers["Authorization"] = f"Basic {encoded}"

        elif auth_type == AuthType.OAUTH2_CLIENT_CREDENTIALS:
            token = await self._get_oauth2_token()
            headers["Authorization"] = f"Bearer {token}"

        elif auth_type == AuthType.GCP_ID_TOKEN:
            token = self._get_gcp_id_token()
            headers["Authorization"] = f"Bearer {token}"

        elif auth_type == AuthType.GCP_ACCESS_TOKEN:
            token = self._get_gcp_access_token()
            headers["Authorization"] = f"Bearer {token}"

        return headers, query_params

    async def _get_oauth2_token(self) -> str:
        """Fetches and caches an OAuth2 access token using the client_credentials grant."""
        now = time.time()
        if self._cached_token and now < self._token_expiry - 60:
            return self._cached_token

        if not self.config.token_url:
            raise ValueError("OAuth2 Client Credentials auth requires 'token_url'.")

        payload: dict[str, str] = {
            "grant_type": "client_credentials",
            "client_id": self.config.client_id or "",
            "client_secret": self.config.client_secret or "",
        }
        if self.config.scopes:
            payload["scope"] = " ".join(self.config.scopes)

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(self.config.token_url, data=payload)
            resp.raise_for_status()
            data = resp.json()

        access_token = data.get("access_token")
        if not access_token:
            raise RuntimeError(f"OAuth2 token endpoint did not return 'access_token': {data}")

        expires_in = float(data.get("expires_in", 3600))
        self._cached_token = access_token
        self._token_expiry = now + expires_in
        return access_token

    def _get_gcp_id_token(self) -> str:
        """
        Fetches a Google Cloud OIDC ID token for service-to-service auth
        (e.g., Cloud Run calling another private Cloud Run service).
        """
        now = time.time()
        if self._cached_token and now < self._token_expiry - 60:
            return self._cached_token

        audience = self.config.audience or self.base_url
        if not audience:
            raise ValueError("GCP_ID_TOKEN auth requires 'audience' or backend 'base_url'.")

        import google.auth.transport.requests
        import google.oauth2.id_token

        auth_req = google.auth.transport.requests.Request()
        token = google.oauth2.id_token.fetch_id_token(auth_req, audience)
        self._cached_token = token
        self._token_expiry = now + 3000  # GCP ID tokens are valid for 1 hour
        return token

    def _get_gcp_access_token(self) -> str:
        """
        Fetches a Google Cloud IAM OAuth2 access token using Application Default Credentials (ADC).
        Ideal for calling Google Cloud REST APIs (BigQuery, Vertex AI, GCS, Cloud Run Admin) directly.
        """
        now = time.time()
        if self._cached_token and now < self._token_expiry - 60:
            return self._cached_token

        import google.auth
        import google.auth.transport.requests

        scopes = self.config.scopes or ["https://www.googleapis.com/auth/cloud-platform"]
        credentials, _ = google.auth.default(scopes=scopes)
        auth_req = google.auth.transport.requests.Request()
        credentials.refresh(auth_req)

        if not credentials.token:
            raise RuntimeError("Failed to obtain GCP access token from Application Default Credentials.")

        self._cached_token = credentials.token
        self._token_expiry = now + 3000
        return credentials.token


def verify_inbound_api_key(
    headers: dict[str, str],
    env_var_name: Optional[str] = "MCP_SERVER_API_KEY",
) -> Tuple[bool, Optional[str]]:
    """
    Verifies inbound client requests against an optional API key configured in the environment.
    If the environment variable is unset or empty, authentication is bypassed (returns True).
    """
    if not env_var_name:
        return True, None

    expected_key = os.environ.get(env_var_name, "").strip()
    if not expected_key:
        # Inbound API key protection is disabled when the env var is empty
        return True, None

    # Normalize header keys to lowercase
    lower_headers = {k.lower(): v for k, v in headers.items()}

    # Check X-MCP-API-Key or X-API-Key
    provided_key = lower_headers.get("x-mcp-api-key") or lower_headers.get("x-api-key")

    # Or Authorization: Bearer <key>
    if not provided_key:
        auth_header = lower_headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            provided_key = auth_header[7:].strip()

    if not provided_key:
        return False, "Missing API Key. Supply 'Authorization: Bearer <key>' or 'X-MCP-API-Key: <key>'."

    if not hmac.compare_digest(provided_key, expected_key):
        return False, "Invalid API Key."

    return True, None
