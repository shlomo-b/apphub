from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from PIL import Image, ImageOps, UnidentifiedImageError

from collections import defaultdict

from bson.binary import Binary
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from pymongo import ReplaceOne
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import mongodb_atlas as atlas
from app import metrics as metrics_mod
from app import saml as saml_mod
from app import cloud_upload

log = logging.getLogger("apphub")
logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(message)s")


def _run_parallel(*fns: Callable[[], Any]) -> list[Any]:
    """Run independent Atlas calls at the same time. Same results, less wait."""
    if not fns:
        return []
    if len(fns) == 1:
        return [fns[0]()]
    with ThreadPoolExecutor(max_workers=len(fns)) as pool:
        futures = [pool.submit(fn) for fn in fns]
        return [future.result() for future in futures]


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Environment variable {name} must not be empty")
    return value


ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
DATA = ROOT / "data" / "apps.json"
ICONS_DIR = ROOT / "data" / "icons"
SESSION_FILE = ROOT / "data" / ".session_secret"

DEFAULT_SECTIONS = [
    {"id": "devops-tools", "name": "DevOps-Tools", "collection": "devops", "builtin": True, "order": 0},
    {"id": "networking-tools", "name": "Networking-Tools", "collection": "networking", "builtin": True, "order": 1},
    {"id": "clouds", "name": "Clouds", "collection": "clouds", "builtin": True, "order": 2},
]
RESERVED_COLLECTIONS = {"icons", "settings", "sections"}
MONGO_COLLECTIONS = ("devops", "networking", "clouds", "icons", "settings", "sections")
ICON_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif"}
ICON_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".gif": "image/gif",
}
TYPE_EXTS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/gif": ".gif",
}
MAX_ICON = 2 * 1024 * 1024
ICON_SIZE = 256
APP_USER = require_env("APPHUB_USER")
APP_PASSWORD = require_env("APPHUB_PASSWORD")

_lock = threading.RLock()
_apps_cache: list[dict[str, Any]] | None = None
_sections_cache: list[dict[str, Any]] | None = None
_icon_cache: dict[str, dict[str, Any]] = {}

if atlas.USE_MONGODB:
    atlas.wait_for_mongo()
    if atlas.using_atlas():
        log.info("Connected to MongoDB Atlas successfully")
    else:
        log.info("Connected to MongoDB successfully")

app = FastAPI(title="AppHub")
app.include_router(metrics_mod.router)


def _session_secret() -> str:
    if atlas.USE_MONGODB:
        col = atlas.mongo_db().settings
        doc = col.find_one({"_id": "session"})
        secret = str((doc or {}).get("secret") or "").strip()
        if secret:
            return secret
        value = secrets.token_hex(32)
        col.replace_one({"_id": "session"}, {"_id": "session", "secret": value}, upsert=True)
        return value
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    if SESSION_FILE.exists() and SESSION_FILE.read_text(encoding="utf-8").strip():
        return SESSION_FILE.read_text(encoding="utf-8").strip()
    value = secrets.token_hex(32)
    SESSION_FILE.write_text(value, encoding="utf-8")
    return value


_https_only = os.environ.get("SESSION_HTTPS_ONLY", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret(),
    same_site="lax",
    https_only=_https_only,
)
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")
saml_mod.attach(app)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def require_login(request: Request, *, allow_viewer: bool = False) -> None:
    if not request.session.get("user"):
        raise HTTPException(status_code=401, detail="Not logged in")
    if not allow_viewer:
        saml_mod.reject_viewer_write(request)


def ensure_collections() -> None:
    if not atlas.USE_MONGODB:
        return
    database = atlas.mongo_db()
    existing = set(database.list_collection_names())
    for name in MONGO_COLLECTIONS:
        if name not in existing:
            database.create_collection(name)


def public_app(doc: dict[str, Any], section: str) -> dict[str, Any]:
    item = dict(doc)
    doc_id = item.pop("_id", None)
    item["id"] = str(item.get("id") or doc_id)
    item["section"] = section
    item.setdefault("icon", "grafana")
    item.setdefault("icon_file", "")
    item.setdefault("icon_url", "")
    item.setdefault("subtitle", "")
    return item


def public_section(doc: dict[str, Any]) -> dict[str, Any]:
    item = dict(doc)
    doc_id = item.pop("_id", None)
    item["id"] = str(item.get("id") or doc_id)
    item["name"] = str(item.get("name") or item["id"])
    item["collection"] = str(item.get("collection") or item["id"]).replace("-", "_")
    item["builtin"] = bool(item.get("builtin"))
    item["order"] = int(item.get("order") or 0)
    return item


def default_sections() -> list[dict[str, Any]]:
    return [dict(row) for row in DEFAULT_SECTIONS]


def load_file_payload() -> dict[str, Any]:
    DATA.parent.mkdir(parents=True, exist_ok=True)
    if not DATA.exists():
        return {"apps": [], "sections": default_sections()}
    try:
        payload = json.loads(DATA.read_text(encoding="utf-8"))
    except Exception:
        return {"apps": [], "sections": default_sections()}
    sections = [public_section(row) for row in (payload.get("sections") or [])]
    return {
        "apps": list(payload.get("apps") or []),
        "sections": sections or default_sections(),
    }


def save_file_payload(apps: list[dict[str, Any]], sections: list[dict[str, Any]] | None = None) -> None:
    DATA.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"apps": apps, "sections": sections if sections is not None else load_sections()}, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(DATA)


def load_file_apps() -> list[dict[str, Any]]:
    return load_file_payload()["apps"]


def save_file_apps(apps: list[dict[str, Any]]) -> None:
    save_file_payload(apps)


def snapshot_icons() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if atlas.USE_MONGODB:
        for doc in atlas.mongo_db().icons.find():
            data = doc.get("data")
            if data is None:
                continue
            rows.append(
                {
                    "id": str(doc.get("_id") or ""),
                    "app_id": str(doc.get("app_id") or doc.get("_id") or ""),
                    "name": str(doc.get("name") or ""),
                    "filename": str(doc.get("filename") or doc.get("icon_file") or ""),
                    "content_type": str(doc.get("content_type") or "application/octet-stream"),
                    "icon_file": str(doc.get("icon_file") or doc.get("filename") or ""),
                    "icon_url": str(doc.get("icon_url") or ""),
                    "data": base64.b64encode(bytes(data)).decode("ascii"),
                }
            )
        return rows
    if not ICONS_DIR.exists():
        return []
    for path in ICONS_DIR.iterdir():
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        rows.append(
            {
                "id": path.stem,
                "app_id": path.stem,
                "name": path.name,
                "filename": path.name,
                "content_type": ICON_TYPES.get(suffix, "application/octet-stream"),
                "icon_file": path.name,
                "icon_url": "",
                "data": base64.b64encode(path.read_bytes()).decode("ascii"),
            }
        )
    return rows


def restore_icon_docs(raw_icons: Any) -> int:
    if not isinstance(raw_icons, list):
        return 0
    mongo_docs: list[dict[str, Any]] = []
    count = 0
    for row in raw_icons:
        if not isinstance(row, dict):
            continue
        encoded = str(row.get("data") or "").strip()
        if not encoded:
            continue
        try:
            raw = base64.b64decode(encoded)
        except Exception:
            continue
        item_id = str(row.get("id") or row.get("app_id") or uuid.uuid4().hex[:12])
        filename = str(row.get("filename") or row.get("icon_file") or f"{item_id}.png")
        content_type = str(row.get("content_type") or "application/octet-stream")
        if atlas.USE_MONGODB:
            mongo_docs.append(
                {
                    "_id": item_id,
                    "app_id": str(row.get("app_id") or item_id),
                    "name": str(row.get("name") or ""),
                    "filename": filename,
                    "content_type": content_type,
                    "icon_file": str(row.get("icon_file") or filename),
                    "icon_url": str(row.get("icon_url") or ""),
                    "data": Binary(raw),
                }
            )
        else:
            ICONS_DIR.mkdir(parents=True, exist_ok=True)
            (ICONS_DIR / Path(filename).name).write_bytes(raw)
        count += 1
    if atlas.USE_MONGODB and mongo_docs:
        atlas.mongo_db().icons.bulk_write(
            [ReplaceOne({"_id": doc["_id"]}, doc, upsert=True) for doc in mongo_docs],
            ordered=False,
        )
        for doc in mongo_docs:
            remember_icon(doc)
    return count


def repair_missing_icons() -> None:
    if not atlas.USE_MONGODB:
        return
    col = atlas.mongo_db().icons
    for app in load_apps():
        if app.get("icon") != "custom":
            continue
        existing = col.find_one({"_id": app["id"]}) or col.find_one({"filename": app.get("icon_file") or ""})
        if existing and existing.get("data") is not None:
            remember_icon(existing)
            continue
        url = str(app.get("icon_url") or "").strip()
        if not url:
            continue
        try:
            raw, content_type, suffix = fetch_remote_icon(url)
        except Exception:
            log.warning("Could not repair icon for %s", app.get("id"))
            continue
        filename = str(app.get("icon_file") or f"{app['id']}{suffix}")
        doc = {
            "_id": app["id"],
            "app_id": app["id"],
            "name": app.get("name") or "",
            "filename": filename,
            "content_type": content_type,
            "icon_file": filename,
            "icon_url": url,
            "data": Binary(raw),
        }
        col.replace_one({"_id": app["id"]}, doc, upsert=True)
        remember_icon(doc)


def apply_snapshot(payload: dict[str, Any]) -> dict[str, int]:
    raw_sections = payload.get("sections")
    raw_apps = payload.get("apps")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise HTTPException(status_code=400, detail="Backup has no sections")
    if not isinstance(raw_apps, list):
        raise HTTPException(status_code=400, detail="Backup has no apps")

    sections: list[dict[str, Any]] = []
    used_collections: set[str] = set()
    for index, row in enumerate(raw_sections):
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        section_id = str(row.get("id") or slugify_section(name)).strip()
        if not section_id:
            continue
        collection = str(row.get("collection") or collection_from_slug(section_id))
        if collection in RESERVED_COLLECTIONS or collection in used_collections:
            collection = f"tab_{section_id.replace('-', '_')}"[:60]
        used_collections.add(collection)
        sections.append(
            {
                "id": section_id,
                "name": name or section_id,
                "collection": collection,
                "builtin": bool(row.get("builtin")),
                "order": int(row.get("order") or index),
            }
        )
    if not sections:
        raise HTTPException(status_code=400, detail="Backup has no sections")
    allowed = {row["id"] for row in sections}

    apps: list[dict[str, Any]] = []
    for row in raw_apps:
        if not isinstance(row, dict):
            continue
        section_id = str(row.get("section") or "").strip()
        if section_id not in allowed:
            continue
        name = str(row.get("name") or "").strip()
        url = str(row.get("url") or "").strip()
        if not name or not url:
            continue
        apps.append(
            {
                "id": str(row.get("id") or uuid.uuid4().hex[:12]),
                "name": name,
                "url": url,
                "section": section_id,
                "icon": str(row.get("icon") or "custom"),
                "icon_file": str(row.get("icon_file") or ""),
                "icon_url": str(row.get("icon_url") or ""),
            }
        )

    live_sections = load_sections()
    live_apps = load_apps(live_sections)
    live_app_map = {row["id"]: row for row in live_apps}
    live_section_map = {row["id"]: row for row in live_sections}
    snap_app_ids = {row["id"] for row in apps}
    snap_section_ids = {row["id"] for row in sections}

    def app_sig(row: dict[str, Any]) -> tuple[str, ...]:
        return (
            str(row.get("name") or ""),
            str(row.get("url") or ""),
            str(row.get("section") or ""),
            str(row.get("icon") or ""),
            str(row.get("icon_file") or ""),
            str(row.get("icon_url") or ""),
        )

    def section_sig(row: dict[str, Any]) -> tuple[Any, ...]:
        return (
            str(row.get("name") or ""),
            str(row.get("collection") or ""),
            bool(row.get("builtin")),
            int(row.get("order") or 0),
        )

    counts = {
        "apps": len(apps),
        "sections": len(sections),
        "apps_added": sum(1 for row in apps if row["id"] not in live_app_map),
        "apps_updated": sum(
            1
            for row in apps
            if row["id"] in live_app_map and app_sig(row) != app_sig(live_app_map[row["id"]])
        ),
        "apps_removed": sum(1 for row in live_apps if row["id"] not in snap_app_ids),
        "sections_added": sum(1 for row in sections if row["id"] not in live_section_map),
        "sections_updated": sum(
            1
            for row in sections
            if row["id"] in live_section_map
            and section_sig(row) != section_sig(live_section_map[row["id"]])
        ),
        "sections_removed": sum(1 for row in live_sections if row["id"] not in snap_section_ids),
    }

    global _sections_cache, _apps_cache
    if not atlas.USE_MONGODB:
        save_file_payload(apps, sections)
        _sections_cache = [dict(row) for row in sections]
        restore_icon_docs(payload.get("icons"))
        return counts

    started = time.monotonic()
    database = atlas.mongo_db()
    old_cols = {row["collection"] for row in live_sections}
    new_cols = {row["collection"] for row in sections}
    names = set(database.list_collection_names())
    for row in sections:
        if row["collection"] not in names:
            database.create_collection(row["collection"])
            names.add(row["collection"])
    database.sections.delete_many({})
    database.sections.bulk_write(
        [ReplaceOne({"_id": row["id"]}, {"_id": row["id"], **row}, upsert=True) for row in sections],
        ordered=False,
    )
    for col in old_cols - new_cols:
        if col and col not in RESERVED_COLLECTIONS and col in names:
            database.drop_collection(col)
    by_col: dict[str, list[ReplaceOne]] = defaultdict(list)
    for item in apps:
        target = next(row["collection"] for row in sections if row["id"] == item["section"])
        doc = {
            "_id": item["id"],
            "id": item["id"],
            "name": item["name"],
            "url": item["url"],
            "icon": item["icon"],
            "icon_file": item.get("icon_file") or "",
            "icon_url": item.get("icon_url") or "",
        }
        by_col[target].append(ReplaceOne({"_id": item["id"]}, doc, upsert=True))

    def replace_collection(col: str) -> None:
        if col in RESERVED_COLLECTIONS:
            return
        database[col].delete_many({})
        ops = by_col.get(col) or []
        if ops:
            database[col].bulk_write(ops, ordered=False)

    app_cols = [col for col in new_cols if col not in RESERVED_COLLECTIONS]
    _run_parallel(*[lambda col=col: replace_collection(col) for col in app_cols])
    database.icons.delete_many({})
    _icon_cache.clear()
    _sections_cache = None
    _apps_cache = None
    restored_icons = restore_icon_docs(payload.get("icons"))
    if restored_icons == 0:
        repair_missing_icons()
    log.info(
        "Restore snapshot %.0fms apps=%s sections=%s icons=%s added_apps=%s",
        (time.monotonic() - started) * 1000,
        len(apps),
        len(sections),
        restored_icons,
        counts.get("apps_added"),
    )
    return counts


def restore_result_message(label: str, counts: dict[str, int]) -> str:
    def phrase(n: int, singular: str, verb: str) -> str | None:
        if n <= 0:
            return None
        noun = singular if n == 1 else f"{singular}s"
        return f"{n} {noun} {verb}"

    parts = [
        phrase(counts.get("apps_added", 0), "app", "restored"),
        phrase(counts.get("apps_updated", 0), "app", "updated"),
        phrase(counts.get("apps_removed", 0), "extra app", "removed"),
        phrase(counts.get("sections_added", 0), "section", "restored"),
        phrase(counts.get("sections_updated", 0), "section", "updated"),
        phrase(counts.get("sections_removed", 0), "extra section", "removed"),
    ]
    changed = [part for part in parts if part]
    if not changed:
        return f"Restore completed from {label} (already up to date)"
    return f"Restore completed from {label} ({', '.join(changed)})"


def load_sections() -> list[dict[str, Any]]:
    global _sections_cache
    if not atlas.USE_MONGODB:
        rows = load_file_payload()["sections"]
        _sections_cache = [dict(row) for row in rows]
        return [dict(row) for row in rows]
    # Always read from Mongo so every Kubernetes pod sees the same list.
    started = time.monotonic()
    rows = [public_section(doc) for doc in atlas.mongo_db().sections.find()]
    if not rows:
        rows = seed_default_sections()
    rows.sort(key=lambda row: (row.get("order") or 0, row["name"].lower()))
    _sections_cache = [dict(row) for row in rows]
    log.info("MongoDB load_sections %.0fms sections=%s", (time.monotonic() - started) * 1000, len(rows))
    return [dict(row) for row in rows]


def seed_default_sections() -> list[dict[str, Any]]:
    rows = default_sections()
    if atlas.USE_MONGODB:
        database = atlas.mongo_db()
        for row in rows:
            database.sections.replace_one(
                {"_id": row["id"]},
                {
                    "_id": row["id"],
                    "id": row["id"],
                    "name": row["name"],
                    "collection": row["collection"],
                    "builtin": True,
                    "order": row["order"],
                },
                upsert=True,
            )
            if row["collection"] not in set(database.list_collection_names()):
                database.create_collection(row["collection"])
    return rows


def section_collections() -> dict[str, str]:
    return {row["id"]: row["collection"] for row in load_sections()}


def collection_sections() -> dict[str, str]:
    return {row["collection"]: row["id"] for row in load_sections()}


def slugify_section(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:40]


def collection_from_slug(slug: str) -> str:
    name = slug.replace("-", "_") or "section"
    if name in RESERVED_COLLECTIONS:
        name = f"tab_{name}"
    return name[:60]


def remember_icon(doc: dict[str, Any]) -> None:
    data = doc.get("data")
    if data is None:
        return
    payload = {
        "data": bytes(data),
        "content_type": str(doc.get("content_type") or "application/octet-stream"),
    }
    keys = {
        str(doc.get("_id") or ""),
        str(doc.get("app_id") or ""),
        str(doc.get("filename") or ""),
        str(doc.get("icon_file") or ""),
    }
    stem = Path(str(doc.get("filename") or doc.get("icon_file") or "")).stem
    if stem:
        keys.add(stem)
    for key in keys:
        if key:
            _icon_cache[key] = payload


def forget_icon(item_id: str, filename: str | None = None) -> None:
    keys = {item_id, filename or "", Path(filename or "").stem, Path(item_id).stem}
    for key in keys:
        if key:
            _icon_cache.pop(key, None)


def cached_icon(name: str) -> dict[str, Any] | None:
    return _icon_cache.get(name) or _icon_cache.get(Path(name).stem)


def set_apps_cache_item(item: dict[str, Any]) -> None:
    global _apps_cache
    if not atlas.USE_MONGODB:
        return
    rows = [] if _apps_cache is None else [row for row in _apps_cache if row.get("id") != item["id"]]
    rows.append(dict(item))
    _apps_cache = rows


def drop_apps_cache_item(item_id: str) -> None:
    global _apps_cache
    if _apps_cache is None:
        return
    _apps_cache = [row for row in _apps_cache if row.get("id") != item_id]


def load_apps(sections: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    if not atlas.USE_MONGODB:
        return load_file_apps()
    global _apps_cache
    # Always read from Mongo so every Kubernetes pod sees the same apps.
    # Per-pod memory cache caused tiles to appear/disappear on refresh
    # when the Service load-balanced across replicas.
    started = time.monotonic()
    mapping = {
        row["collection"]: row["id"]
        for row in (sections if sections is not None else load_sections())
    }
    database = atlas.mongo_db()

    def fetch(name: str, section: str) -> list[dict[str, Any]]:
        return [public_app(doc, section) for doc in database[name].find()]

    pairs = list(mapping.items())
    if len(pairs) <= 1:
        parts = [fetch(name, section) for name, section in pairs]
    else:
        with ThreadPoolExecutor(max_workers=min(8, len(pairs))) as pool:
            parts = list(pool.map(lambda pair: fetch(pair[0], pair[1]), pairs))
    rows: list[dict[str, Any]] = []
    for part in parts:
        rows.extend(part)
    _apps_cache = [dict(row) for row in rows]
    log.info("MongoDB load_apps %.0fms apps=%s", (time.monotonic() - started) * 1000, len(rows))
    return [dict(row) for row in rows]


def find_app(item_id: str) -> dict[str, Any] | None:
    return next((row for row in load_apps() if row["id"] == item_id), None)


def write_app(item: dict[str, Any], previous: dict[str, Any] | None = None) -> None:
    if not atlas.USE_MONGODB:
        apps = [row for row in load_file_apps() if row.get("id") != item["id"]]
        apps.append(item)
        save_file_apps(apps)
        return
    database = atlas.mongo_db()
    target = section_collections()[item["section"]]
    if previous and previous.get("section") != item["section"]:
        old = section_collections().get(previous["section"])
        if old:
            database[old].delete_one({"_id": item["id"]})
    write_icon_doc(item, previous)
    doc = {
        "_id": item["id"],
        "id": item["id"],
        "name": item["name"],
        "url": item["url"],
        "icon": item["icon"],
        "icon_file": item.get("icon_file") or "",
        "icon_url": item.get("icon_url") or "",
    }
    database[target].replace_one({"_id": item["id"]}, doc, upsert=True)
    set_apps_cache_item(public_app(doc, item["section"]))


def delete_stored_app(item: dict[str, Any]) -> None:
    if atlas.USE_MONGODB:
        database = atlas.mongo_db()
        col = section_collections().get(item["section"])
        if col:
            database[col].delete_one({"_id": item["id"]})
        database.icons.delete_one({"_id": item["id"]})
        drop_apps_cache_item(item["id"])
        forget_icon(item["id"], item.get("icon_file"))
    else:
        save_file_apps([row for row in load_file_apps() if row.get("id") != item["id"]])
    remove_icon_file(item.get("icon_file"))


def normalize_icon_bytes(raw: bytes, suffix: str, content_type: str) -> tuple[bytes, str, str]:
    start = raw.lstrip()[:256]
    if suffix == ".svg" or start.startswith(b"<svg") or start.startswith(b"<?xml"):
        return raw, "image/svg+xml", ".svg"
    try:
        with Image.open(BytesIO(raw)) as src:
            img = ImageOps.exif_transpose(src) or src
            img = img.copy()
        img.thumbnail((ICON_SIZE, ICON_SIZE), Image.Resampling.LANCZOS)
        has_alpha = img.mode in {"RGBA", "LA"} or (img.mode == "P" and "transparency" in img.info)
        out = BytesIO()
        if has_alpha:
            if img.mode != "RGBA":
                img = img.convert("RGBA")
            img.save(out, format="PNG", optimize=True)
            return out.getvalue(), "image/png", ".png"
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.save(out, format="JPEG", quality=88, optimize=True)
        return out.getvalue(), "image/jpeg", ".jpg"
    except UnidentifiedImageError as exc:
        if looks_like_image(raw):
            return raw, content_type, suffix
        raise HTTPException(status_code=400, detail="Icon file is not a valid image") from exc
    except HTTPException:
        raise
    except Exception as exc:
        if looks_like_image(raw):
            log.warning("Could not resize icon, using original: %s", exc)
            return raw, content_type, suffix
        raise HTTPException(status_code=400, detail="Icon file is not a valid image") from exc


def looks_like_image(raw: bytes) -> bool:
    start = raw.lstrip()
    return (
        raw.startswith(b"\x89PNG")
        or raw.startswith(b"\xff\xd8\xff")
        or raw.startswith(b"GIF")
        or (raw.startswith(b"RIFF") and b"WEBP" in raw[:16])
        or start.startswith(b"<svg")
        or start.startswith(b"<?xml")
    )


def icon_suffix(url: str, content_type: str, raw: bytes) -> str:
    path = urlparse(url).path.lower()
    for ext in ICON_EXTS:
        if path.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    if content_type in TYPE_EXTS:
        return TYPE_EXTS[content_type]
    if raw.startswith(b"\x89PNG"):
        return ".png"
    if raw.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if raw.startswith(b"GIF"):
        return ".gif"
    if raw.startswith(b"RIFF") and b"WEBP" in raw[:16]:
        return ".webp"
    if raw.lstrip().startswith(b"<svg") or raw.lstrip().startswith(b"<?xml"):
        return ".svg"
    return ".png"


def fetch_remote_icon(url: str) -> tuple[bytes, str, str]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; AppHub/1.0)",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            content_type = (resp.headers.get_content_type() or "").lower()
            raw = resp.read(MAX_ICON + 1)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Could not download that icon URL") from exc
    if not raw:
        raise HTTPException(status_code=400, detail="Icon URL did not return an image")
    if len(raw) > MAX_ICON:
        raise HTTPException(status_code=400, detail="Icon is too large (max 2MB)")
    if content_type not in TYPE_EXTS and not looks_like_image(raw):
        raise HTTPException(status_code=400, detail="Icon URL must point to an image")
    suffix = icon_suffix(url, content_type, raw)
    media = content_type if content_type in TYPE_EXTS else ICON_TYPES.get(suffix, "application/octet-stream")
    return normalize_icon_bytes(raw, suffix, media)


def write_icon_doc(item: dict[str, Any], previous: dict[str, Any] | None = None) -> None:
    if not atlas.USE_MONGODB:
        return
    col = atlas.mongo_db().icons
    if item.get("icon") != "custom":
        col.delete_one({"_id": item["id"]})
        forget_icon(item["id"], item.get("icon_file"))
        if previous:
            forget_icon(item["id"], previous.get("icon_file"))
        return

    existing = col.find_one({"_id": item["id"]}) or {}
    url = str(item.get("icon_url") or "").strip()
    filename = str(item.get("icon_file") or "").strip()
    prev_file = str((previous or {}).get("icon_file") or existing.get("icon_file") or existing.get("filename") or "")

    def store(doc: dict[str, Any]) -> None:
        forget_icon(item["id"], prev_file)
        forget_icon(item["id"], existing.get("filename"))
        forget_icon(item["id"], existing.get("icon_file"))
        col.replace_one({"_id": item["id"]}, doc, upsert=True)
        remember_icon(doc)
        item["icon_file"] = str(doc.get("icon_file") or "")

    # Upload path already wrote bytes in save_icon_upload.
    if filename and not url and existing.get("data") is not None:
        store(
            {
                "_id": item["id"],
                "app_id": item["id"],
                "name": item["name"],
                "filename": existing.get("filename") or filename,
                "content_type": existing.get("content_type") or "application/octet-stream",
                "icon_file": filename,
                "icon_url": "",
                "data": existing["data"],
            }
        )
        return

    if url:
        prev_url = str(existing.get("icon_url") or "").strip()
        same_url = prev_url == url and existing.get("data") is not None
        if same_url:
            item["icon_file"] = str(existing.get("icon_file") or existing.get("filename") or "")
            remember_icon(existing)
            return
        try:
            raw, content_type, suffix = fetch_remote_icon(url)
        except HTTPException:
            log.warning("Could not download icon URL for %s", item["id"])
            raise
        stored_name = f"{item['id']}{suffix}"
        store(
            {
                "_id": item["id"],
                "app_id": item["id"],
                "name": item["name"],
                "filename": stored_name,
                "content_type": content_type,
                "icon_file": stored_name,
                "icon_url": url,
                "data": Binary(raw),
            }
        )
        return

    if existing.get("data") is not None:
        item["icon_file"] = str(existing.get("icon_file") or existing.get("filename") or "")
        remember_icon(existing)
        return

    raise HTTPException(status_code=400, detail="Paste an icon URL or upload an image")


def hydrate_remote_icons() -> None:
    if not atlas.USE_MONGODB:
        return
    database = atlas.mongo_db()
    for doc in database.icons.find():
        if doc.get("data") is not None:
            continue
        url = str(doc.get("icon_url") or "").strip()
        if not url:
            continue
        try:
            raw, content_type, suffix = fetch_remote_icon(url)
        except Exception:
            log.warning("Could not download stored icon URL for %s", doc.get("_id"))
            continue
        filename = f"{doc['_id']}{suffix}"
        database.icons.replace_one(
            {"_id": doc["_id"]},
            {
                "_id": doc["_id"],
                "app_id": doc.get("app_id") or doc["_id"],
                "name": doc.get("name") or "",
                "filename": filename,
                "content_type": content_type,
                "icon_file": filename,
                "icon_url": url,
                "data": Binary(raw),
            },
            upsert=True,
        )
        for name in section_collections().values():
            database[name].update_one({"_id": doc["_id"]}, {"$set": {"icon_file": filename}})
        log.info("Stored icon image for %s", doc["_id"])
    warm_icon_cache()


def warm_icon_cache() -> None:
    if not atlas.USE_MONGODB:
        return
    started = time.monotonic()
    count = 0
    for doc in atlas.mongo_db().icons.find():
        if doc.get("data") is None:
            continue
        remember_icon(doc)
        count += 1
    log.info("MongoDB icon cache %.0fms icons=%s", (time.monotonic() - started) * 1000, count)


def save_icon_upload(upload: UploadFile, item_id: str, app_name: str) -> str:
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix == ".jpeg":
        suffix = ".jpg"
    if suffix not in ICON_EXTS:
        raise HTTPException(status_code=400, detail="Icon must be png, jpg, webp, svg, or gif")
    raw = upload.file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Icon file is empty")
    if len(raw) > MAX_ICON:
        raise HTTPException(status_code=400, detail="Icon is too large (max 2MB)")
    content_type = upload.content_type or ICON_TYPES.get(suffix, "application/octet-stream")
    raw, content_type, suffix = normalize_icon_bytes(raw, suffix, content_type)
    filename = f"{item_id}{suffix}"
    forget_icon(item_id, filename)
    if atlas.USE_MONGODB:
        payload = {
            "_id": item_id,
            "app_id": item_id,
            "name": app_name,
            "filename": filename,
            "content_type": content_type,
            "icon_file": filename,
            "icon_url": "",
            "data": Binary(raw),
        }
        atlas.mongo_db().icons.replace_one({"_id": item_id}, payload, upsert=True)
        remember_icon(payload)
        return filename
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    for old in ICONS_DIR.glob(f"{item_id}.*"):
        old.unlink()
    (ICONS_DIR / filename).write_bytes(raw)
    return filename


def remove_icon_file(filename: str | None) -> None:
    if not filename:
        return
    path = ICONS_DIR / Path(filename).name
    if path.exists() and path.parent.resolve() == ICONS_DIR.resolve():
        path.unlink()


def migrate_file_apps() -> None:
    if not atlas.USE_MONGODB:
        return
    database = atlas.mongo_db()
    if any(database[name].find_one() for name in section_collections().values()):
        return
    seed = load_file_apps()
    if not seed:
        return
    for item in seed:
        item["id"] = item.get("id") or uuid.uuid4().hex[:12]
        item["section"] = clean_section(item.get("section") or "devops-tools")
        item.setdefault("icon", "grafana")
        item.setdefault("icon_file", "")
        item.setdefault("icon_url", "")
        if item.get("icon_file"):
            path = ICONS_DIR / Path(str(item["icon_file"])).name
            if path.exists():
                suffix = path.suffix.lower()
                atlas.mongo_db().icons.replace_one(
                    {"_id": item["id"]},
                    {
                        "_id": item["id"],
                        "app_id": item["id"],
                        "name": item.get("name") or "",
                        "filename": item["icon_file"],
                        "content_type": ICON_TYPES.get(suffix, "application/octet-stream"),
                        "icon_file": item["icon_file"],
                        "icon_url": "",
                        "data": Binary(path.read_bytes()),
                    },
                    upsert=True,
                )
        write_app(item)
    log.info("Moved %s AppHub apps from file into Atlas", len(seed))


def clean_url(value: str) -> str:
    url = (value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")
    return url


def clean_section(value: str) -> str:
    section = (value or "").strip().lower()
    allowed = {row["id"] for row in load_sections()}
    if section not in allowed:
        raise HTTPException(status_code=400, detail="Unknown section")
    return section


def clean_icon(value: str) -> str:
    icon = (value or "custom").strip().lower()
    if icon == "grafana":
        return "grafana"
    if icon != "custom":
        raise HTTPException(status_code=400, detail="Choose an icon URL or upload an image")
    return icon


def clean_icon_url(value: str) -> str:
    url = (value or "").strip()
    if not url:
        return ""
    return clean_url(url)


def clean_name(value: str) -> str:
    name = re.sub(r"\s+", " ", (value or "").strip())
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    # Keep user capitals (AWS, k8s-API); raise the first letter of each word.
    name = re.sub(r"(^|[\s\-_])([a-z])", lambda m: m.group(1) + m.group(2).upper(), name)
    return name[:80]


def clean_section_name(value: str) -> str:
    return clean_name(value)


class LoginIn(BaseModel):
    username: str
    password: str


class SectionIn(BaseModel):
    name: str


def upsert_app(
    name: str,
    url: str,
    section: str,
    icon: str,
    upload: UploadFile | None,
    icon_url: str = "",
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item_id = (existing or {}).get("id") or uuid.uuid4().hex[:12]
    kind = clean_icon(icon)
    filename = (existing or {}).get("icon_file") or ""
    remote = (existing or {}).get("icon_url") or ""
    app_name = clean_name(name)
    if kind == "grafana":
        if filename:
            remove_icon_file(filename)
        if atlas.USE_MONGODB:
            atlas.mongo_db().icons.delete_one({"_id": item_id})
            forget_icon(item_id, filename)
        filename = ""
        remote = ""
    elif upload is not None and upload.filename:
        previous_file = filename
        filename = save_icon_upload(upload, item_id, app_name)
        if previous_file and previous_file != filename:
            remove_icon_file(previous_file)
            forget_icon(item_id, previous_file)
        remote = ""
    else:
        pasted = clean_icon_url(icon_url)
        if pasted:
            if pasted != remote:
                if filename:
                    remove_icon_file(filename)
                forget_icon(item_id, filename)
                filename = ""
            remote = pasted
        elif not filename and not remote:
            raise HTTPException(status_code=400, detail="Paste an icon URL or upload an image")
    return {
        "id": item_id,
        "name": app_name,
        "url": clean_url(url),
        "section": clean_section(section),
        "subtitle": "",
        "icon": kind,
        "icon_file": filename,
        "icon_url": remote,
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(
        STATIC / "index.html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.post("/api/login")
def login(body: LoginIn, request: Request) -> dict[str, Any]:
    if body.username != APP_USER or body.password != APP_PASSWORD:
        raise HTTPException(status_code=401, detail="Wrong username or password")
    request.session["user"] = body.username
    saml_mod.clear_sso_session(request)
    return {"user": body.username, "mongodb": atlas.mongo_status(), **saml_mod.session_user_info(request)}


@app.post("/api/logout")
def logout(request: Request) -> dict[str, bool]:
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
def me(request: Request) -> dict[str, Any]:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")
    return {
        "user": str(user),
        "mongodb": atlas.mongo_status(),
        "aws": cloud_upload.is_cloud_enabled(),
        "cloud": cloud_upload.is_cloud_enabled(),
        "backup_storage_label": cloud_upload.get_backup_storage_label(),
        **saml_mod.session_user_info(request),
    }


@app.get("/api/apps")
def list_apps(request: Request) -> dict[str, Any]:
    require_login(request)
    with _lock:
        sections = load_sections()
        return {
            "apps": load_apps(sections),
            "sections": sections,
            "mongodb": atlas.mongo_status(),
            "aws": cloud_upload.is_cloud_enabled(),
            "cloud": cloud_upload.is_cloud_enabled(),
            "backup_storage_label": cloud_upload.get_backup_storage_label(),
        }


@app.post("/api/apps/backup")
def backup_apps(request: Request) -> dict[str, Any]:
    require_login(request)
    if not cloud_upload.is_cloud_enabled():
        raise HTTPException(status_code=400, detail="Cloud backup is disabled")
    label = cloud_upload.get_backup_storage_label()
    started = time.monotonic()
    with _lock:
        sections = load_sections()
        apps, icons = _run_parallel(lambda: load_apps(sections), snapshot_icons)
        ok, err = cloud_upload.push_snapshot(apps, sections, icons)
    log.info(
        "Backup snapshot %.0fms apps=%s sections=%s icons=%s",
        (time.monotonic() - started) * 1000,
        len(apps),
        len(sections),
        len(icons),
    )
    if not ok:
        raise HTTPException(status_code=500, detail=f"Backup to {label} failed: {err}")
    return {"ok": True, "message": f"Backup completed — saved to {label}"}


@app.post("/api/apps/restore")
def restore_apps(request: Request) -> dict[str, Any]:
    require_login(request, allow_viewer=True)
    if not cloud_upload.is_cloud_enabled():
        raise HTTPException(status_code=400, detail="Cloud restore is disabled")
    label = cloud_upload.get_backup_storage_label()
    payload, err = cloud_upload.fetch_snapshot()
    if err:
        raise HTTPException(status_code=500, detail=f"Restore from {label} failed: {err}")
    with _lock:
        counts = apply_snapshot(payload or {})
    return {
        "ok": True,
        "message": restore_result_message(label, counts),
        **counts,
    }


@app.get("/api/sections")
def list_sections(request: Request) -> dict[str, Any]:
    require_login(request)
    with _lock:
        return {"sections": load_sections()}


@app.post("/api/sections")
def create_section(request: Request, name: str = Form()) -> dict[str, Any]:
    require_login(request)
    with _lock:
        section_name = clean_section_name(name)
        slug = slugify_section(section_name)
        if not slug:
            raise HTTPException(status_code=400, detail="Section name must include letters or numbers")
        existing = load_sections()
        if any(row["id"] == slug or row["name"].lower() == section_name.lower() for row in existing):
            raise HTTPException(status_code=409, detail="That section already exists")
        collection = collection_from_slug(slug)
        used = {row["collection"] for row in existing}
        if collection in used or collection in RESERVED_COLLECTIONS:
            collection = f"tab_{slug.replace('-', '_')}"[:60]
        item = {
            "id": slug,
            "name": section_name,
            "collection": collection,
            "builtin": False,
            "order": max((row.get("order") or 0) for row in existing) + 1 if existing else 0,
        }
        global _sections_cache
        if atlas.USE_MONGODB:
            database = atlas.mongo_db()
            names = set(database.list_collection_names())
            if collection not in names:
                database.create_collection(collection)
            database.sections.replace_one(
                {"_id": item["id"]},
                {"_id": item["id"], **item},
                upsert=True,
            )
            _sections_cache = None
        else:
            rows = existing + [item]
            save_file_payload(load_file_apps(), rows)
            _sections_cache = [dict(row) for row in rows]
        return item


@app.put("/api/sections/{section_id}")
def rename_section(section_id: str, request: Request, name: str = Form()) -> dict[str, Any]:
    require_login(request)
    with _lock:
        rows = load_sections()
        current = next((row for row in rows if row["id"] == section_id), None)
        if not current:
            raise HTTPException(status_code=404, detail="Section not found")
        section_name = clean_section_name(name)
        if any(row["id"] != section_id and row["name"].lower() == section_name.lower() for row in rows):
            raise HTTPException(status_code=409, detail="That section already exists")
        current["name"] = section_name
        global _sections_cache
        if atlas.USE_MONGODB:
            atlas.mongo_db().sections.replace_one(
                {"_id": section_id},
                {"_id": section_id, **current},
                upsert=True,
            )
            _sections_cache = None
        else:
            save_file_payload(load_file_apps(), rows)
            _sections_cache = [dict(row) for row in rows]
        return current


@app.delete("/api/sections/{section_id}")
def delete_section(section_id: str, request: Request) -> dict[str, bool]:
    require_login(request)
    with _lock:
        rows = load_sections()
        current = next((row for row in rows if row["id"] == section_id), None)
        if not current:
            raise HTTPException(status_code=404, detail="Section not found")
        if len(rows) <= 1:
            raise HTTPException(status_code=400, detail="Keep at least one section")
        for app in [row for row in load_apps() if row.get("section") == section_id]:
            delete_stored_app(app)
        leftover = [row for row in rows if row["id"] != section_id]
        if atlas.USE_MONGODB:
            database = atlas.mongo_db()
            database.sections.delete_one({"_id": section_id})
            collection = current.get("collection")
            if collection and collection not in RESERVED_COLLECTIONS and collection in set(database.list_collection_names()):
                database.drop_collection(collection)
            global _sections_cache
            _sections_cache = None
        else:
            save_file_payload(load_file_apps(), leftover)
            _sections_cache = [dict(row) for row in leftover]
        return {"ok": True}


@app.get("/api/icons/{filename}")
def get_icon(filename: str, request: Request) -> Response:
    require_login(request)
    name = Path(filename).name
    cached = cached_icon(name)
    if cached:
        return Response(
            content=cached["data"],
            media_type=cached["content_type"],
            headers={"Cache-Control": "private, max-age=86400"},
        )
    if atlas.USE_MONGODB:
        col = atlas.mongo_db().icons
        doc = col.find_one({"filename": name}) or col.find_one({"_id": Path(name).stem}) or col.find_one({"_id": name})
        if doc and doc.get("data") is None and doc.get("icon_url"):
            try:
                raw, content_type, suffix = fetch_remote_icon(str(doc["icon_url"]))
                stored_name = f"{doc['_id']}{suffix}"
                doc = {
                    **{k: doc.get(k) for k in ("_id", "app_id", "name", "icon_url")},
                    "filename": stored_name,
                    "content_type": content_type,
                    "icon_file": stored_name,
                    "data": Binary(raw),
                }
                col.replace_one({"_id": doc["_id"]}, doc, upsert=True)
            except HTTPException:
                doc = None
        data = (doc or {}).get("data")
        if data:
            remember_icon(doc or {})
            return Response(
                content=bytes(data),
                media_type=str((doc or {}).get("content_type") or "application/octet-stream"),
                headers={"Cache-Control": "private, max-age=86400"},
            )
        raise HTTPException(status_code=404, detail="Icon not found")
    path = ICONS_DIR / name
    if path.parent.resolve() != ICONS_DIR.resolve() or not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Icon not found")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


@app.post("/api/apps")
async def create_app(
    request: Request,
    name: str = Form(),
    url: str = Form(),
    section: str = Form(),
    icon: str = Form("custom"),
    icon_url: str = Form(""),
    icon_file: UploadFile | None = File(None),
) -> dict[str, Any]:
    require_login(request)
    with _lock:
        item = upsert_app(name, url, section, icon, icon_file, icon_url)
        if any(row["url"].rstrip("/") == item["url"].rstrip("/") and row["section"] == item["section"] for row in load_apps()):
            if atlas.USE_MONGODB:
                atlas.mongo_db().icons.delete_one({"_id": item["id"]})
                forget_icon(item["id"], item.get("icon_file"))
            raise HTTPException(status_code=409, detail="This URL is already in that tab")
        write_app(item)
    return item


@app.put("/api/apps/{item_id}")
async def update_app(
    item_id: str,
    request: Request,
    name: str = Form(),
    url: str = Form(),
    section: str = Form(),
    icon: str = Form("custom"),
    icon_url: str = Form(""),
    icon_file: UploadFile | None = File(None),
) -> dict[str, Any]:
    require_login(request)
    with _lock:
        current = find_app(item_id)
        if not current:
            raise HTTPException(status_code=404, detail="App not found")
        item = upsert_app(name, url, section, icon, icon_file, icon_url, current)
        write_app(item, current)
    return item


@app.delete("/api/apps/{item_id}")
def delete_app(item_id: str, request: Request) -> dict[str, bool]:
    require_login(request)
    with _lock:
        current = find_app(item_id)
        if not current:
            raise HTTPException(status_code=404, detail="App not found")
        delete_stored_app(current)
    return {"ok": True}


ensure_collections()
load_sections()
migrate_file_apps()
hydrate_remote_icons()
load_apps()
repair_missing_icons()
cloud_upload.log_cloud_config()

