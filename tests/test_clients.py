"""Exercise the Redfish and Infoblox clients against in-memory fakes (no network)."""
import json

import requests
from requests.adapters import BaseAdapter

from ocpdeploy.infoblox import Infoblox
from ocpdeploy.redfish import Redfish


class FakeAdapter(BaseAdapter):
    """Routes every request to handler(method, path, query, body) -> (status, json)."""
    def __init__(self, handler):
        super().__init__()
        self.handler, self.calls = handler, []

    def send(self, request, **kw):
        url = requests.utils.urlparse(request.url)
        body = json.loads(request.body) if request.body else None
        self.calls.append((request.method, url.path, body))
        status, payload = self.handler(request.method, url.path, dict(p.split("=") for p in url.query.split("&") if p), body)
        r = requests.Response()
        r.status_code, r.request, r.url = status, request, request.url
        r._content = json.dumps(payload).encode() if payload is not None else b""
        return r

    def close(self):
        pass


def fake_bmc():
    state = {"power": "On", "inserted": False, "image": None, "boot": None}
    SYS, VM = "/redfish/v1/Systems/1", "/redfish/v1/Managers/1/VirtualMedia/CD"

    def h(method, path, q, body):
        if method == "GET":
            return 200, {
                "/redfish/v1/Systems": {"Members": [{"@odata.id": SYS}]},
                "/redfish/v1/Managers": {"Members": [{"@odata.id": "/redfish/v1/Managers/1"}]},
                "/redfish/v1/Managers/1": {"VirtualMedia": {"@odata.id": "/redfish/v1/Managers/1/VirtualMedia"}},
                "/redfish/v1/Managers/1/VirtualMedia": {"Members": [{"@odata.id": VM}]},
                SYS: {"PowerState": state["power"],
                      "Actions": {"#ComputerSystem.Reset": {"target": SYS + "/Actions/ComputerSystem.Reset"}}},
                VM: {"@odata.id": VM, "MediaTypes": ["CD", "DVD"], "Inserted": state["inserted"],
                     "Actions": {"#VirtualMedia.InsertMedia": {"target": VM + "/Actions/VirtualMedia.InsertMedia"},
                                 "#VirtualMedia.EjectMedia": {"target": VM + "/Actions/VirtualMedia.EjectMedia"}}},
            }[path]
        if path.endswith("InsertMedia"):
            if "WriteProtected" in body:      # behave like a strict BMC
                return 400, {"error": "unknown property"}
            state.update(inserted=True, image=body["Image"])
        elif path.endswith("EjectMedia"):
            state.update(inserted=False, image=None)
        elif path.endswith("ComputerSystem.Reset"):
            state["power"] = "Off" if body["ResetType"] == "ForceOff" else "On"
        elif method == "PATCH" and path == SYS:
            state["boot"] = body["Boot"]
        return 204, None
    return state, h


def test_redfish_boot_from_iso(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    state, h = fake_bmc()
    state["inserted"] = True  # stale media from a previous run must be ejected first
    rf = Redfish("bmc", "u", "p")
    adapter = FakeAdapter(h)
    rf.s.mount("https://", adapter)
    rf.boot_from_iso("http://bastion:8080/agent.x86_64.iso")
    assert state == {"power": "On", "inserted": True, "image": "http://bastion:8080/agent.x86_64.iso",
                     "boot": {"BootSourceOverrideEnabled": "Once", "BootSourceOverrideTarget": "Cd"}}
    resets = [b["ResetType"] for m, p, b in adapter.calls if p.endswith("ComputerSystem.Reset")]
    assert resets == ["ForceOff", "On"]
    rf.eject()
    assert state["inserted"] is False


def test_infoblox_idempotent(monkeypatch):
    for k, v in {"INFOBLOX_HOST": "ib", "INFOBLOX_USER": "u", "INFOBLOX_PASSWORD": "p"}.items():
        monkeypatch.setenv(k, v)
    db = {}  # ref -> record

    def h(method, path, q, body):
        obj = path.split("/wapi/v2.12/")[1]
        if method == "GET":
            kind = obj
            return 200, [r for r in db.values() if r["_kind"] == kind and
                         all(r.get(k) == v.replace("%2A", "*") for k, v in q.items())]
        if method == "POST":
            ref = f"{obj}/{len(db)}"
            db[ref] = {"_ref": ref, "_kind": obj, **body}
            return 201, ref
        if method == "PUT":
            db[obj].update(body)
            return 200, obj
        db.pop(obj)
        return 200, obj

    ib = Infoblox(ttl=300)
    ib.s.mount("https://", FakeAdapter(h))
    assert ib.ensure_a("api.c.example.com", "10.0.0.5") == "created"
    assert ib.ensure_a("api.c.example.com", "10.0.0.5") == "unchanged"
    assert ib.ensure_a("api.c.example.com", "10.0.0.9") == "updated"
    assert ib.ensure_a("*.apps.c.example.com", "10.0.0.6") == "created"
    assert ib.ensure_ptr("10.0.0.9", "api.c.example.com") == "created"
    assert ib.ensure_ptr("10.0.0.9", "api.c.example.com") == "unchanged"
    assert ib.delete_a("api.c.example.com") == 1 and ib.delete_ptr("10.0.0.9", "api.c.example.com") == 1
    assert [r["name"] for r in db.values()] == ["*.apps.c.example.com"]
