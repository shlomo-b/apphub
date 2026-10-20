"""Azure App Role mapping for SAML SSO users only (not local users)."""

from __future__ import annotations

import base64
import logging
import os
from xml.etree import ElementTree as ET

logger = logging.getLogger("apphub.saml")


def _role_name(env_name: str, default: str) -> str:
    return os.getenv(env_name, default).strip() or default


AZURE_APP_ROLE_ADMIN = _role_name("AZURE_APP_ROLE_ADMIN", "AppHubAdmin")
AZURE_APP_ROLE_VIEWER = _role_name("AZURE_APP_ROLE_VIEWER", "AppHubViewer")
SSO_ROLE_ADMIN = "admin"
SSO_ROLE_VIEWER = "viewer"

ROLE_ATTRIBUTE_NAMES = (
    "http://schemas.microsoft.com/ws/2008/06/identity/claims/role",
    "roles",
    "Role",
)


def extract_azure_app_roles_from_attributes(attributes) -> list[str]:
    roles: list[str] = []
    attrs = attributes or {}
    for attr_name in ROLE_ATTRIBUTE_NAMES:
        if attr_name in attrs:
            values = attrs[attr_name]
            if isinstance(values, list):
                for value in values:
                    if value and str(value).strip() and str(value).strip() not in roles:
                        roles.append(str(value).strip())
            elif values and str(values).strip() and str(values).strip() not in roles:
                roles.append(str(values).strip())

    for attr_name, attr_val in attrs.items():
        if attr_name in ROLE_ATTRIBUTE_NAMES:
            continue
        if "role" not in attr_name.lower():
            continue
        if isinstance(attr_val, list):
            for value in attr_val:
                if value and str(value).strip() and str(value).strip() not in roles:
                    roles.append(str(value).strip())
        elif attr_val and str(attr_val).strip() and str(attr_val).strip() not in roles:
            roles.append(str(attr_val).strip())
    return roles


def extract_azure_app_roles_from_saml_xml(saml_response_b64: str) -> list[str]:
    try:
        saml_xml = base64.b64decode(saml_response_b64)
        root = ET.fromstring(saml_xml)
        ns = {
            "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
            "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
        }
        roles: list[str] = []
        for attr in root.findall(".//saml:Attribute", ns):
            attr_name = attr.get("Name", "")
            if "role" not in attr_name.lower():
                continue
            for attr_val in attr.findall("saml:AttributeValue", ns):
                val = attr_val.text
                if val and val.strip() and val.strip() not in roles:
                    roles.append(val.strip())
        return roles
    except Exception as exc:
        logger.debug("Could not extract roles from SAML XML: %s", exc)
        return []


def resolve_sso_app_role(role_values: list[str]) -> str | None:
    """Map Azure app role values to internal SSO role.

    - AppHubAdmin -> admin (full access)
    - AppHubViewer -> viewer (read-only)
    - No matching Azure app role -> None (caller must deny login)
    """
    normalized = {str(role).strip() for role in role_values if role and str(role).strip()}
    if AZURE_APP_ROLE_ADMIN in normalized:
        return SSO_ROLE_ADMIN
    if AZURE_APP_ROLE_VIEWER in normalized:
        return SSO_ROLE_VIEWER
    logger.info(
        "No matching AppHub Azure app role in %s (need %s or %s)",
        sorted(normalized),
        AZURE_APP_ROLE_ADMIN,
        AZURE_APP_ROLE_VIEWER,
    )
    return None


def is_sso_viewer_session(session: dict) -> bool:
    return bool(session.get("is_sso")) and session.get("sso_role") == SSO_ROLE_VIEWER
