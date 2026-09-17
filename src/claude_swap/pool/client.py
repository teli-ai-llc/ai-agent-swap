"""Supabase Auth + PostgREST over urllib, for one signed-in pool member.

Every method takes the session explicitly and never stores it; callers own
persistence (``session.save_session``). ``transport`` is injectable so the
whole client is testable against ``tests/pool/fake_pool.FakePool``.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, replace
from urllib.parse import urlencode

from claude_swap.exceptions import PoolAuthError, PoolError
from claude_swap.pool.blob import validate_blob
from claude_swap.pool.session import PoolSession

_logger = logging.getLogger("claude-swap")

Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]

REFRESH_WINDOW_S = 300.0
_USER_AGENT = "claude-swap-pool/1"


def _urllib_transport(timeout_s: float) -> Transport:
    def send(method: str, url: str, headers: dict[str, str], body: bytes | None):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read() if hasattr(e, "read") else b""
    return send


def _num_or_none(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class PoolAccountRow:
    id: str
    account_uuid: str
    organization_uuid: str
    email: str
    organization_name: str
    owner_user_id: str
    credential: dict | None
    credential_version: int
    credential_fingerprint: str | None
    status: str
    shared: bool
    share_swap_limit: float | None
    share_hard_limit: float | None
    updated_at: str
    updated_by_user_id: str | None = None
    updated_by_machine_id: str | None = None
    needs_relogin_since: str | None = None
    needs_relogin_reported_by: str | None = None

    @classmethod
    def from_json(cls, d: dict) -> PoolAccountRow:
        credential = d.get("credential")
        if credential is not None:
            try:
                validate_blob(credential)
            except PoolError as e:
                _logger.warning("pool row %s carries an invalid credential (%s); ignoring it", d.get("id"), e)
                credential = None
        try:
            version = int(d.get("credential_version") or 0)
        except (TypeError, ValueError):
            version = 0
        return cls(
            id=str(d["id"]),
            account_uuid=str(d["account_uuid"]),
            organization_uuid=str(d.get("organization_uuid") or ""),
            email=str(d.get("email") or ""),
            organization_name=str(d.get("organization_name") or ""),
            owner_user_id=str(d["owner_user_id"]),
            credential=credential,
            credential_version=version,
            credential_fingerprint=d.get("credential_fingerprint"),
            status=str(d.get("status") or "ok"),
            shared=bool(d.get("shared")),
            share_swap_limit=_num_or_none(d.get("share_swap_limit")),
            share_hard_limit=_num_or_none(d.get("share_hard_limit")),
            updated_at=str(d.get("updated_at") or ""),
            updated_by_user_id=d.get("updated_by_user_id"),
            updated_by_machine_id=d.get("updated_by_machine_id"),
            needs_relogin_since=d.get("needs_relogin_since"),
            needs_relogin_reported_by=d.get("needs_relogin_reported_by"),
        )


class PoolClient:
    def __init__(
        self,
        url: str,
        anon_key: str,
        *,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.time,
        timeout_s: float = 10.0,
    ):
        self.url = url.rstrip("/")
        self.anon_key = anon_key
        self._send = transport or _urllib_transport(timeout_s)
        self._clock = clock

    # -- auth -----------------------------------------------------------------
    def _auth_post(self, grant: str, payload: dict) -> dict:
        status, body = self._call(
            "POST", f"{self.url}/auth/v1/token?grant_type={grant}",
            {"apikey": self.anon_key, "Content-Type": "application/json",
             "User-Agent": _USER_AGENT},
            json.dumps(payload).encode(),
        )
        data = self._decode(body)
        if status in (400, 401, 403):
            detail = data.get("error_description") or data.get("message") or data.get("error") or "refused"
            raise PoolAuthError(f"pool sign-in refused: {detail}")
        if status >= 300:
            raise PoolError(f"pool auth endpoint answered HTTP {status}")
        return data

    def _session_from(self, data: dict) -> PoolSession:
        try:
            return PoolSession(
                url=self.url, anon_key=self.anon_key,
                access_token=data["access_token"], refresh_token=data["refresh_token"],
                expires_at=self._clock() + float(data["expires_in"]),
                user_id=data["user"]["id"], email=data["user"].get("email") or "",
            )
        except (KeyError, TypeError, ValueError) as e:
            raise PoolError(f"pool auth response is incomplete: {e}") from None

    def sign_in_password(self, email: str, password: str) -> PoolSession:
        return self._session_from(self._auth_post("password", {"email": email, "password": password}))

    def ensure_fresh(self, session: PoolSession) -> PoolSession:
        """The same session when its JWT has more than 300 s left, else a
        refreshed one. ``PoolAuthError`` when the refresh token is refused."""
        if session.expires_at - self._clock() > REFRESH_WINDOW_S:
            return session
        data = self._auth_post("refresh_token", {"refresh_token": session.refresh_token})
        return self._session_from(data)

    # -- rest -------------------------------------------------------------------
    def _call(self, method, url, headers, body):
        try:
            return self._send(method, url, headers, body)
        except PoolError:
            raise
        except Exception as e:  # network, TLS, timeout
            raise PoolError(f"pool unreachable: {type(e).__name__}: {e}") from None

    @staticmethod
    def _decode(body: bytes) -> dict:
        if not body:
            return {}
        try:
            parsed = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {"rows": parsed}

    def _rest(self, session: PoolSession, method: str, table: str, *,
              params: dict | None = None, body: object = None, prefer: str | None = None) -> list:
        query = urlencode(params or {}, safe="*.,:-")
        url = f"{self.url}/rest/v1/{table}" + (f"?{query}" if query else "")
        headers = {
            "apikey": self.anon_key,
            "Authorization": f"Bearer {session.access_token}",
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        }
        raw = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            raw = json.dumps(body).encode()
        if prefer:
            headers["Prefer"] = prefer
        status, payload = self._call(method, url, headers, raw)
        if status == 401:
            raise PoolAuthError("pool session refused; run: cswap pool login")
        if status >= 300:
            detail = self._decode(payload).get("message") or payload[:200].decode("utf-8", "replace")
            raise PoolError(f"pool {method} {table} answered HTTP {status}: {detail}")
        if not payload:
            return []
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except ValueError:
            raise PoolError(f"pool {method} {table} answered non-JSON") from None
        return parsed if isinstance(parsed, list) else [parsed]

    def schema_version(self, session: PoolSession) -> int:
        rows = self._rest(session, "GET", "pool_meta", params={"key": "eq.schema_version", "select": "value"})
        if not rows:
            raise PoolError("pool schema is not installed (no pool_meta.schema_version)")
        try:
            return int(rows[0]["value"])
        except (KeyError, TypeError, ValueError):
            raise PoolError("pool_meta.schema_version is not a number") from None

    def member(self, session: PoolSession) -> dict:
        rows = self._rest(session, "GET", "pool_members",
                          params={"user_id": f"eq.{session.user_id}", "select": "user_id,display_name,role"})
        if not rows:
            raise PoolAuthError("your login has no pool_members row; ask the pool admin to add you")
        return {"user_id": rows[0]["user_id"], "display_name": rows[0].get("display_name") or "",
                "role": rows[0].get("role") or "member"}

    def register_machine(self, session: PoolSession, machine_id: str, hostname: str, version: str) -> None:
        self._rest(session, "POST", "pool_machines",
                   params={"on_conflict": "machine_id"},
                   body={"machine_id": machine_id, "user_id": session.user_id,
                         "hostname": hostname, "cswap_version": version},
                   prefer="resolution=merge-duplicates,return=representation")

    def list_accounts(self, session: PoolSession, since: str | None = None) -> list[PoolAccountRow]:
        params = {"select": "*", "order": "updated_at.asc"}
        if since:
            params["updated_at"] = f"gt.{since}"
        return [PoolAccountRow.from_json(r) for r in self._rest(session, "GET", "pool_accounts", params=params)]

    def find_account(self, session: PoolSession, account_uuid: str, organization_uuid: str) -> PoolAccountRow | None:
        rows = self._rest(session, "GET", "pool_accounts", params={
            "select": "*", "account_uuid": f"eq.{account_uuid}",
            "organization_uuid": f"eq.{organization_uuid}",
        })
        return PoolAccountRow.from_json(rows[0]) if rows else None

    def get_account(self, session: PoolSession, account_id: str) -> PoolAccountRow | None:
        rows = self._rest(session, "GET", "pool_accounts", params={"select": "*", "id": f"eq.{account_id}"})
        return PoolAccountRow.from_json(rows[0]) if rows else None

    def create_account(self, session: PoolSession, fields: dict) -> PoolAccountRow:
        rows = self._rest(session, "POST", "pool_accounts", body=fields, prefer="return=representation")
        if not rows:
            raise PoolError("pool did not return the created account row")
        return PoolAccountRow.from_json(rows[0])

    def push_credential(self, session: PoolSession, account_id: str, blob: dict, version: int,
                        fingerprint: str, machine_id: str) -> bool:
        validate_blob(blob)
        rows = self._rest(
            session, "PATCH", "pool_accounts",
            params={"id": f"eq.{account_id}", "credential_version": f"lt.{version}"},
            body={"credential": blob, "credential_version": version,
                  "credential_fingerprint": fingerprint, "updated_by_machine_id": machine_id},
            prefer="return=representation",
        )
        return len(rows) == 1

    def update_sharing(self, session: PoolSession, account_id: str, *, shared: bool,
                       swap_limit: float | None, hard_limit: float | None) -> None:
        rows = self._rest(
            session, "PATCH", "pool_accounts", params={"id": f"eq.{account_id}"},
            body={"shared": shared, "share_swap_limit": swap_limit, "share_hard_limit": hard_limit},
            prefer="return=representation",
        )
        if not rows:
            raise PoolError("pool account not found or not yours")

    def set_status(self, session: PoolSession, account_id: str, status: str, machine_id: str) -> None:
        body: dict = {"status": status}
        if status == "needs_relogin":
            body["needs_relogin_reported_by"] = machine_id
        rows = self._rest(session, "PATCH", "pool_accounts", params={"id": f"eq.{account_id}"},
                          body=body, prefer="return=representation")
        if not rows:
            raise PoolError("pool account not found or not visible")
