"""Secure Secret Management via Google Cloud Secret Manager (ADC).

Ensures zero hardcoded API keys, database URLs, or signing secrets exist in
source code. Secrets are resolved on demand from Google Cloud Secret Manager
using Application Default Credentials (ADC), with safe local environment
variable indirection for offline unit tests.
"""

from __future__ import annotations

import hmac
import hashlib
import logging
import os
import time
from typing import Callable

from google.api_core import exceptions as gcp_exceptions
from google.cloud import secretmanager

logger = logging.getLogger("resolutions_agent.secrets")


class SecretResolutionError(RuntimeError):
    """Raised when a required secret cannot be retrieved securely."""


class SecretManagerProvider:
    """Retrieves and caches secrets from Google Cloud Secret Manager."""

    def __init__(
        self,
        project_id: str | None = None,
        cache_ttl_seconds: int = 300,
        client_factory: Callable[[], secretmanager.SecretManagerServiceClient] | None = None,
    ) -> None:
        self._project_id = project_id or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
        self._cache_ttl_seconds = cache_ttl_seconds
        self._client_factory = client_factory or secretmanager.SecretManagerServiceClient
        self._client: secretmanager.SecretManagerServiceClient | None = None
        self._cache: dict[str, tuple[str, float]] = {}

    def _get_client(self) -> secretmanager.SecretManagerServiceClient:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def get_secret(
        self,
        secret_id: str,
        *,
        version: str = "latest",
        env_fallback_var: str | None = None,
        default_for_local_dev: str | None = None,
    ) -> str:
        """Fetches a secret payload from Google Cloud Secret Manager.

        Args:
            secret_id: Secret Manager identifier (not the secret value itself).
            version: Secret version alias, defaults to 'latest'.
            env_fallback_var: Optional environment variable name checked only when
                running in local development without a configured GCP project.
            default_for_local_dev: Optional non-sensitive local fallback value
                (e.g., local SQLite DSN) when neither Secret Manager nor env is set.

        Returns:
            The resolved secret string (never logged).

        Raises:
            SecretResolutionError: If the secret cannot be resolved from any
                configured secure source.
        """
        cache_key = f"{secret_id}:{version}"
        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if cached is not None and cached[1] > now:
            return cached[0]

        if self._project_id:
            resource_name = (
                f"projects/{self._project_id}/secrets/{secret_id}/versions/{version}"
            )
            try:
                response = self._get_client().access_secret_version(
                    request={"name": resource_name}
                )
                payload = response.payload.data.decode("utf-8").strip()
                self._cache[cache_key] = (payload, now + self._cache_ttl_seconds)
                logger.info(
                    "Resolved secret reference from Secret Manager",
                    extra={
                        "event_type": "secret_access",
                        "secret_id": secret_id,
                        "version": version,
                        "source": "gcp_secret_manager",
                    },
                )
                return payload
            except (gcp_exceptions.GoogleAPICallError, OSError, ValueError) as exc:
                logger.warning(
                    "Secret Manager lookup unavailable; checking configured fallback",
                    extra={
                        "event_type": "secret_fallback",
                        "secret_id": secret_id,
                        "error_type": type(exc).__name__,
                    },
                )

        if env_fallback_var:
            env_val = os.environ.get(env_fallback_var, "").strip()
            if env_val:
                self._cache[cache_key] = (env_val, now + self._cache_ttl_seconds)
                return env_val

        if default_for_local_dev is not None:
            return default_for_local_dev

        raise SecretResolutionError(
            f"Unable to resolve secret '{secret_id}' from Google Cloud Secret Manager. "
            "Configure GOOGLE_CLOUD_PROJECT with Secret Manager IAM permissions "
            "(roles/secretmanager.secretAccessor)."
        )

    def configure_genai_environment(self) -> None:
        """Injects Vertex AI or Secret Manager credentials into runtime config."""
        use_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "TRUE").upper() == "TRUE"
        if use_vertex and self._project_id:
            return

        secret_id = os.environ.get(
            "GEMINI_API_KEY_SECRET_ID", "resolutions-2027-gemini-api-key"
        )
        try:
            api_key = self.get_secret(
                secret_id, env_fallback_var="GOOGLE_API_KEY_FROM_SECRET_MOUNT"
            )
            os.environ["GOOGLE_API_KEY"] = api_key
        except SecretResolutionError:
            logger.info(
                "Proceeding with Application Default Credentials (ADC)",
                extra={"event_type": "adc_auth_mode"},
            )

    def create_hitl_approval_token(self, session_id: str, manifesto_id: str) -> str:
        """Generates an HMAC token binding a Human-in-the-Loop approval request."""
        signing_secret_id = os.environ.get(
            "HITL_SIGNING_SECRET_ID", "resolutions-2027-hitl-signing-secret"
        )
        key = self.get_secret(
            signing_secret_id,
            env_fallback_var="HITL_EPHEMERAL_SIGNING_KEY",
            default_for_local_dev="local-dev-ephemeral-hitl-hmac-key",
        )
        message = f"{session_id}:{manifesto_id}".encode("utf-8")
        return hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()[:24]


default_secret_provider = SecretManagerProvider()
