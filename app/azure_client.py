"""Async Azure BlobServiceClient lifecycle and upload helper."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from azure.core.exceptions import ClientAuthenticationError, ResourceNotFoundError
from azure.storage.blob.aio import BlobServiceClient, ContainerClient

from app.config import Settings

logger = logging.getLogger(__name__)


async def create_azure_client(
    settings: Settings,
) -> tuple[BlobServiceClient, ContainerClient, Any]:
    """Create and validate Azure client. Raises SystemExit on auth/container failure.

    Returns (blob_service_client, container_client, credential).
    credential may be a DefaultAzureCredential or None (when using connection string).
    """
    from azure.identity.aio import DefaultAzureCredential

    credential: Any = DefaultAzureCredential()
    kwargs: dict[str, Any] = {}
    if settings.azure_max_block_size is not None:
        kwargs["max_block_size"] = settings.azure_max_block_size
    if settings.azure_max_single_put_size is not None:
        kwargs["max_single_put_size"] = settings.azure_max_single_put_size

    try:
        blob_service_client = BlobServiceClient(
            account_url=settings.azure_account_url,
            credential=credential,
            **kwargs,
        )
    except ValueError:
        logger.warning(
            "DefaultAzureCredential rejected (HTTP endpoint), attempting fallback auth"
        )
        await credential.close()
        blob_service_client, container_client = await _try_fallback(settings, kwargs)
        return blob_service_client, container_client, None

    container_client = blob_service_client.get_container_client(
        settings.azure_container
    )
    try:
        await container_client.get_container_properties()
        logger.info("Azure container '%s' validated", settings.azure_container)
        return blob_service_client, container_client, credential
    except ClientAuthenticationError:
        logger.warning("DefaultAzureCredential failed, attempting fallback auth")
        await blob_service_client.close()
        await credential.close()
    except ResourceNotFoundError:
        try:
            await container_client.create_container()
            logger.info("Created Azure container '%s'", settings.azure_container)
            return blob_service_client, container_client, credential
        except Exception as exc:
            await blob_service_client.close()
            await credential.close()
            raise SystemExit(
                f"Cannot create container '{settings.azure_container}': {exc}"
            ) from exc
    except Exception as exc:
        await blob_service_client.close()
        await credential.close()
        raise SystemExit(f"Azure client init failed: {exc}") from exc

    # Fallback auth
    blob_service_client, container_client = await _try_fallback(settings, kwargs)
    return blob_service_client, container_client, None


async def _try_fallback(
    settings: Settings,
    kwargs: dict[str, Any],
) -> tuple[BlobServiceClient, ContainerClient]:
    """Attempt connection string or account key auth."""
    if settings.azure_connection_string:
        blob_service_client = BlobServiceClient.from_connection_string(
            settings.azure_connection_string,
            **kwargs,
        )
    elif settings.azure_account_name and settings.azure_account_key:
        blob_service_client = BlobServiceClient(
            account_url=settings.azure_account_url,
            credential=settings.azure_account_key,
            **kwargs,
        )
    else:
        raise SystemExit("No viable Azure credentials configured")

    container_client = blob_service_client.get_container_client(
        settings.azure_container
    )
    try:
        await container_client.get_container_properties()
    except ResourceNotFoundError:
        try:
            await container_client.create_container()
            logger.info(
                "Created Azure container '%s' (fallback auth)", settings.azure_container
            )
        except Exception as exc:
            await blob_service_client.close()
            raise SystemExit(
                f"Cannot create container '{settings.azure_container}' with fallback: {exc}"
            ) from exc
    except Exception as exc:
        await blob_service_client.close()
        raise SystemExit(f"Fallback auth failed: {exc}") from exc

    logger.info("Azure client initialized with fallback credentials")
    return blob_service_client, container_client


async def upload_file(
    container_client: ContainerClient,
    local_path: Path,
    blob_name: str,
    max_concurrency: int,
) -> None:
    """Upload a local file to Azure Blob Storage as Block Blob."""
    blob_client = container_client.get_blob_client(blob_name)
    file_size = local_path.stat().st_size
    with open(local_path, "rb") as f:
        await blob_client.upload_blob(
            f,
            overwrite=True,
            blob_type="BlockBlob",
            max_concurrency=max_concurrency,
            length=file_size,
        )


async def close_azure_client(
    blob_service_client: BlobServiceClient,
    credential: Any,
) -> None:
    """Close both the blob client and credential (credential owns its own HTTP session)."""
    await blob_service_client.close()
    if credential is not None:
        await credential.close()
