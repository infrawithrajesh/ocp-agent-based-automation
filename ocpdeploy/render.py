"""Render install-config.yaml and agent-config.yaml for the Agent-based Installer."""
from __future__ import annotations

import yaml


def install_config(cfg, pull_secret: str, ssh_key: str) -> dict:
    net = cfg.net
    return {
        "apiVersion": "v1",
        "baseDomain": cfg.base_domain,
        "metadata": {"name": cfg.name},
        "controlPlane": {"name": "master", "replicas": len(cfg.masters), "architecture": "amd64"},
        "compute": [{"name": "worker", "replicas": len(cfg.workers), "architecture": "amd64"}],
        "networking": {
            "networkType": "OVNKubernetes",
            "clusterNetwork": [{"cidr": net.get("cluster_cidr", "10.128.0.0/14"),
                                "hostPrefix": net.get("host_prefix", 23)}],
            "serviceNetwork": [net.get("service_cidr", "172.30.0.0/16")],
            "machineNetwork": [{"cidr": net["machine_cidr"]}],
        },
        # platform: baremetal => OpenShift runs keepalived + haproxy itself as static
        # pods on the nodes and floats these two VIPs. No external load balancer.
        "platform": {"baremetal": {"apiVIPs": [cfg.api_vip], "ingressVIPs": [cfg.ingress_vip]}},
        "pullSecret": pull_secret.strip(),
        "sshKey": ssh_key.strip(),
    }


def _network_config(cfg, node) -> dict:
    net = cfg.net
    vlan = net.get("vlan_id")
    ipv4 = {"enabled": True, "dhcp": False,
            "address": [{"ip": node.ip, "prefix-length": cfg.prefix_len}]}
    phys = {"name": node.interface, "type": "ethernet", "state": "up", "mac-address": node.mac}
    if vlan:
        l3 = f"{node.interface}.{vlan}"
        phys["ipv4"] = {"enabled": False}
        phys["ipv6"] = {"enabled": False}
        ifaces = [phys, {"name": l3, "type": "vlan", "state": "up",
                         "vlan": {"base-iface": node.interface, "id": int(vlan)},
                         "ipv4": ipv4, "ipv6": {"enabled": False}}]
    else:
        l3 = node.interface
        phys["ipv4"] = ipv4
        phys["ipv6"] = {"enabled": False}
        ifaces = [phys]
    return {
        "interfaces": ifaces,
        "dns-resolver": {"config": {"server": list(net["dns_servers"])}},
        "routes": {"config": [{"destination": "0.0.0.0/0", "next-hop-address": net["gateway"],
                               "next-hop-interface": l3, "table-id": 254}]},
    }


def agent_config(cfg) -> dict:
    out = {
        "apiVersion": "v1beta1",
        "kind": "AgentConfig",
        "metadata": {"name": cfg.name},
        "rendezvousIP": cfg.masters[0].ip,  # this master hosts the temporary assisted-service
        "hosts": [{
            "hostname": cfg.fqdn(n),
            "role": n.role,
            "rootDeviceHints": {"deviceName": n.root_device},
            "interfaces": [{"name": n.interface, "macAddress": n.mac}],
            "networkConfig": _network_config(cfg, n),
        } for n in cfg.nodes],
    }
    if cfg.net.get("ntp_servers"):
        out["additionalNTPSources"] = list(cfg.net["ntp_servers"])
    return out


def write(cfg, pull_secret: str, ssh_key: str, dirs) -> None:
    """Write both files into each directory in `dirs` (mode 0600: they hold the pull secret)."""
    docs = {"install-config.yaml": install_config(cfg, pull_secret, ssh_key),
            "agent-config.yaml": agent_config(cfg)}
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        for fname, doc in docs.items():
            p = d / fname
            p.write_text(yaml.safe_dump(doc, sort_keys=False, default_flow_style=False))
            p.chmod(0o600)
