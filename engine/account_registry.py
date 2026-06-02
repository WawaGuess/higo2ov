"""Account registry with lazy creation and local persistence.

Each Higo user gets their own OpenViking account + USER key.
This module manages the mapping and auto-provisions accounts on first use.
"""

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import TypedDict

from engine.config import OpenVikingConfig
from engine.openviking_client import OpenVikingClient

logger = logging.getLogger(__name__)


class AccountEntry(TypedDict):
    account_id: str
    user_id: str
    api_key: str
    created_at: str


class AccountRegistry:
    """Manages higo_user_id -> OpenViking account + USER key mapping.

    - In-memory client cache for performance
    - JSON file persistence for recovery across restarts
    - asyncio.Lock + atomic file write for concurrency safety
    """

    def __init__(self, registry_path: str, admin_client: OpenVikingClient) -> None:
        self._admin = admin_client
        self._registry_path = registry_path
        self._registry: dict[str, AccountEntry] = self._load()
        self._cache: dict[str, OpenVikingClient] = {}
        self._lock = asyncio.Lock()

    # ---------- persistence ----------

    def _load(self) -> dict[str, AccountEntry]:
        try:
            with open(self._registry_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
                logger.warning("[account_registry] registry file is not a dict, starting fresh")
        except FileNotFoundError:
            pass
        except json.JSONDecodeError as e:
            logger.warning("[account_registry] failed to parse registry: %s", e)
        return {}

    def _persist(self) -> None:
        """Atomic write: tmp file -> os.replace."""
        dir_name = os.path.dirname(os.path.abspath(self._registry_path)) or "."
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._registry, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self._registry_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ---------- public API ----------

    async def get_client(self, higo_user_id: str) -> OpenVikingClient:
        """Return an OpenVikingClient configured with the user's own USER key.

        Creates the account lazily on first encounter.
        """
        if higo_user_id in self._cache:
            return self._cache[higo_user_id]

        entry = self._registry.get(higo_user_id)
        if not entry:
            entry = await self._lazy_create(higo_user_id)

        config = OpenVikingConfig(
            base_url=self._admin.config.base_url,
            api_key=entry["api_key"],
            agent_id=self._admin.config.agent_id,
        )
        client = OpenVikingClient(config)
        self._cache[higo_user_id] = client
        return client

    # ---------- lazy creation ----------

    async def _lazy_create(self, higo_user_id: str) -> AccountEntry:
        async with self._lock:
            # Double-check after acquiring lock
            entry = self._registry.get(higo_user_id)
            if entry:
                return entry

            account_id = higo_user_id

            # 1. Ensure account exists
            account_exists = await self._account_exists(account_id)
            if not account_exists:
                await self._admin.request(
                    "/api/v1/admin/accounts",
                    {
                        "method": "POST",
                        "body": {
                            "account_id": account_id,
                            "admin_user_id": "default",
                        },
                    },
                )
                logger.info("[account_registry] created account %s", account_id)

            # 2. Obtain USER key for default user.
            # OpenViking auto-creates the admin_user_id on account creation, so
            # POST /users may return 409. We try register first, then fall back
            # to regenerating the key. Either path yields a valid user_key.
            try:
                reg_resp = await self._admin.request(
                    f"/api/v1/admin/accounts/{account_id}/users",
                    {
                        "method": "POST",
                        "body": {"user_id": "default", "role": "user"},
                    },
                )
                api_key = reg_resp["user_key"]
                logger.info("[account_registry] registered user default for %s", account_id)
            except Exception as reg_err:
                if "409" in str(reg_err) or "Conflict" in str(reg_err):
                    logger.warning(
                        "[account_registry] user default already exists for %s (409), regenerating key",
                        account_id,
                    )
                    key_resp = await self._admin.request(
                        f"/api/v1/admin/accounts/{account_id}/users/default/key",
                        {"method": "POST"},
                    )
                    api_key = key_resp["user_key"]
                    logger.info("[account_registry] regenerated key for %s/default", account_id)
                else:
                    raise

            entry = AccountEntry(
                account_id=account_id,
                user_id="default",
                api_key=api_key,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            self._registry[higo_user_id] = entry
            self._persist()
            return entry

    # ---------- helpers ----------

    async def _account_exists(self, account_id: str) -> bool:
        try:
            resp = await self._admin.request(
                "/api/v1/admin/accounts",
                {"method": "GET"},
            )
            accounts = resp if isinstance(resp, list) else []
            return any(a.get("account_id") == account_id for a in accounts)
        except Exception as e:
            logger.warning("[account_registry] failed to list accounts: %s", e)
            return False

    async def _user_exists(self, account_id: str, user_id: str) -> bool:
        try:
            resp = await self._admin.request(
                f"/api/v1/admin/accounts/{account_id}/users",
                {"method": "GET"},
            )
            users = resp.get("users", []) if isinstance(resp, dict) else []
            return any(u.get("user_id") == user_id for u in users)
        except Exception as e:
            logger.warning(
                "[account_registry] failed to list users for %s: %s", account_id, e
            )
            return False
