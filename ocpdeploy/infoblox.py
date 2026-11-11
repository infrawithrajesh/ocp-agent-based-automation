"""Idempotent A / PTR record management through the Infoblox WAPI."""
from __future__ import annotations

import logging
import os

import requests

from .config import env_verify

log = logging.getLogger("infoblox")


class InfobloxError(Exception):
    pass


class Infoblox:
    def __init__(self, view: str = "default", ttl: int | None = None):
        host = os.environ.get("INFOBLOX_HOST")
        user = os.environ.get("INFOBLOX_USER")
        pw = os.environ.get("INFOBLOX_PASSWORD")
        if not (host and user and pw):
            raise InfobloxError("INFOBLOX_HOST / INFOBLOX_USER / INFOBLOX_PASSWORD must be set in .env")
        ver = os.environ.get("INFOBLOX_WAPI_VERSION", "v2.12")
        self.base = f"https://{host}/wapi/{ver}"
        self.view, self.ttl = view, ttl
        self.s = requests.Session()
        self.s.auth = (user, pw)
        self.s.verify = env_verify("INFOBLOX_VERIFY_SSL", "false")

    def _req(self, method: str, path: str, **kw):
        r = self.s.request(method, f"{self.base}/{path}", timeout=30, **kw)
        if not r.ok:
            raise InfobloxError(f"{method} {path}: HTTP {r.status_code} {r.text[:300]}")
        return r.json() if r.text else None

    def ping(self) -> None:
        self._req("GET", "grid", params={"_max_results": 1})

    def _ttl(self) -> dict:
        return {"ttl": self.ttl, "use_ttl": True} if self.ttl else {}

    def ensure_a(self, name: str, ip: str) -> str:
        found = self._req("GET", "record:a", params={"name": name, "view": self.view})
        if any(r["ipv4addr"] == ip for r in found):
            return "unchanged"
        if found:  # same name, wrong IP -> repoint the first, leave a clean single record
            self._req("PUT", found[0]["_ref"], json={"ipv4addr": ip})
            for extra in found[1:]:
                self._req("DELETE", extra["_ref"])
            return "updated"
        self._req("POST", "record:a", json={"name": name, "ipv4addr": ip, "view": self.view, **self._ttl()})
        return "created"

    def ensure_ptr(self, ip: str, name: str) -> str:
        found = self._req("GET", "record:ptr", params={"ipv4addr": ip, "view": self.view})
        if any(r["ptrdname"] == name for r in found):
            return "unchanged"
        if found:
            self._req("PUT", found[0]["_ref"], json={"ptrdname": name})
            return "updated"
        self._req("POST", "record:ptr", json={"ptrdname": name, "ipv4addr": ip, "view": self.view, **self._ttl()})
        return "created"

    def delete_a(self, name: str) -> int:
        found = self._req("GET", "record:a", params={"name": name, "view": self.view})
        for r in found:
            self._req("DELETE", r["_ref"])
        return len(found)

    def delete_ptr(self, ip: str, name: str) -> int:
        found = self._req("GET", "record:ptr", params={"ipv4addr": ip, "ptrdname": name, "view": self.view})
        for r in found:
            self._req("DELETE", r["_ref"])
        return len(found)


def records(cfg) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return (A records as (name, ip), PTR records as (ip, name)) the cluster needs."""
    a = [(f"api.{cfg.domain}", cfg.api_vip),
         (f"api-int.{cfg.domain}", cfg.api_vip),
         (f"*.apps.{cfg.domain}", cfg.ingress_vip)]
    ptr = [(cfg.api_vip, f"api.{cfg.domain}")]
    for n in cfg.nodes:
        a.append((cfg.fqdn(n), n.ip))
        ptr.append((n.ip, cfg.fqdn(n)))
    return a, ptr
