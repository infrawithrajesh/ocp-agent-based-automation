import copy
from pathlib import Path

import pytest
import yaml

from ocpdeploy import config, infoblox, render

RAW = yaml.safe_load((Path(__file__).parent.parent / "config/cluster.example.yaml").read_text())


def make(raw=RAW):
    nodes = [config.Node(n["name"], n["role"], n["ip"], n["mac"], n["bmc"], n["interface"], n["root_device"])
             for n in raw["nodes"]]
    cfg = config.Config(raw, nodes, Path("work/test"))
    config.validate(cfg)
    return cfg


def test_install_config():
    ic = render.install_config(make(), '{"auths":{}}', "ssh-ed25519 AAAA")
    assert ic["controlPlane"]["replicas"] == 3 and ic["compute"][0]["replicas"] == 3
    assert ic["platform"]["baremetal"] == {"apiVIPs": ["10.10.20.5"], "ingressVIPs": ["10.10.20.6"]}
    assert ic["networking"]["machineNetwork"] == [{"cidr": "10.10.20.0/24"}]


def test_agent_config_static_ip():
    ac = render.agent_config(make())
    assert ac["rendezvousIP"] == "10.10.20.11" and len(ac["hosts"]) == 6
    h = ac["hosts"][0]
    assert h["hostname"] == "master-0.ocp422.lab.example.com"
    nic = h["networkConfig"]["interfaces"][0]
    assert nic["ipv4"]["address"] == [{"ip": "10.10.20.11", "prefix-length": 24}]
    assert h["networkConfig"]["routes"]["config"][0]["next-hop-interface"] == "eno1"


def test_agent_config_vlan():
    raw = copy.deepcopy(RAW)
    raw["network"]["vlan_id"] = 120
    nc = render.agent_config(make(raw))["hosts"][0]["networkConfig"]
    assert [i["name"] for i in nc["interfaces"]] == ["eno1", "eno1.120"]
    assert nc["interfaces"][0]["ipv4"] == {"enabled": False}
    assert nc["routes"]["config"][0]["next-hop-interface"] == "eno1.120"


def test_dns_records():
    a, ptr = infoblox.records(make())
    assert ("api.ocp422.lab.example.com", "10.10.20.5") in a
    assert ("api-int.ocp422.lab.example.com", "10.10.20.5") in a
    assert ("*.apps.ocp422.lab.example.com", "10.10.20.6") in a
    assert len(a) == 9 and len(ptr) == 7


@pytest.mark.parametrize("mutate,msg", [
    (lambda r: r["cluster"].update(api_vip="10.99.0.5"), "outside machine_cidr"),
    (lambda r: r["cluster"].update(ingress_vip="10.10.20.5"), "must be different"),
    (lambda r: r["nodes"].pop(0), "exactly 3 masters"),
    (lambda r: r["nodes"][1].update(ip="10.10.20.11"), "duplicate node ip"),
    (lambda r: r["nodes"][0].update(mac="zz"), "bad MAC"),
])
def test_validation_rejects(mutate, msg):
    raw = copy.deepcopy(RAW)
    mutate(raw)
    with pytest.raises(config.ConfigError, match=msg):
        make(raw)
