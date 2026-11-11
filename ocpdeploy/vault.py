"""Minimal HashiCorp Vault KV v2 reader (token or AppRole auth)."""
from __future__ import annotations

import os

import requests

from .config import env_verify


class VaultError(Exception):
    pass


class Vault:
    def __init__(self):
        self.addr = os.environ.get("VAULT_ADDR", "").rstrip("/")
        if not self.addr:
            raise VaultError("VAULT_ADDR is not set")
        self.s = requests.Session()
        self.s.verify = env_verify("VAULT_VERIFY_SSL", "true")
        if os.environ.get("VAULT_NAMESPACE"):
            self.s.headers["X-Vault-Namespace"] = os.environ["VAULT_NAMESPACE"]
        token = os.environ.get("VAULT_TOKEN")
        if not token:
            role, secret = os.environ.get("VAULT_ROLE_ID"), os.environ.get("VAULT_SECRET_ID")
            if not (role and secret):
                raise VaultError("set VAULT_TOKEN, or VAULT_ROLE_ID + VAULT_SECRET_ID")
            r = self.s.post(f"{self.addr}/v1/auth/approle/login",
                            json={"role_id": role, "secret_id": secret}, timeout=15)
            if not r.ok:
                raise VaultError(f"AppRole login failed: {r.status_code} {r.text[:200]}")
            token = r.json()["auth"]["client_token"]
        self.s.headers["X-Vault-Token"] = token

    def read(self, mount: str, path: str) -> dict:
        r = self.s.get(f"{self.addr}/v1/{mount}/data/{path}", timeout=15)
        if not r.ok:
            raise VaultError(f"read {mount}/{path}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()["data"]["data"]

    def bmc_creds(self, mount: str, path: str) -> tuple[str, str]:
        d = self.read(mount, path)
        try:
            return d["username"], d["password"]
        except KeyError:
            raise VaultError(f"{mount}/{path} must contain keys 'username' and 'password'")
