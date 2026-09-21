"""In-memory stand-in for the Supabase endpoints PoolClient uses.

A ``Transport`` callable plus a tiny PostgREST: eq/lt/gt filters, select,
insert, upsert, patch with representation. It also mirrors the guard
triggers from ``supabase/migrations/0001_pool.sql`` so the client tests see
the same refusals a real project would give.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlsplit


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass
class FakeUser:
    user_id: str
    email: str
    password: str
    role: str = "member"
    display_name: str = ""


@dataclass
class FakePool:
    """``transport(method, url, headers, body) -> (status, bytes)``."""

    base_url: str = "https://fake.supabase.co"
    anon_key: str = "anon-key"
    schema_version: str = "1"
    jwt_ttl_s: float = 3600.0
    users: dict[str, FakeUser] = field(default_factory=dict)
    accounts: dict[str, dict] = field(default_factory=dict)      # id -> row
    machines: dict[str, dict] = field(default_factory=dict)      # machine_id -> row
    tokens: dict[str, tuple[str, float]] = field(default_factory=dict)  # jwt -> (user_id, expires_at)
    refresh_tokens: dict[str, str] = field(default_factory=dict)  # rt -> user_id
    calls: list[tuple[str, str]] = field(default_factory=list)
    clock: object = time.time
    offline: bool = False
    #: Email-code sign-in (``/auth/v1/otp`` + ``/auth/v1/verify``). A domain
    #: list mirrors the ``pool_signup_guard`` trigger: when non-empty, a
    #: first-time email outside it fails the way GoTrue reports a trigger
    #: exception; when empty, anyone is created (signups open, no guard).
    signup_domains: list[str] = field(default_factory=list)
    codes: dict[str, str] = field(default_factory=dict)          # email -> pending code
    sent_codes: list[str] = field(default_factory=list)          # every email a code went to
    mailer_limited: bool = False                                 # 429 on /otp

    # -- setup helpers ------------------------------------------------------
    def add_member(self, email: str, password: str = "pw", *, role: str = "member",
                   display_name: str | None = None) -> FakeUser:
        user = FakeUser(str(uuid.uuid4()), email, password, role, display_name or email.split("@")[0])
        self.users[user.user_id] = user
        return user

    def row(self, account_id: str) -> dict:
        return self.accounts[account_id]

    def rows(self) -> list[dict]:
        return list(self.accounts.values())

    def session_for(self, user: FakeUser):
        """A ready PoolSession for tests that skip sign-in."""
        from claude_swap.pool.session import PoolSession
        jwt = f"jwt-{uuid.uuid4()}"
        rt = f"rt-{uuid.uuid4()}"
        exp = self.clock() + self.jwt_ttl_s
        self.tokens[jwt] = (user.user_id, exp)
        self.refresh_tokens[rt] = user.user_id
        return PoolSession(self.base_url, self.anon_key, jwt, rt, exp, user.user_id, user.email)

    # -- transport ------------------------------------------------------------
    def __call__(self, method: str, url: str, headers: dict, body: bytes | None):
        self.calls.append((method, url))
        if self.offline:
            raise OSError("fake pool offline")
        parts = urlsplit(url)
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        payload = json.loads(body.decode()) if body else None
        if headers.get("apikey") != self.anon_key:
            return 401, b'{"message":"bad apikey"}'
        if parts.path == "/auth/v1/token":
            return self._auth(params.get("grant_type"), payload)
        if parts.path == "/auth/v1/otp":
            return self._otp(payload or {})
        if parts.path == "/auth/v1/verify":
            return self._verify(payload or {})
        if parts.path.startswith("/rest/v1/"):
            auth = headers.get("Authorization", "")
            jwt = auth.removeprefix("Bearer ").strip()
            entry = self.tokens.get(jwt)
            if entry is None or entry[1] <= self.clock():
                return 401, b'{"message":"JWT expired"}'
            return self._rest(entry[0], method, parts.path.removeprefix("/rest/v1/"), params, headers, payload)
        return 404, b"{}"

    def _auth(self, grant: str | None, payload: dict | None):
        if grant == "password":
            for user in self.users.values():
                if user.email == payload.get("email") and user.password == payload.get("password"):
                    return 200, self._token_response(user)
            return 400, b'{"error":"invalid_grant","error_description":"Invalid login credentials"}'
        if grant == "refresh_token":
            user_id = self.refresh_tokens.pop(payload.get("refresh_token", ""), None)
            if user_id is None:
                return 400, b'{"error":"invalid_grant","error_description":"Invalid Refresh Token"}'
            return 200, self._token_response(self.users[user_id])
        return 400, b'{"error":"unsupported_grant_type"}'

    def _otp(self, payload: dict):
        email = (payload.get("email") or "").strip().lower()
        if not email or "@" not in email:
            return 400, b'{"code":400,"error_code":"validation_failed","msg":"Unable to validate email address: invalid format"}'
        if self.mailer_limited:
            return 429, b'{"code":429,"error_code":"over_email_send_rate_limit","msg":"email rate limit exceeded"}'
        user = next((u for u in self.users.values() if u.email == email), None)
        if user is None:
            if not payload.get("create_user", True):
                return 422, b'{"code":422,"error_code":"otp_disabled","msg":"Signups not allowed for otp"}'
            domain = email.rsplit("@", 1)[1]
            if self.signup_domains and domain not in self.signup_domains:
                # What hosted GoTrue answers when pool_signup_guard raises
                # (captured from the real project, 2026-09-21).
                return 500, json.dumps({
                    "code": "23514",
                    "message": f"pool: sign-ups from @{domain} are not allowed (pool_meta.signup_domains)",
                }).encode()
            self.add_member(email, password="")
        code = f"{len(self.sent_codes) + 1:06d}"
        self.codes[email] = code
        self.sent_codes.append(email)
        return 200, b"{}"

    def _verify(self, payload: dict):
        email = (payload.get("email") or "").strip().lower()
        if payload.get("type") != "email":
            return 400, b'{"code":400,"error_code":"validation_failed","msg":"Verify requires a verification type"}'
        if email and self.codes.get(email) == payload.get("token"):
            del self.codes[email]
            user = next(u for u in self.users.values() if u.email == email)
            return 200, self._token_response(user)
        return 403, b'{"code":403,"error_code":"otp_expired","msg":"Token has expired or is invalid"}'

    def _token_response(self, user: FakeUser) -> bytes:
        jwt = f"jwt-{uuid.uuid4()}"
        rt = f"rt-{uuid.uuid4()}"
        self.tokens[jwt] = (user.user_id, self.clock() + self.jwt_ttl_s)
        self.refresh_tokens[rt] = user.user_id
        return json.dumps({
            "access_token": jwt, "refresh_token": rt, "token_type": "bearer",
            "expires_in": int(self.jwt_ttl_s),
            "user": {"id": user.user_id, "email": user.email},
        }).encode()

    # -- rest -------------------------------------------------------------------
    def _table(self, name: str) -> dict[str, dict]:
        if name == "pool_accounts":
            return self.accounts
        if name == "pool_machines":
            return self.machines
        if name == "pool_meta":
            return {"schema_version": {"key": "schema_version", "value": self.schema_version}}
        if name == "pool_members":
            return {
                u.user_id: {"user_id": u.user_id, "display_name": u.display_name,
                            "role": u.role, "created_at": "2026-01-01T00:00:00Z"}
                for u in self.users.values()
            }
        raise KeyError(name)

    @staticmethod
    def _matches(row: dict, params: dict) -> bool:
        for key, raw in params.items():
            if key in ("select", "order", "on_conflict", "limit"):
                continue
            op, _, value = raw.partition(".")
            actual = row.get(key)
            if op == "eq":
                if str(actual) != value:
                    return False
            elif op == "lt":
                if actual is None or not (float(actual) < float(value)):
                    return False
            elif op == "gt":
                if actual is None or not (str(actual) > value if isinstance(actual, str) else float(actual) > float(value)):
                    return False
            else:
                raise AssertionError(f"fake pool: unsupported filter {key}={raw}")
        return True

    def _rest(self, user_id: str, method: str, table: str, params: dict, headers: dict, payload):
        try:
            rows = self._table(table)
        except KeyError:
            return 404, json.dumps({"message": f"relation {table} does not exist"}).encode()
        is_admin = self.users[user_id].role == "admin"

        if method == "GET":
            visible = [
                r for r in rows.values()
                if table != "pool_accounts" or r["shared"] or r["owner_user_id"] == user_id or is_admin
            ]
            out = [r for r in visible if self._matches(r, params)]
            if params.get("order", "").startswith("updated_at"):
                out.sort(key=lambda r: r["updated_at"])
            return 200, json.dumps(out).encode()

        if method == "POST":
            if table == "pool_machines":
                row = {"last_seen_at": _now_iso(), **payload}
                if row["user_id"] != user_id:
                    return 403, b'{"message":"row-level security"}'
                self.machines[row["machine_id"]] = row
                return 201, json.dumps([row]).encode()
            if table != "pool_accounts":
                return 405, b"{}"
            if payload.get("owner_user_id") != user_id and not is_admin:
                return 403, b'{"message":"pool: an account can only be published by its owner"}'
            for existing in self.accounts.values():
                if (existing["account_uuid"], existing["organization_uuid"]) == (
                    payload["account_uuid"], payload.get("organization_uuid", "")
                ):
                    return 409, b'{"message":"duplicate key value violates unique constraint"}'
            row = {
                "id": str(uuid.uuid4()), "organization_uuid": "", "organization_name": "",
                "credential": None, "credential_version": 0, "credential_fingerprint": None,
                "updated_by_machine_id": None, "status": "ok", "needs_relogin_since": None,
                "needs_relogin_reported_by": None, "shared": False,
                "share_swap_limit": None, "share_hard_limit": None,
                "created_at": _now_iso(),
                **payload,
                "updated_by_user_id": user_id, "updated_at": _now_iso(),
            }
            self.accounts[row["id"]] = row
            return 201, json.dumps([row]).encode()

        if method == "PATCH":
            if table != "pool_accounts":
                return 405, b"{}"
            changed = []
            for row in list(rows.values()):
                if not (row["shared"] or row["owner_user_id"] == user_id or is_admin):
                    continue
                if not self._matches(row, params):
                    continue
                new = {**row, **payload}
                status, err = self._guard(user_id, is_admin, row, new)
                if status != 200:
                    return status, err
                row.update(new)
                changed.append(row)
            return 200, json.dumps(changed).encode()

        return 405, b"{}"

    def _guard(self, user_id: str, is_admin: bool, old: dict, new: dict):
        """Mirror of pool_accounts_guard().

        Order (matches the corrected SQL trigger, not the docstring order
        an earlier draft used): stale-version check first; then the
        owner-only column/status checks, evaluated against the status the
        client sent; only then the version-heal (status -> ok, clear
        needs_relogin fields); finally the needs_relogin_since stamp.
        """
        is_owner = old["owner_user_id"] == user_id

        if new["credential_version"] < old["credential_version"]:
            return 400, b'{"code":"23514","message":"pool: stale credential version"}'

        if not (is_owner or is_admin):
            for col in ("owner_user_id", "shared", "share_swap_limit", "share_hard_limit",
                        "email", "account_uuid", "organization_uuid"):
                if new.get(col) != old.get(col):
                    return 403, b'{"code":"42501","message":"pool: only the owner may change sharing or identity"}'
            if new["status"] != old["status"] and not (new["status"] == "needs_relogin" and old["status"] == "ok"):
                return 403, b'{"code":"42501","message":"pool: only the owner may set status"}'

        if new["credential_version"] > old["credential_version"]:
            new["updated_by_user_id"] = user_id
            if old["status"] == "needs_relogin":
                new["status"] = "ok"
                new["needs_relogin_since"] = None
                new["needs_relogin_reported_by"] = None

        if new["status"] == "needs_relogin" and old["status"] != "needs_relogin":
            new["needs_relogin_since"] = new.get("needs_relogin_since") or _now_iso()
        new["updated_at"] = _now_iso()
        return 200, b""
