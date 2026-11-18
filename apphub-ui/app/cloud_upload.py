"""Push and restore AppHub snapshots to AWS S3, Azure Blob, or GCP. Copied from SecretPass."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("apphub")

USE_AWS = os.environ.get("aws", "false").strip().lower() == "true"
USE_AZURE = os.environ.get("azure", "false").strip().lower() == "true"
USE_GCP = os.environ.get("gcp", "false").strip().lower() == "true"
CLOUD_FOLDER = "apphub"
CLOUD_OBJECT = f"{CLOUD_FOLDER}/apps.json"


def is_cloud_enabled() -> bool:
    return USE_AWS or USE_AZURE or USE_GCP


def get_backup_storage_label() -> str:
    if USE_AWS:
        return "AWS S3"
    if USE_AZURE:
        return "Azure Blob Storage"
    if USE_GCP:
        return "Google Cloud Storage"
    return "cloud storage"


STORAGE_LABEL = get_backup_storage_label()


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _s3_error_type(exc: Exception) -> str:
    return "s3_client_error" if "client" in str(exc).lower() else "upload_error"


def _azure_error_type(exc: Exception) -> str:
    text = str(exc).lower()
    return "azure_client_error" if "credential" in text or "blob" in text else "upload_error"


def _gcp_error_type(exc: Exception) -> str:
    text = str(exc).lower()
    return "gcp_client_error" if "google" in text or "credentials" in text else "upload_error"


def _gcp_service_account_info(raw: str) -> dict[str, Any] | None:
    try:
        info = json.loads(raw)
    except Exception:
        return None
    if not isinstance(info, dict):
        return None
    if info.get("type") != "service_account":
        return None
    if not info.get("private_key") or not info.get("client_email"):
        return None
    return info


def validate_cloud_config() -> tuple[bool, str | None, str]:
    """
    Check the active provider has a target and either complete user secrets or SA/default auth.
    Same error types as SecretPass / FortiGate backup.
    Returns (ok, error_type, auth_mode).
    """
    if not is_cloud_enabled():
        return False, "no_cloud_provider_enabled", "none"

    if USE_AWS:
        if not _env("BUCKET_NAME"):
            return False, "missing_bucket_name", "none"
        access_key = _env("AWS_ACCESS_KEY_ID")
        secret_key = _env("AWS_SECRET_ACCESS_KEY")
        if access_key and secret_key:
            return True, None, "user_keys"
        if access_key or secret_key:
            log.warning("AWS: set both AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, or leave both empty for IAM")
            return False, "s3_client_error", "none"
        return True, None, "iam"

    if USE_AZURE:
        if not _env("AZURE_STORAGE_ACCOUNT") or not _env("AZURE_STORAGE_CONTAINER"):
            return False, "missing_azure_config", "none"
        tenant_id = _env("AZURE_TENANT_ID")
        client_id = _env("AZURE_CLIENT_ID")
        client_secret = _env("AZURE_CLIENT_SECRET")
        present = [bool(tenant_id), bool(client_id), bool(client_secret)]
        if all(present):
            return True, None, "client_secret"
        if any(present):
            log.warning("Azure: set AZURE_TENANT_ID, AZURE_CLIENT_ID, and AZURE_CLIENT_SECRET together, or leave all empty for managed identity")
            return False, "azure_client_error", "none"
        return True, None, "managed_identity"

    if USE_GCP:
        if not (_env("GCP_BUCKET_NAME") or _env("GCS_BUCKET_NAME")):
            return False, "missing_gcp_config", "none"
        creds_value = _env("GCP_APPLICATION_CREDENTIALS") or _env("GOOGLE_APPLICATION_CREDENTIALS")
        if not creds_value:
            return True, None, "adc"
        if os.path.isfile(creds_value):
            try:
                info = _gcp_service_account_info(Path(creds_value).read_text(encoding="utf-8"))
            except Exception:
                log.warning("GCP: service-account file is not readable JSON")
                return False, "gcp_client_error", "none"
            if not info:
                log.warning("GCP: file is not a valid service_account JSON (need type, client_email, private_key)")
                return False, "gcp_client_error", "none"
            return True, None, "service_account_file"
        if creds_value.startswith("/") or creds_value.endswith(".json"):
            log.warning("GCP: service-account path does not exist: %s", creds_value)
            return False, "gcp_client_error", "none"
        if not _gcp_service_account_info(creds_value):
            log.warning("GCP: GCP_APPLICATION_CREDENTIALS is not valid service_account JSON")
            return False, "gcp_client_error", "none"
        return True, None, "service_account_json"

    return False, "no_cloud_provider_enabled", "none"


def log_cloud_config() -> None:
    if not is_cloud_enabled():
        log.info("Cloud backup disabled")
        return
    ok, err, mode = validate_cloud_config()
    label = get_backup_storage_label()
    if ok:
        log.info("Cloud backup ready: %s auth=%s", label, mode)
    else:
        log.warning("Cloud backup config invalid for %s: %s", label, err)


def _s3_client():
    """User keys if set; otherwise IAM role / instance profile / default chain (FortiGate)."""
    import boto3

    access_key = _env("AWS_ACCESS_KEY_ID")
    secret_key = _env("AWS_SECRET_ACCESS_KEY")
    if access_key and secret_key:
        return boto3.client(
            "s3",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
    log.info("AWS: no user keys; using IAM / default credentials")
    return boto3.client("s3")


def _azure_blob_container_client():
    """App registration keys if set; otherwise DefaultAzureCredential (managed identity)."""
    from azure.identity import ClientSecretCredential, DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient

    account = _env("AZURE_STORAGE_ACCOUNT")
    container_name = _env("AZURE_STORAGE_CONTAINER")
    tenant_id = _env("AZURE_TENANT_ID")
    client_id = _env("AZURE_CLIENT_ID")
    client_secret = _env("AZURE_CLIENT_SECRET")
    if not account or not container_name:
        return None, None
    if tenant_id and client_id and client_secret:
        credential = ClientSecretCredential(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
        )
    else:
        log.info("Azure: no client secret; using DefaultAzureCredential")
        credential = DefaultAzureCredential()
    account_url = f"https://{account}.blob.core.windows.net"
    blob_service = BlobServiceClient(account_url=account_url, credential=credential)
    return blob_service.get_container_client(container_name), container_name


def _gcp_storage_client():
    """Service-account file or JSON env; otherwise default ADC / workload identity (FortiGate)."""
    from google.cloud import storage

    creds_value = _env("GCP_APPLICATION_CREDENTIALS") or _env("GOOGLE_APPLICATION_CREDENTIALS")
    if creds_value:
        if os.path.isfile(creds_value):
            try:
                return storage.Client.from_service_account_json(creds_value), None
            except Exception as exc:
                log.exception("GCP credentials (file) error: %s", exc)
                return None, "gcp_client_error"
        try:
            info = json.loads(creds_value)
            return storage.Client.from_service_account_info(info), None
        except Exception as exc:
            log.exception("GCP credentials (JSON env) error: %s", exc)
            return None, "gcp_client_error"
    try:
        log.info("GCP: no service-account JSON; using default credentials")
        return storage.Client(), None
    except Exception as exc:
        log.exception("GCP default credentials error: %s", exc)
        return None, "gcp_client_error"


def put_backup_object(key: str, body: bytes, content_type: str = "application/json") -> tuple[bool, str | None]:
    """Store backup JSON. Priority if multiple flags: AWS → Azure → GCP."""
    ok, err, _mode = validate_cloud_config()
    if not ok:
        return False, err

    if USE_AWS:
        bucket = _env("BUCKET_NAME")
        try:
            _s3_client().put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
            log.info("Pushed AppHub snapshot to s3://%s/%s", bucket, key)
            return True, None
        except Exception as exc:
            log.exception("AWS S3 push to %s/%s failed", bucket, key)
            return False, _s3_error_type(exc)

    if USE_AZURE:
        container_client, container_name = _azure_blob_container_client()
        if container_client is None:
            return False, "missing_azure_config"
        try:
            container_client.get_blob_client(key).upload_blob(
                body, overwrite=True, content_type=content_type
            )
            log.info("Pushed AppHub snapshot to Azure Blob %s/%s", container_name, key)
            return True, None
        except Exception as exc:
            log.exception("Azure Blob push to %s failed", key)
            return False, _azure_error_type(exc)

    if USE_GCP:
        bucket_name = _env("GCP_BUCKET_NAME") or _env("GCS_BUCKET_NAME")
        client, cred_err = _gcp_storage_client()
        if cred_err:
            return False, cred_err
        try:
            client.bucket(bucket_name).blob(key).upload_from_string(body, content_type=content_type)
            log.info("Pushed AppHub snapshot to gs://%s/%s", bucket_name, key)
            return True, None
        except Exception as exc:
            log.exception("GCP push to %s/%s failed", bucket_name, key)
            return False, _gcp_error_type(exc)

    return False, "no_cloud_provider_enabled"


def get_backup_object(key: str) -> tuple[bytes | None, str | None]:
    """Read backup bytes. Same provider priority as put_backup_object."""
    ok, err, _mode = validate_cloud_config()
    if not ok:
        return None, err

    if USE_AWS:
        bucket = _env("BUCKET_NAME")
        try:
            response = _s3_client().get_object(Bucket=bucket, Key=key)
            return response["Body"].read(), None
        except Exception as exc:
            log.exception("AWS S3 restore from %s/%s failed", bucket, key)
            return None, _s3_error_type(exc)

    if USE_AZURE:
        container_client, _ = _azure_blob_container_client()
        if container_client is None:
            return None, "missing_azure_config"
        try:
            return container_client.get_blob_client(key).download_blob().readall(), None
        except Exception as exc:
            log.exception("Azure Blob restore from %s failed", key)
            return None, _azure_error_type(exc)

    if USE_GCP:
        bucket_name = _env("GCP_BUCKET_NAME") or _env("GCS_BUCKET_NAME")
        client, cred_err = _gcp_storage_client()
        if cred_err:
            return None, cred_err
        try:
            return client.bucket(bucket_name).blob(key).download_as_bytes(), None
        except Exception as exc:
            log.exception("GCP restore from %s/%s failed", bucket_name, key)
            return None, _gcp_error_type(exc)

    return None, "no_cloud_provider_enabled"


def push_snapshot(
    apps: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    icons: list[dict[str, Any]] | None = None,
) -> tuple[bool, str | None]:
    """Upload apps, sections, and icon bytes to $provider/apphub/apps.json."""
    if not is_cloud_enabled():
        return False, "no_cloud_provider_enabled"
    body = json.dumps(
        {"apps": apps, "sections": sections, "icons": icons or []},
        indent=2,
    ).encode("utf-8")
    return put_backup_object(CLOUD_OBJECT, body)


def fetch_snapshot() -> tuple[dict[str, Any] | None, str | None]:
    """Read apphub/apps.json from the active cloud provider. Returns (payload, error)."""
    if not is_cloud_enabled():
        return None, "no_cloud_provider_enabled"
    raw, err = get_backup_object(CLOUD_OBJECT)
    if err:
        return None, err
    try:
        payload = json.loads((raw or b"").decode("utf-8"))
    except Exception as exc:
        return None, str(exc)
    if not isinstance(payload, dict):
        return None, "backup is not a JSON object"
    log.info("Loaded AppHub snapshot from %s %s", STORAGE_LABEL, CLOUD_OBJECT)
    return payload, None
