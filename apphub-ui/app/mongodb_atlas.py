"""MongoDB Atlas connection. Same pattern as CronBoard and k8s-ipam."""

from __future__ import annotations

import logging
import os
import time
from typing import Any
from urllib.parse import quote_plus

from pymongo import MongoClient

log = logging.getLogger("apphub")

USE_MONGODB = os.environ.get("USE_MONGODB", "false").strip().lower() in {"1", "true", "yes", "on"}
MONGO_URI = os.environ.get("MONGO_URI", "").strip()
MONGO_HOST = os.environ.get("MONGO_HOST", "mongo").strip() or "mongo"
MONGO_DB_NAME = os.environ.get("MONGO_DB", "apphub").strip() or "apphub"
MONGO_USER = os.environ.get("MONGO_INITDB_ROOT_USERNAME", "").strip()
MONGO_PASSWORD = os.environ.get("MONGO_INITDB_ROOT_PASSWORD", "").strip()

_mongo: MongoClient | None = None
_status_cache: dict[str, Any] = {"at": 0.0, "data": None}


def using_atlas() -> bool:
    host = MONGO_HOST.lower()
    uri = MONGO_URI.lower()
    return (
        "mongodb+srv://" in uri
        or "mongodb.net" in uri
        or "mongodb.net" in host
        or host.startswith("mongodb+srv://")
    )


def mongo_uri() -> str:
    if MONGO_URI:
        return MONGO_URI
    user = quote_plus(MONGO_USER)
    password = quote_plus(MONGO_PASSWORD)
    host = MONGO_HOST.strip()
    if host.startswith("mongodb+srv://") or host.startswith("mongodb://"):
        return host
    if using_atlas():
        return f"mongodb+srv://{user}:{password}@{host}/{MONGO_DB_NAME}"
    return f"mongodb://{user}:{password}@{host}:27017/{MONGO_DB_NAME}?authSource=admin"


def mongo() -> MongoClient:
    global _mongo
    if _mongo is None:
        _mongo = MongoClient(
            mongo_uri(),
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            minPoolSize=1,
        )
    return _mongo


def mongo_db():
    return mongo()[MONGO_DB_NAME]


def mongo_status() -> dict[str, bool | str]:
    now = time.monotonic()
    cached = _status_cache["data"]
    if cached is not None and now - _status_cache["at"] < 10:
        return cached
    if not USE_MONGODB:
        status = {"name": "mongodb", "connected": False}
    else:
        name = "mongodb-atlas" if using_atlas() else "mongodb"
        try:
            mongo().admin.command("ping")
            status = {"name": name, "connected": True}
        except Exception:
            status = {"name": name, "connected": False}
    _status_cache["at"] = now
    _status_cache["data"] = status
    return status


def wait_for_mongo(tries: int = 30) -> None:
    if using_atlas():
        log.info("Connecting to MongoDB Atlas database %s", MONGO_DB_NAME)
    else:
        log.info("Connecting to MongoDB database %s", MONGO_DB_NAME)
    last_error: Exception | None = None
    for attempt in range(1, tries + 1):
        try:
            mongo().admin.command("ping")
            log.info("MongoDB ping ok (%s/%s)", attempt, tries)
            _status_cache["at"] = time.monotonic()
            _status_cache["data"] = {
                "name": "mongodb-atlas" if using_atlas() else "mongodb",
                "connected": True,
            }
            return
        except Exception as exc:
            last_error = exc
            log.warning("MongoDB not ready yet (%s/%s): %s", attempt, tries, exc)
            time.sleep(1)
    raise RuntimeError(f"MongoDB is not reachable: {last_error}") from last_error
