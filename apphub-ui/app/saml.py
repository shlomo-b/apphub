"""Azure AD SAML SSO for AppHub. Routes live under /remote/saml and /saml."""

from __future__ import annotations

import base64
import html as html_module
import logging
import os
import traceback
from datetime import datetime, timezone
from typing import Any
from xml.etree import ElementTree as ET

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import mongodb_atlas as atlas
from app.sso_roles import (
    AZURE_APP_ROLE_ADMIN,
    AZURE_APP_ROLE_VIEWER,
    extract_azure_app_roles_from_attributes,
    extract_azure_app_roles_from_saml_xml,
    is_sso_viewer_session,
    resolve_sso_app_role,
)

logger = logging.getLogger("apphub.saml")

router = APIRouter()

REQUIRED_SAML_ENV = (
    "SAML_IDENTIFIER",
    "SAML_REPLY_URL",
    "SAML_LOGIN_URL",
    "SAML_CERTIFICATE",
    "MICROSOFT_ENTRA_IDENTIFIER",
    "FRONTEND_URL_SAML",
)

MICROSOFT_LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="21" height="21" viewBox="0 0 21 21">'
    '<rect x="0" y="0" width="10" height="10" fill="#f25022"/>'
    '<rect x="11" y="0" width="10" height="10" fill="#7fba00"/>'
    '<rect x="0" y="11" width="10" height="10" fill="#00a4ef"/>'
    '<rect x="11" y="11" width="10" height="10" fill="#ffb900"/>'
    "</svg>"
)


def _env_flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def saml_flag_enabled() -> bool:
    return _env_flag("ENABLE_SAML")


def saml_configured() -> bool:
    return all(os.getenv(name, "").strip() for name in REQUIRED_SAML_ENV)


def saml_enabled() -> bool:
    return saml_flag_enabled() and saml_configured()


def _bool_env(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() == "true"


def get_saml_settings() -> dict[str, Any]:
    return {
        "strict": True,
        "debug": True,
        "sp": {
            "entityId": os.getenv("SAML_IDENTIFIER", "").strip(),
            "assertionConsumerService": {
                "url": os.getenv("SAML_REPLY_URL", "").strip(),
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
            },
            "singleLogoutService": {
                "url": os.getenv("SAML_REPLY_URL", "").strip(),
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            "x509cert": "",
            "privateKey": "",
        },
        "idp": {
            "entityId": os.getenv("MICROSOFT_ENTRA_IDENTIFIER", "").strip(),
            "singleSignOnService": {
                "url": os.getenv("SAML_LOGIN_URL", "").strip(),
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            "singleLogoutService": {
                "url": os.getenv("SAML_LOGIN_URL", "").strip(),
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            "x509cert": os.getenv("SAML_CERTIFICATE", "").strip(),
        },
    }


def prepare_request(request: Request, post_data: dict[str, Any] | None = None) -> dict[str, Any]:
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    host = host.split(",")[0].strip()
    if scheme == "https":
        port = "443"
    elif scheme == "http":
        port = "80"
    else:
        port = str(request.url.port or "")
    url_data = str(request.url).split("?")
    return {
        "https": "on" if scheme == "https" else "off",
        "http_host": host,
        "server_port": port,
        "script_name": request.url.path,
        "get_data": dict(request.query_params),
        "post_data": dict(post_data or {}),
        "query_string": url_data[1] if len(url_data) > 1 else "",
    }


def access_denied_html(reason: str) -> str:
    reason_escaped = html_module.escape(str(reason))
    return (
        """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Sign in - Access denied</title>
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0;
      padding: 24px;
      font-family: "Segoe UI", "Segoe UI Web (West European)", "Segoe UI", -apple-system, BlinkMacSystemFont, "Roboto", "Helvetica Neue", sans-serif;
      font-size: 16px;
      line-height: 1.6;
      color: #1b1b1f;
      background: #fff;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
    }
    .wrapper { width: 100%; max-width: 520px; text-align: center; }
    .header { display: flex; justify-content: center; margin-bottom: 36px; }
    .logo { display: flex; align-items: center; gap: 12px; text-decoration: none; color: #1b1b1f; font-weight: 600; font-size: 24px; }
    .logo svg { width: 28px; height: 28px; }
    .content { text-align: left; }
    .main-msg { border: 1px solid #1b1b1f; padding: 20px 24px; margin-bottom: 28px; font-size: 18px; text-align: center; }
    .error-code { font-size: 14px; color: #605e5c; margin-bottom: 12px; }
    .detail { color: #1b1b1f; margin-bottom: 18px; font-size: 16px; }
    .detail.details-box { margin-top: 16px; font-size: 15px; color: #605e5c; }
    .contact { color: #605e5c; font-size: 16px; }
    .try { margin-top: 28px; }
    .try-title { font-weight: 600; margin: 0 0 8px; color: #1b1b1f; }
    .try ul { margin: 0; padding-left: 22px; }
    .try li { margin: 6px 0; }
    .try a { color: #1f8a4c; }
    .try a:hover { color: #176b3b; }
  </style>
</head>
<body>
  <div class="wrapper">
    <div class="header">
      <a href="https://www.microsoft.com" class="logo" aria-label="Microsoft">
        """
        + MICROSOFT_LOGO_SVG
        + """
        <span>Microsoft</span>
      </a>
    </div>
    <div class="content">
      <div class="main-msg">Sorry, but we're having trouble signing you in.</div>
      <div class="error-code">AADSTS50105</div>
      <div class="detail">Your administrator has configured this application to allow only specific users or groups. The signed-in user is not assigned access. Please contact your administrator to request access to this application.</div>
      <div class="detail details-box">Details: """
        + reason_escaped
        + """</div>
      <div class="contact">Please contact your administrator to assign access to this application.</div>
      <div class="try">
        <p class="try-title">What you can try:</p>
        <ul>
          <li><a href="/" onclick="if (history.length > 1) { event.preventDefault(); history.back(); }">Go back to the previous page</a></li>
          <li><a href="/">Go back to login</a></li>
        </ul>
      </div>
    </div>
  </div>
</body>
</html>"""
    )


def extract_groups_from_saml_xml(saml_response_b64: str) -> list[str]:
    try:
        saml_xml = base64.b64decode(saml_response_b64)
        root = ET.fromstring(saml_xml)
        ns = {
            "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
            "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
        }
        groups: list[str] = []
        for attr in root.findall(".//saml:Attribute", ns):
            attr_name = attr.get("Name", "")
            if "group" in attr_name.lower():
                for attr_val in attr.findall("saml:AttributeValue", ns):
                    val = attr_val.text
                    if val and val.strip():
                        groups.append(val.strip())
        return groups
    except Exception as exc:
        logger.debug("Could not extract groups from XML: %s", exc)
        return []


def check_azure_group_user_access(
    attributes,
    name_id,
    email,
    auth=None,
    saml_response_b64=None,
    app_role_ok: bool = False,
) -> tuple[bool, str]:
    enable_groups = _bool_env("ENABLE_AZURE_GROUPS") and not app_role_ok
    enable_users = _bool_env("ENABLE_AZURE_USERS") and not app_role_ok
    if not enable_groups and not enable_users and not app_role_ok:
        return True, "Azure access control disabled"

    allowed_orgs_str = os.getenv("ALLOWED_ORGANIZATIONS_ID", "")
    if allowed_orgs_str and allowed_orgs_str.strip():
        allowed_orgs = [org.strip() for org in allowed_orgs_str.split(",") if org.strip()]
        tenant_id = None
        tenant_attributes = [
            "http://schemas.microsoft.com/identity/claims/tenantid",
            "tenantid",
            "http://schemas.microsoft.com/ws/2008/06/identity/claims/tenantid",
        ]
        for attr_name in tenant_attributes:
            if attr_name in attributes:
                tenant_id = attributes[attr_name]
                if isinstance(tenant_id, list) and len(tenant_id) > 0:
                    tenant_id = tenant_id[0]
                break
        if not tenant_id and auth:
            try:
                issuer = auth.get_settings().get_idp_data().get("entityId", "")
                if "sts.windows.net" in issuer:
                    parts = issuer.split("/")
                    for i, part in enumerate(parts):
                        if part == "sts.windows.net" and i + 1 < len(parts):
                            tenant_id = parts[i + 1]
                            break
            except Exception as exc:
                logger.debug("Could not extract tenant ID from issuer: %s", exc)
        if tenant_id:
            logger.info("User tenant ID: %s", tenant_id)
            if tenant_id not in allowed_orgs:
                logger.warning("User tenant ID %s is not in allowed organizations - access denied", tenant_id)
                return False, f"Organization/tenant ID not allowed: {tenant_id}"
            logger.info("User tenant ID %s is in allowed organizations - will check groups/users", tenant_id)
        else:
            logger.warning("Could not determine tenant ID from SAML response - will check groups/users")

    allowed_group_ids: set[str] = set()
    if enable_groups:
        groups_str = os.getenv("ALLOWED_AZURE_GROUPS_ID", "")
        if groups_str and groups_str.strip().lower() != "false":
            allowed_group_ids = {item.strip() for item in groups_str.split(",") if item.strip()}
        if not allowed_group_ids:
            logger.warning("ENABLE_AZURE_GROUPS is true but ALLOWED_AZURE_GROUPS_ID is empty or false - denying access")
            return False, "ENABLE_AZURE_GROUPS is true but ALLOWED_AZURE_GROUPS_ID is empty or false"

    allowed_user_ids: set[str] = set()
    allowed_user_emails_lower: set[str] = set()
    allowed_user_ids_lower: set[str] = set()
    if enable_users:
        users_str = os.getenv("ALLOWED_AZURE_USERS_ID", "")
        if users_str and users_str.strip().lower() != "false":
            for item in users_str.split(","):
                item = item.strip()
                if not item:
                    continue
                if "@" in item:
                    allowed_user_emails_lower.add(item.lower())
                else:
                    allowed_user_ids.add(item)
            allowed_user_ids_lower = {x.lower() for x in allowed_user_ids}
        if not allowed_user_ids and not allowed_user_emails_lower:
            logger.warning("ENABLE_AZURE_USERS is true but ALLOWED_AZURE_USERS_ID is empty or false - denying access")
            return False, "ENABLE_AZURE_USERS is true but ALLOWED_AZURE_USERS_ID is empty or false"

    logger.info("Checking access: Groups mode=%s, Users mode=%s", enable_groups, enable_users)

    user_object_id = None
    object_id_attrs = [
        "http://schemas.microsoft.com/identity/claims/objectidentifier",
        "objectidentifier",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/nameidentifier",
    ]
    for attr_name in object_id_attrs:
        if attr_name in attributes:
            obj_id_val = attributes[attr_name]
            if isinstance(obj_id_val, list):
                if len(obj_id_val) > 0:
                    obj_id_val = obj_id_val[0]
                else:
                    continue
            if obj_id_val:
                user_object_id = str(obj_id_val).strip()
                if user_object_id:
                    break

    logger.info("Extracted Object ID from SAML: %s", user_object_id)

    user_email = None
    email_attrs = [
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/email",
        "email",
        "mail",
        "http://schemas.microsoft.com/identity/claims/emailaddress",
    ]
    for attr_name in email_attrs:
        if attr_name in attributes:
            email_val = attributes[attr_name]
            if isinstance(email_val, list) and len(email_val) > 0:
                email_val = email_val[0]
            if email_val:
                user_email = str(email_val).lower().strip()
                if user_email and "@" in user_email:
                    break
                user_email = None
    if not user_email and email:
        email_str = str(email).lower().strip()
        if "@" in email_str:
            user_email = email_str

    user_groups: list[Any] = []
    attrs = attributes or {}
    group_attributes = [
        "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups",
        "groups",
        "memberOf",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/groups",
    ]
    for attr_name in group_attributes:
        if attr_name in attrs:
            groups = attrs[attr_name]
            if isinstance(groups, list):
                for group in groups:
                    if group and str(group).strip() and group not in user_groups:
                        user_groups.append(group)
            elif groups and groups not in user_groups:
                user_groups.append(groups)
    for attr_name, attr_val in attrs.items():
        if "group" in attr_name.lower() and attr_name not in group_attributes:
            if isinstance(attr_val, list):
                for group in attr_val:
                    if group and str(group).strip() and group not in user_groups:
                        user_groups.append(group)
            elif attr_val and attr_val not in user_groups:
                user_groups.append(attr_val)
    if not user_groups and saml_response_b64:
        xml_groups = extract_groups_from_saml_xml(saml_response_b64)
        if xml_groups:
            user_groups.extend(xml_groups)

    logger.info("User groups from Azure SAML: %s", user_groups)
    logger.info("Extracted user Object ID: %s", user_object_id)
    logger.info("Extracted user email: %s", user_email)

    allowed_domains_str = os.getenv("ALLOWED_EMAIL_DOMAINS", "")
    if allowed_domains_str and allowed_domains_str.strip():
        allowed_domains = [domain.strip().lower() for domain in allowed_domains_str.split(",") if domain.strip()]
        if allowed_domains:
            logger.info("Checking email domain restriction. Allowed domains: %s", allowed_domains)
            if not user_email:
                logger.warning("Email domain restriction enabled but no email found in SAML - access denied")
                return False, "Email domain restriction enabled but no email found in SAML token"
            user_email_lower = user_email.lower()
            email_domain_match = False
            for domain in allowed_domains:
                if user_email_lower.endswith("@" + domain):
                    email_domain_match = True
                    logger.info("User email %s matches allowed domain %s", user_email, domain)
                    break
            if not email_domain_match:
                allowed_domains_str_display = ", ".join(allowed_domains)
                logger.warning(
                    "Access denied: user email %s does not match any allowed domain. Allowed domains: %s",
                    user_email,
                    allowed_domains,
                )
                return False, f"User {user_email} is not from allowed domain: {allowed_domains_str_display}"
            logger.info("Email domain check passed - user email is from allowed domain")

    if app_role_ok:
        return True, "Azure AppHub app role assigned"

    if enable_groups:
        logger.info("==== CHECKING GROUPS-BASED ACCESS ====")
        logger.info("Allowed group IDs from ENV: %s", list(allowed_group_ids))
        if not user_groups:
            logger.warning("ENABLE_AZURE_GROUPS=true but Azure did not send groups.")
            return False, (
                "Azure did not send groups in SAML token. "
                "Configure Azure AD App Registration → Token configuration → Add 'Groups' claim."
            )
        allowed_group_ids_lower = {x.lower() for x in allowed_group_ids}
        for group_id in user_groups:
            group = str(group_id).strip()
            if not group:
                continue
            if group in allowed_group_ids or group.lower() in allowed_group_ids_lower:
                logger.info("User group %s is in allowed list - access granted", group)
                return True, f"User group allowed: {group}"
        logger.warning("Access denied: user not in any allowed group. Groups: %s", user_groups)
        return False, "User not in any allowed group"

    if enable_users:
        logger.info("==== CHECKING USERS-BASED ACCESS ====")
        user_in_env = False
        if user_object_id:
            obj_id_to_check = user_object_id.strip() if isinstance(user_object_id, str) else str(user_object_id).strip()
            obj_id_lower = obj_id_to_check.lower()
            exact_match = obj_id_to_check in allowed_user_ids
            case_insensitive_match = obj_id_lower in allowed_user_ids_lower
            if exact_match or case_insensitive_match:
                user_in_env = True
                logger.info("User Object ID %s found in ENV", obj_id_to_check)
            else:
                logger.info("Object ID %s NOT found in allowed Object IDs", obj_id_to_check)
        else:
            logger.warning("No Object ID found in SAML attributes - cannot check Object ID")

        if not user_in_env and user_email:
            user_email_clean = str(user_email).strip().lower()
            if user_email_clean in allowed_user_emails_lower:
                user_in_env = True
                logger.info("User email %s found in ENV", user_email_clean)
            else:
                logger.info("Email %s NOT found in allowed emails", user_email_clean)

        if not user_in_env:
            logger.error("ACCESS DENIED: User not in ENV. Object ID: %s Email: %s", user_object_id, user_email)
            return False, "User not in allowed users"

        if enable_groups:
            if not user_groups:
                return False, (
                    "User is in ENV but Azure did not send groups. "
                    "Configure Azure AD to send Groups claim, or user must be in an allowed group."
                )
            allowed_group_ids_lower = {x.lower() for x in allowed_group_ids}
            for group_id in user_groups:
                group = str(group_id).strip()
                if not group:
                    continue
                if group in allowed_group_ids or group.lower() in allowed_group_ids_lower:
                    return True, f"User Object ID allowed: {user_object_id} (in ENV and in allowed Azure group)"
            return False, "User is in ENV but not in any allowed group in Azure"
        logger.info("User is in ENV - access granted (users mode only)")
        return True, f"User Object ID allowed: {user_object_id}"

    return True, "Both groups and users access control disabled"


def check_saml_response_status(saml_response_b64: str) -> tuple[bool | None, str]:
    try:
        saml_response_xml = base64.b64decode(saml_response_b64)
        root = ET.fromstring(saml_response_xml)
        ns = {
            "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
            "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
        }
        status = root.find(".//samlp:Status", ns)
        if status is not None:
            status_code = status.find("samlp:StatusCode", ns)
            if status_code is not None:
                status_value = status_code.get("Value", "")
                logger.info("SAML Response Status Code: %s", status_value)
                failure_statuses = [
                    "urn:oasis:names:tc:SAML:2.0:status:AuthnFailed",
                    "urn:oasis:names:tc:SAML:2.0:status:InvalidAttrNameOrValue",
                    "urn:oasis:names:tc:SAML:2.0:status:RequestDenied",
                    "urn:oasis:names:tc:SAML:2.0:status:RequestUnsupported",
                    "urn:oasis:names:tc:SAML:2.0:status:NoAuthnContext",
                ]
                if any(fail_status in status_value for fail_status in failure_statuses):
                    return False, status_value
                if "urn:oasis:names:tc:SAML:2.0:status:Success" in status_value:
                    return True, status_value
                secondary_status = status_code.find("samlp:StatusCode", ns)
                if secondary_status is not None:
                    secondary_value = secondary_status.get("Value", "")
                    logger.info("SAML Response Secondary Status Code: %s", secondary_value)
                    if any(fail_status in secondary_value for fail_status in failure_statuses):
                        return False, f"{status_value} / {secondary_value}"
        return None, "Status not found or unclear"
    except Exception as exc:
        logger.warning("Error parsing SAML response status: %s", exc)
        return None, f"Parse error: {exc}"


def persist_sso_user(username: str, email: str, sso_app_role: str, azure_app_roles: list[str]) -> None:
    if not atlas.USE_MONGODB:
        return
    try:
        col = atlas.mongo_db().sso_login_users
        user = col.find_one({"username": username})
        payload = {
            "username": username,
            "email": email,
            "password": "",
            "role": "sso_user",
            "sso_app_role": sso_app_role,
            "azure_app_roles": azure_app_roles,
        }
        if not user:
            payload["created_at"] = datetime.now(timezone.utc)
            col.insert_one(payload)
        else:
            col.update_one(
                {"_id": user["_id"]},
                {"$set": {"sso_app_role": sso_app_role, "azure_app_roles": azure_app_roles}},
            )
    except Exception as exc:
        logger.warning("Could not persist SSO user %s: %s", username, exc)


def clear_sso_session(request: Request) -> None:
    request.session.pop("is_sso", None)
    request.session.pop("sso_role", None)


def reject_viewer_write(request: Request) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if is_sso_viewer_session(request.session):
        raise HTTPException(status_code=403, detail="Access denied: SSO viewer role is read-only")


def session_user_info(request: Request) -> dict[str, Any]:
    return {
        "is_sso": bool(request.session.get("is_sso")),
        "sso_role": str(request.session.get("sso_role") or ""),
        "saml_enabled": saml_enabled(),
    }


def _require_saml() -> None:
    if not saml_flag_enabled():
        raise HTTPException(status_code=404, detail="SAML is disabled")
    if not saml_configured():
        raise HTTPException(status_code=503, detail="SAML is not fully configured")


def _onelogin_auth(req: dict[str, Any]):
    from onelogin.saml2.auth import OneLogin_Saml2_Auth

    return OneLogin_Saml2_Auth(req, old_settings=get_saml_settings())


@router.get("/status")
def saml_status() -> dict[str, bool]:
    return {"enabled": saml_enabled()}


@router.api_route("/login", methods=["GET", "POST"])
def saml_login(request: Request):
    logger.info("==== SAML LOGIN INITIATED ====")
    _require_saml()
    req = prepare_request(request)
    auth = _onelogin_auth(req)
    logger.info("Redirecting to Microsoft/Azure SSO for authentication")
    return RedirectResponse(auth.login(), status_code=302)


@router.get("/metadata")
def saml_metadata():
    _require_saml()
    from onelogin.saml2.settings import OneLogin_Saml2_Settings

    settings = OneLogin_Saml2_Settings(settings=get_saml_settings(), sp_validation_only=True)
    metadata = settings.get_sp_metadata()
    errors = settings.validate_metadata(metadata)
    if errors:
        raise HTTPException(status_code=500, detail=f"Invalid SAML metadata: {errors}")
    return Response(content=metadata, media_type="application/xml")


@router.api_route("/acs", methods=["GET", "POST"])
async def saml_acs(request: Request):
    logger.info("==== SAML ACS ENDPOINT CALLED ====")
    logger.info("Request method: %s", request.method)
    _require_saml()

    post_data: dict[str, Any] = {}
    if request.method == "POST":
        form = await request.form()
        post_data = {str(key): str(value) for key, value in form.items()}

    saml_response = post_data.get("SAMLResponse") or request.query_params.get("SAMLResponse")
    if not saml_response and post_data.get("SAMLart"):
        saml_response = post_data.get("SAMLart")
        logger.info("SAML artifact binding detected")
    if not saml_response:
        logger.error("SAML LOGIN FAILURE: No SAMLResponse in POST/GET data")
        return HTMLResponse("No SAML response received", status_code=400)

    status_result, status_info = check_saml_response_status(saml_response)
    if status_result is False:
        logger.error("SAML LOGIN FAILURE: SAML Status Code indicates failure: %s", status_info)
        return HTMLResponse(f"SAML authentication failed: {status_info}", status_code=401)

    try:
        req = prepare_request(request, post_data)
        auth = _onelogin_auth(req)
        auth.process_response()
        errors = auth.get_errors()
        logger.info("SAML ERRORS: %s", errors)
        logger.info("SAML LAST ERROR REASON: %s", auth.get_last_error_reason())
        is_auth = auth.is_authenticated()
        logger.info("SAML IS AUTHENTICATED: %s", is_auth)
        if not is_auth:
            logger.error("SAML LOGIN FAILURE: User not authenticated. Errors: %s Reason: %s", errors, auth.get_last_error_reason())
            return HTMLResponse("User not authenticated", status_code=401)
        if errors:
            logger.error("SAML LOGIN FAILURE: Errors: %s Reason: %s", errors, auth.get_last_error_reason())
            return HTMLResponse(
                f"SAML response error: {errors} | Reason: {auth.get_last_error_reason()}",
                status_code=400,
            )
    except Exception as exc:
        logger.error("SAML LOGIN FAILURE: Exception: %s", exc)
        logger.error(traceback.format_exc())
        return HTMLResponse(f"SAML processing error: {exc}", status_code=500)

    name_id = auth.get_nameid()
    attributes = auth.get_attributes()
    if _bool_env("ENABLE_AZURE_GROUPS") or _bool_env("ENABLE_AZURE_USERS"):
        logger.info("NameID: %s", name_id)
        logger.info("All attribute keys: %s", list(attributes.keys()) if attributes else "None")

    email = None
    email_attr_names = [
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/email",
        "email",
        "mail",
        "http://schemas.microsoft.com/identity/claims/emailaddress",
    ]
    for attr_name in email_attr_names:
        if attr_name in attributes:
            email_val = attributes[attr_name]
            if isinstance(email_val, list) and len(email_val) > 0:
                email_val = email_val[0]
            if email_val and "@" in str(email_val):
                email = str(email_val)
                break
    if not email:
        email = name_id

    azure_app_roles = extract_azure_app_roles_from_attributes(attributes)
    if not azure_app_roles and saml_response:
        azure_app_roles = extract_azure_app_roles_from_saml_xml(saml_response)
    sso_app_role = resolve_sso_app_role(azure_app_roles)
    logger.info("Azure app roles from SAML: %s resolved=%s", azure_app_roles, sso_app_role)
    if not sso_app_role:
        reason = (
            f"No AppHub Azure app role assigned. Assign {AZURE_APP_ROLE_ADMIN} "
            f"or {AZURE_APP_ROLE_VIEWER} to this user."
        )
        logger.error("SAML ACCESS DENIED: %s user=%s", reason, email)
        return HTMLResponse(access_denied_html(reason), status_code=403, media_type="text/html")

    is_allowed, reason = check_azure_group_user_access(
        attributes,
        name_id,
        email,
        auth,
        saml_response,
        app_role_ok=True,
    )
    if not is_allowed:
        logger.error("SAML ACCESS DENIED: %s user=%s", reason, email)
        return HTMLResponse(access_denied_html(reason), status_code=403, media_type="text/html")

    logger.info("SAML ACCESS GRANTED: %s role=%s", reason, sso_app_role)

    username = email
    persist_sso_user(username, email, sso_app_role, azure_app_roles)
    request.session["user"] = username
    request.session["is_sso"] = True
    request.session["sso_role"] = sso_app_role
    logger.info("SAML LOGIN SUCCESS user=%s", username)

    frontend_url = os.getenv("FRONTEND_URL_SAML", "").rstrip("/")
    return RedirectResponse(f"{frontend_url}/?sso=success", status_code=302)


def attach(app: FastAPI) -> None:
    app.include_router(router, prefix="/remote/saml")
    app.include_router(router, prefix="/saml", include_in_schema=False)
    if saml_flag_enabled() and not saml_configured():
        missing = [name for name in REQUIRED_SAML_ENV if not os.getenv(name, "").strip()]
        logger.warning("ENABLE_SAML=true but SAML env is incomplete: %s", missing)
    elif saml_enabled():
        logger.info("Azure AD SAML SSO enabled")
