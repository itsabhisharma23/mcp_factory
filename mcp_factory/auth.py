"""
Enterprise Authentication & Security Manager for MCPFactory.

Handles:
1. Outbound Authentication to Backend Services (10 mechanisms):
   - `none`                      : Unauthenticated public endpoints
   - `api_key`                   : Header (e.g., X-API-Key) or URL query param
   - `bearer`                    : Static Bearer token
   - `basic`                     : HTTP Basic Auth (RFC 7617)
   - `custom_headers`            : Multi-header auth (e.g., Datadog, Cloudflare, Algolia)
   - `oauth2_client_credentials` : OAuth2 M2M flow (supports client_secret_post & client_secret_basic)
   - `oauth2_refresh_token`      : OAuth2 user-delegated refresh_token exchange & auto-rotation
   - `gcp_id_token`              : Google Cloud OIDC ID token (Cloud Run -> private Cloud Run)
   - `gcp_access_token`          : Google Cloud IAM token via ADC (BigQuery, Vertex AI, GCS)
   - `hmac_sha256`               : Cryptographic HMAC-SHA256 request signing with timestamp header
   - `passthrough`               : Zero-Trust caller token delegation (forwards client's own token)

2. Concurrency & Event-Loop Safety:
   - Uses `asyncio.Lock` double-checked locking to prevent "thundering herd" token stampedes.
   - Offloads blocking Google Cloud metadata/ADC calls via `asyncio.to_thread`.

3. Inbound Authentication Guard for Cloud Run:
   - Multi-key rotatable API keys (constant-time `hmac.compare_digest`)
   - RFC 7519 JWT (HS256) verification (`exp`, `nbf`, `iss`, `aud`, `required_scopes`)
   - Google Cloud OIDC ID token verification (`audience`, `allowed_emails`, `allowed_domains`)

4. Sensitive Data Redaction:
   - Automatically masks secrets, tokens, passwords, and API keys before logging.
"""

from __future__ import annotations

import asyncio
import base64
from contextvars import ContextVar
import hashlib
import hmac
import json
import logging
import os
import re
import time
from typing import Any, Optional, Tuple

import httpx

from mcp_factory.schema import AuthType, BackendAuthConfig, InboundAuthConfig, ServerMetadataConfig

logger = logging.getLogger("mcp_factory.auth")

# ContextVar holding the current inbound HTTP request's headers (lowercase keys)
# Used by `AuthType.PASSTHROUGH` and custom plugins for Zero-Trust user delegation.
current_request_headers: ContextVar[dict[str, str]] = ContextVar(
    "current_request_headers", default={}
)

SENSITIVE_KEY_PATTERN = re.compile(
    r"(password|passwd|secret|token|api[-_]?key|authorization|credential|private[-_]?key|access[-_]?key)",
    re.IGNORECASE,
)


def redact_sensitive_data(data: Any) -> Any:
    """
    Recursively masks sensitive fields in dictionaries and lists before logging.
    """
    if isinstance(data, dict):
        redacted: dict[str, Any] = {}
        for k, v in data.items():
            if SENSITIVE_KEY_PATTERN.search(str(k)):
                redacted[k] = "***REDACTED***"
            else:
                redacted[k] = redact_sensitive_data(v)
        return redacted
    elif isinstance(data, list):
        return [redact_sensitive_data(item) for item in data]
    return data


class BackendAuthenticator:
    """
    Manages outbound authentication headers and query parameters for a configured backend.
    Thread-safe and coroutine-safe via double-checked `asyncio.Lock`.
    """

    def __init__(self, auth_config: BackendAuthConfig, base_url: Optional[str] = None) -> None:
        self.config = auth_config
        self.base_url = base_url
        self._cached_token: Optional[str] = None
        self._token_expiry: float = 0.0
        self._refresh_token: Optional[str] = auth_config.refresh_token
        self._lock = asyncio.Lock()

    async def apply_auth(
        self,
        headers: dict[str, str],
        query_params: dict[str, str],
        body_bytes: Optional[bytes] = None,
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

        elif auth_type == AuthType.CUSTOM_HEADERS:
            for h_name, h_val in self.config.custom_headers.items():
                if h_val:
                    headers[h_name] = h_val

        elif auth_type == AuthType.OAUTH2_CLIENT_CREDENTIALS:
            token = await self._get_oauth2_token(grant_type="client_credentials")
            headers["Authorization"] = f"Bearer {token}"

        elif auth_type == AuthType.OAUTH2_REFRESH_TOKEN:
            token = await self._get_oauth2_token(grant_type="refresh_token")
            headers["Authorization"] = f"Bearer {token}"

        elif auth_type == AuthType.GCP_ID_TOKEN:
            token = await self._get_gcp_id_token()
            headers["Authorization"] = f"Bearer {token}"

        elif auth_type == AuthType.GCP_ACCESS_TOKEN:
            token = await self._get_gcp_access_token()
            headers["Authorization"] = f"Bearer {token}"

        elif auth_type == AuthType.HMAC_SHA256:
            secret = (self.config.hmac_secret or self.config.token or "").encode("utf-8")
            if not secret:
                raise ValueError("HMAC_SHA256 auth requires 'hmac_secret' or 'token'.")
            timestamp = str(int(time.time()))
            payload_bytes = body_bytes or b""
            signing_input = timestamp.encode("ascii") + b"." + payload_bytes
            signature = hmac.new(secret, signing_input, hashlib.sha256).hexdigest()
            headers[self.config.timestamp_header] = timestamp
            headers[self.config.header_name] = f"sha256={signature}"

        elif auth_type == AuthType.PASSTHROUGH:
            inbound_headers = current_request_headers.get()
            src_key = self.config.source_header.lower()
            caller_token = inbound_headers.get(src_key)
            if caller_token:
                headers[self.config.target_header] = caller_token
            elif self.config.token:
                # Fallback token if configured and no inbound header present (e.g. local CLI testing)
                headers[self.config.target_header] = (
                    self.config.token
                    if self.config.target_header.lower() != "authorization"
                    or self.config.token.lower().startswith(("bearer ", "basic "))
                    else f"Bearer {self.config.token}"
                )
            else:
                raise PermissionError(
                    f"Passthrough authentication requires inbound header '{self.config.source_header}'."
                )

        return headers, query_params

    async def _get_oauth2_token(self, grant_type: str = "client_credentials") -> str:
        """
        Fetches and caches an OAuth2 access token using `client_credentials` or `refresh_token`.
        Protected by an `asyncio.Lock` to prevent thundering-herd refreshes under high concurrency.
        """
        now = time.time()
        if self._cached_token and now < self._token_expiry - 60:
            return self._cached_token

        async with self._lock:
            now = time.time()
            if self._cached_token and now < self._token_expiry - 60:
                return self._cached_token

            if not self.config.token_url:
                raise ValueError(f"OAuth2 ({grant_type}) auth requires 'token_url'.")

            payload: dict[str, str] = {"grant_type": grant_type, **self.config.extra_token_params}
            req_headers: dict[str, str] = {"Accept": "application/json"}

            if self.config.client_auth_method == "client_secret_basic":
                raw_cred = f"{self.config.client_id or ''}:{self.config.client_secret or ''}".encode("utf-8")
                req_headers["Authorization"] = f"Basic {base64.b64encode(raw_cred).decode('ascii')}"
            else:
                if self.config.client_id:
                    payload["client_id"] = self.config.client_id
                if self.config.client_secret:
                    payload["client_secret"] = self.config.client_secret

            if grant_type == "refresh_token":
                active_refresh = self._refresh_token or self.config.refresh_token
                if not active_refresh:
                    raise ValueError("OAuth2 refresh_token flow requires 'refresh_token'.")
                payload["refresh_token"] = active_refresh

            if self.config.scopes:
                payload["scope"] = " ".join(self.config.scopes)

            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(self.config.token_url, data=payload, headers=req_headers)
                resp.raise_for_status()
                data = resp.json()

            access_token = data.get("access_token")
            if not access_token:
                raise RuntimeError("OAuth2 token endpoint did not return 'access_token'.")

            # Support automatic refresh_token rotation if the OAuth server issues a new refresh_token
            if "refresh_token" in data and data["refresh_token"]:
                self._refresh_token = data["refresh_token"]

            expires_in = float(data.get("expires_in", 3600))
            self._cached_token = access_token
            self._token_expiry = now + expires_in
            return access_token

    async def _get_gcp_id_token(self) -> str:
        """
        Fetches a Google Cloud OIDC ID token for service-to-service auth
        (e.g., Cloud Run calling another private Cloud Run service).
        Offloaded to a background thread so blocking metadata HTTP calls never stall the event loop.
        """
        now = time.time()
        if self._cached_token and now < self._token_expiry - 60:
            return self._cached_token

        async with self._lock:
            now = time.time()
            if self._cached_token and now < self._token_expiry - 60:
                return self._cached_token

            audience = self.config.audience or self.base_url
            if not audience:
                raise ValueError("GCP_ID_TOKEN auth requires 'audience' or backend 'base_url'.")

            def _fetch_sync() -> str:
                import google.auth.transport.requests
                import google.oauth2.id_token

                auth_req = google.auth.transport.requests.Request()
                return google.oauth2.id_token.fetch_id_token(auth_req, audience)

            token = await asyncio.to_thread(_fetch_sync)
            self._cached_token = token
            self._token_expiry = now + 3000  # GCP ID tokens are valid for 1 hour
            return token

    async def _get_gcp_access_token(self) -> str:
        """
        Fetches a Google Cloud IAM OAuth2 access token using Application Default Credentials (ADC).
        Offloaded to a worker thread via `asyncio.to_thread`.
        """
        now = time.time()
        if self._cached_token and now < self._token_expiry - 60:
            return self._cached_token

        async with self._lock:
            now = time.time()
            if self._cached_token and now < self._token_expiry - 60:
                return self._cached_token

            scopes = self.config.scopes or ["https://www.googleapis.com/auth/cloud-platform"]

            def _refresh_sync() -> str:
                import google.auth
                import google.auth.transport.requests

                credentials, _ = google.auth.default(scopes=scopes)
                auth_req = google.auth.transport.requests.Request()
                credentials.refresh(auth_req)
                if not credentials.token:
                    raise RuntimeError("Failed to obtain GCP access token from ADC.")
                return credentials.token

            token = await asyncio.to_thread(_refresh_sync)
            self._cached_token = token
            self._token_expiry = now + 3000
            return token


# ============================================================================
# Inbound Authentication & JWT / OIDC Verifiers (Client -> MCP Server)
# ============================================================================


def _b64url_decode(segment: str) -> bytes:
    """Decodes a Base64URL-encoded JWT segment without padding errors."""
    rem = len(segment) % 4
    if rem > 0:
        segment += "=" * (4 - rem)
    return base64.urlsafe_b64decode(segment.encode("ascii"))


def verify_jwt_hs256(
    token: str,
    secret: str,
    expected_issuer: Optional[str] = None,
    expected_audience: Optional[str] = None,
    required_scopes: Optional[list[str]] = None,
) -> Tuple[bool, Optional[str], Optional[dict[str, Any]]]:
    """
    Verifies an RFC 7519 HS256 JWT token in pure Python with constant-time signature comparison.
    Checks signature, `exp`, `nbf`, `iss`, `aud`, and `required_scopes`.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return False, "Malformed JWT token (expected 3 segments).", None

    header_b64, payload_b64, signature_b64 = parts
    try:
        header = json.loads(_b64url_decode(header_b64).decode("utf-8"))
        claims = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        provided_sig = _b64url_decode(signature_b64)
    except Exception:
        return False, "Malformed JWT base64/JSON encoding.", None

    if header.get("alg") != "HS256":
        return False, f"Unsupported JWT algorithm '{header.get('alg')}'. Expected 'HS256'.", None

    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    expected_sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()

    if not hmac.compare_digest(provided_sig, expected_sig):
        return False, "Invalid JWT signature.", None

    now = time.time()
    if "exp" in claims and float(claims["exp"]) < now:
        return False, "JWT token has expired.", None

    if "nbf" in claims and float(claims["nbf"]) > now + 30:
        return False, "JWT token is not yet valid (nbf).", None

    if expected_issuer and claims.get("iss") != expected_issuer:
        return False, f"Invalid JWT issuer '{claims.get('iss')}'.", None

    if expected_audience:
        aud = claims.get("aud")
        aud_list = [aud] if isinstance(aud, str) else (aud if isinstance(aud, list) else [])
        if expected_audience not in aud_list:
            return False, f"Invalid JWT audience '{aud}'.", None

    if required_scopes:
        raw_scopes = claims.get("scope") or claims.get("scp") or ""
        token_scopes = set(raw_scopes.split()) if isinstance(raw_scopes, str) else set(raw_scopes)
        missing = [s for s in required_scopes if s not in token_scopes]
        if missing:
            return False, f"Missing required JWT scopes: {missing}", None

    return True, None, claims


def verify_inbound_api_key(
    headers: dict[str, str],
    env_var_name: Optional[str] = "MCP_SERVER_API_KEY",
) -> Tuple[bool, Optional[str]]:
    """
    Verifies inbound client requests against an API key (or comma-separated list of rotatable keys)
    configured in the environment. Uses constant-time `hmac.compare_digest` across all keys.
    """
    if not env_var_name:
        return True, None

    raw_expected = os.environ.get(env_var_name, "").strip()
    if not raw_expected:
        # Inbound API key protection is disabled when the env var is empty
        return True, None

    valid_keys = [k.strip() for k in raw_expected.split(",") if k.strip()]
    if not valid_keys:
        return True, None

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

    # Evaluate against all rotatable keys without short-circuit timing leaks
    matched = False
    for candidate in valid_keys:
        if hmac.compare_digest(provided_key, candidate):
            matched = True

    if not matched:
        return False, "Invalid API Key."

    return True, None


def verify_inbound_request(
    headers: dict[str, str],
    server_config: ServerMetadataConfig,
) -> Tuple[bool, Optional[str]]:
    """
    Master inbound authentication dispatcher supporting:
    - Multi-key rotatable API keys (`api_key`)
    - JWT HS256 verification (`jwt_hs256`)
    - Google Cloud OIDC ID token verification (`google_oidc`)
    """
    inbound_cfg: Optional[InboundAuthConfig] = server_config.inbound_auth
    lower_headers = {k.lower(): v for k, v in headers.items()}

    if inbound_cfg is None or inbound_cfg.type == "api_key":
        env_var = inbound_cfg.api_key_env if inbound_cfg else server_config.require_api_key_env
        return verify_inbound_api_key(headers, env_var_name=env_var)

    if inbound_cfg.type == "none":
        return True, None

    # Extract Bearer token for JWT or Google OIDC
    auth_header = lower_headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return False, "Missing 'Authorization: Bearer <token>' header."
    bearer_token = auth_header[7:].strip()

    if inbound_cfg.type == "jwt_hs256":
        secret = os.environ.get(inbound_cfg.jwt_secret_env, "").strip()
        if not secret:
            logger.error("JWT auth enabled but env var '%s' is not set!", inbound_cfg.jwt_secret_env)
            return False, "Server authentication configuration error."
        ok, err, _ = verify_jwt_hs256(
            bearer_token,
            secret=secret,
            expected_issuer=inbound_cfg.issuer,
            expected_audience=inbound_cfg.audience,
            required_scopes=inbound_cfg.required_scopes,
        )
        return ok, err

    if inbound_cfg.type == "google_oidc":
        try:
            import google.auth.transport.requests
            import google.oauth2.id_token

            auth_req = google.auth.transport.requests.Request()
            effective_audience = inbound_cfg.audience.strip() if inbound_cfg.audience else None
            claims = google.oauth2.id_token.verify_oauth2_token(
                bearer_token,
                auth_req,
                audience=effective_audience or None,
            )
            email = claims.get("email", "")
            if inbound_cfg.allowed_emails and email not in inbound_cfg.allowed_emails:
                return False, f"Caller '{email}' is not in allowed_emails."
            if inbound_cfg.allowed_domains:
                domain = email.split("@")[-1] if "@" in email else claims.get("hd", "")
                if domain not in inbound_cfg.allowed_domains:
                    return False, f"Caller domain '{domain}' is not in allowed_domains."
            return True, None
        except Exception as exc:
            return False, f"Google OIDC token verification failed: {exc}"

    return False, f"Unsupported inbound_auth type: {inbound_cfg.type}"
