"""Load and validate config/cluster.yaml and the .env settings."""
from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")


class ConfigError(Exception):
    pass


@dataclass
class Node:
    name: str
    role: str
    ip: str
    mac: str
    bmc: str
    interface: str
    root_device: str


@dataclass
class Config:
    raw: dict
    nodes: list[Node]
    workdir: Path

    # --- cluster ---
    @property
    def name(self) -> str: return self.raw["cluster"]["name"]
    @property
    def base_domain(self) -> str: return self.raw["cluster"]["base_domain"]
    @property
    def domain(self) -> str: return f"{self.name}.{self.base_domain}"
    @property
    def version(self) -> str: return str(self.raw["cluster"]["version"])
    @property
    def api_vip(self) -> str: return self.raw["cluster"]["api_vip"]
    @property
    def ingress_vip(self) -> str: return self.raw["cluster"]["ingress_vip"]
    @property
    def net(self) -> dict: return self.raw["network"]
    @property
    def prefix_len(self) -> int:
        return ipaddress.ip_network(self.net["machine_cidr"]).prefixlen
    @property
    def masters(self) -> list[Node]: return [n for n in self.nodes if n.role == "master"]
    @property
    def workers(self) -> list[Node]: return [n for n in self.nodes if n.role == "worker"]
    @property
    def iso_url(self) -> str:
        b = self.raw["bastion"]
        return f"http://{b['iso_host']}:{b.get('iso_port', 8080)}/agent.x86_64.iso"

    def fqdn(self, node: Node) -> str:
        return f"{node.name}.{self.domain}"

    def vault_path(self, node: Node) -> str:
        return self.raw["vault"]["bmc_path"].format(name=node.name)

    # --- paths ---
    @property
    def bin_dir(self) -> Path: return self.workdir / "bin"
    @property
    def install_dir(self) -> Path: return self.workdir / "install"
    @property
    def rendered_dir(self) -> Path: return self.workdir / "rendered"
    @property
    def iso_path(self) -> Path: return self.install_dir / "agent.x86_64.iso"


def env_verify(var: str, default: str = "true"):
    """true/false or a CA bundle path -> value for requests' `verify`."""
    v = os.environ.get(var, default).strip()
    if v.lower() in ("true", "1", "yes"):
        return True
    if v.lower() in ("false", "0", "no", ""):
        return False
    return v


def load(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"{path} not found (copy config/cluster.example.yaml)")
    raw = yaml.safe_load(path.read_text())
    try:
        nodes = [Node(n["name"], n["role"], n["ip"], n["mac"].lower(), n["bmc"],
                      n.get("interface", "eno1"), n.get("root_device", "/dev/sda"))
                 for n in raw["nodes"]]
        cfg = Config(raw, nodes, Path("work") / raw["cluster"]["name"])
        validate(cfg)
    except KeyError as e:
        raise ConfigError(f"missing required key in {path}: {e}") from e
    return cfg


def validate(cfg: Config) -> None:
    errs = []
    try:
        cidr = ipaddress.ip_network(cfg.net["machine_cidr"])
    except ValueError as e:
        raise ConfigError(f"network.machine_cidr: {e}")

    def in_cidr(label, ip):
        try:
            if ipaddress.ip_address(ip) not in cidr:
                errs.append(f"{label} {ip} is outside machine_cidr {cidr}")
        except ValueError:
            errs.append(f"{label} '{ip}' is not a valid IP")

    in_cidr("api_vip", cfg.api_vip)
    in_cidr("ingress_vip", cfg.ingress_vip)
    in_cidr("gateway", cfg.net["gateway"])
    for n in cfg.nodes:
        in_cidr(f"node {n.name} ip", n.ip)
        if n.role not in ("master", "worker"):
            errs.append(f"node {n.name}: role must be master or worker")
        if not MAC_RE.match(n.mac):
            errs.append(f"node {n.name}: bad MAC '{n.mac}'")

    if cfg.api_vip == cfg.ingress_vip:
        errs.append("api_vip and ingress_vip must be different")
    if len(cfg.masters) != 3:
        errs.append(f"need exactly 3 masters, found {len(cfg.masters)}")
    if len(cfg.workers) < 2:
        errs.append(f"need at least 2 workers, found {len(cfg.workers)}")

    for field in ("name", "ip", "mac", "bmc"):
        vals = [getattr(n, field) for n in cfg.nodes]
        dups = {v for v in vals if vals.count(v) > 1}
        if dups:
            errs.append(f"duplicate node {field}: {sorted(dups)}")
    clash = {cfg.api_vip, cfg.ingress_vip} & {n.ip for n in cfg.nodes}
    if clash:
        errs.append(f"VIP collides with a node IP: {sorted(clash)}")
    if not cfg.net.get("dns_servers"):
        errs.append("network.dns_servers is empty")
    if errs:
        raise ConfigError("invalid cluster config:\n  - " + "\n  - ".join(errs))
