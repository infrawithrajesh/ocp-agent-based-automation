"""DMTF Redfish client: mount an ISO as virtual CD, one-time boot from it, power cycle.

Uses only standard schema (discovered from the service root), which covers
Dell iDRAC9, HPE iLO5/6, Lenovo XCC and Supermicro X12+ firmware.
"""
from __future__ import annotations

import logging
import time

import requests
import urllib3

log = logging.getLogger("redfish")


class RedfishError(Exception):
    pass


class Redfish:
    def __init__(self, host: str, user: str, password: str, verify=False, label: str = ""):
        self.base = f"https://{host}"
        self.label = label or host
        self.s = requests.Session()
        self.s.auth = (user, password)
        self.s.verify = verify
        self.s.headers["Content-Type"] = "application/json"
        if verify is False:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self._system = None

    # ---- http ----
    def _req(self, method: str, path: str, ok=(200, 201, 202, 204), **kw):
        r = self.s.request(method, self.base + path, timeout=60, **kw)
        if r.status_code not in ok:
            raise RedfishError(f"[{self.label}] {method} {path}: HTTP {r.status_code} {r.text[:300]}")
        return r

    def get(self, path: str) -> dict:
        return self._req("GET", path).json()

    def _first_member(self, collection: str) -> str:
        members = self.get(collection).get("Members", [])
        if not members:
            raise RedfishError(f"[{self.label}] {collection} has no members")
        return members[0]["@odata.id"]

    # ---- system ----
    @property
    def system_path(self) -> str:
        if not self._system:
            self._system = self._first_member("/redfish/v1/Systems")
        return self._system

    def system(self) -> dict:
        return self.get(self.system_path)

    def power_state(self) -> str:
        return self.system().get("PowerState", "Unknown")

    def reset(self, reset_type: str) -> None:
        target = self.system()["Actions"]["#ComputerSystem.Reset"]["target"]
        self._req("POST", target, json={"ResetType": reset_type})

    def power_cycle(self, timeout: int = 180) -> None:
        """Hard off (if on), wait, then on. More reliable than ForceRestart across vendors."""
        if self.power_state() != "Off":
            self.reset("ForceOff")
            end = time.time() + timeout
            while self.power_state() != "Off":
                if time.time() > end:
                    raise RedfishError(f"[{self.label}] did not power off in {timeout}s")
                time.sleep(3)
        time.sleep(2)
        self.reset("On")

    def set_boot_once_cd(self) -> None:
        body = {"Boot": {"BootSourceOverrideEnabled": "Once", "BootSourceOverrideTarget": "Cd"}}
        r = self.s.patch(self.base + self.system_path, json=body, timeout=60)
        if r.status_code in (412, 428):  # BMC insists on an ETag
            etag = self.s.get(self.base + self.system_path, timeout=60).headers.get("ETag", "*")
            r = self.s.patch(self.base + self.system_path, json=body, timeout=60,
                             headers={"If-Match": etag})
        if r.status_code not in (200, 202, 204):
            raise RedfishError(f"[{self.label}] set boot override: HTTP {r.status_code} {r.text[:300]}")

    # ---- virtual media ----
    def virtual_cd(self) -> dict:
        """Find the virtual CD/DVD device (under the System on new firmware, else the Manager)."""
        colls = []
        sys_vm = self.system().get("VirtualMedia", {}).get("@odata.id")
        if sys_vm:
            colls.append(sys_vm)
        try:
            mgr = self.get(self._first_member("/redfish/v1/Managers"))
            if mgr.get("VirtualMedia", {}).get("@odata.id"):
                colls.append(mgr["VirtualMedia"]["@odata.id"])
        except RedfishError:
            pass
        for coll in colls:
            for m in self.get(coll).get("Members", []):
                dev = self.get(m["@odata.id"])
                if {"CD", "DVD"} & set(dev.get("MediaTypes", [])):
                    return dev
        raise RedfishError(f"[{self.label}] no virtual CD/DVD device found via Redfish")

    def eject(self) -> None:
        dev = self.virtual_cd()
        if not dev.get("Inserted"):
            return
        action = dev.get("Actions", {}).get("#VirtualMedia.EjectMedia", {}).get("target")
        if action:
            self._req("POST", action, json={})
        else:
            self._req("PATCH", dev["@odata.id"], json={"Image": None, "Inserted": False})

    def insert(self, url: str) -> None:
        self.eject()
        dev = self.virtual_cd()
        action = dev.get("Actions", {}).get("#VirtualMedia.InsertMedia", {}).get("target")
        if not action:  # older firmware: PATCH the resource instead of calling the action
            self._req("PATCH", dev["@odata.id"], json={"Image": url, "Inserted": True})
            return
        r = self.s.post(self.base + action, timeout=60,
                        json={"Image": url, "Inserted": True, "WriteProtected": True})
        if r.status_code == 400:  # some BMCs only accept the Image property
            r = self.s.post(self.base + action, json={"Image": url}, timeout=60)
        if r.status_code not in (200, 202, 204):
            raise RedfishError(f"[{self.label}] InsertMedia: HTTP {r.status_code} {r.text[:300]}")

    # ---- one-shot ----
    def boot_from_iso(self, url: str) -> None:
        self.insert(url)
        self.set_boot_once_cd()
        self.power_cycle()
