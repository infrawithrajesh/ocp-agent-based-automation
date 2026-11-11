"""CLI: python -m ocpdeploy <command> [-c config/cluster.yaml] [--dry-run] [--force]"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

from . import config, infoblox, installer, render
from .config import env_verify
from .redfish import Redfish
from .vault import Vault

log = logging.getLogger("ocpdeploy")
ISO_MAX_AGE_H = 20  # certs baked into the agent ISO expire after 24h


def pull_secret() -> str:
    p = Path(os.environ.get("PULL_SECRET_FILE", "./pull-secret.json")).expanduser()
    if not p.exists():
        raise config.ConfigError(f"pull secret not found at {p} (set PULL_SECRET_FILE)")
    text = p.read_text()
    if "auths" not in json.loads(text):
        raise config.ConfigError(f"{p} is not a pull secret (no 'auths' key)")
    return text


def bmc_clients(cfg) -> dict[str, Redfish]:
    """Read every node's BMC credentials from Vault and build Redfish clients."""
    vault = Vault()
    verify = env_verify("REDFISH_VERIFY_SSL", "false")
    out = {}
    for n in cfg.nodes:
        user, pw = vault.bmc_creds(cfg.raw["vault"]["mount"], cfg.vault_path(n))
        out[n.name] = Redfish(n.bmc, user, pw, verify=verify, label=n.name)
    return out


def parallel(fn, items: dict, what: str) -> None:
    """Run fn(client) for every node at once; fail with a per-node summary."""
    with ThreadPoolExecutor(max_workers=len(items)) as ex:
        futs = {name: ex.submit(fn, c) for name, c in items.items()}
    failed = {n: f.exception() for n, f in futs.items() if f.exception()}
    for n in items:
        log.info("  %-10s %s: %s", n, what, f"FAILED - {failed[n]}" if n in failed else "ok")
    if failed:
        raise RuntimeError(f"{what} failed on: {', '.join(failed)}")


# ------------------------------------------------------------------ stages

def stage_preflight(cfg, args) -> None:
    pull_secret()
    missing = [t for t in ("nmstatectl", "ssh-keygen") if not shutil.which(t)]
    if missing:
        raise RuntimeError(f"missing tools on this host: {missing} (dnf install -y nmstate openssh-clients)")
    log.info("config, pull secret and local tools ok")
    if args.dry_run:
        log.info("[dry-run] skipping Vault / Redfish / Infoblox reachability checks")
        return

    infoblox.Infoblox(cfg.raw["infoblox"].get("dns_view", "default")).ping()
    log.info("Infoblox WAPI reachable, credentials ok")

    clients = bmc_clients(cfg)
    log.info("Vault ok: BMC credentials read for %d nodes", len(clients))

    def check(c: Redfish):
        s = c.system()
        c.virtual_cd()
        log.info("  %-10s %s %s, power=%s", c.label, s.get("Manufacturer", "?"), s.get("Model", "?"), s.get("PowerState"))
    parallel(check, clients, "redfish + virtual media")

    for label, vip in (("api_vip", cfg.api_vip), ("ingress_vip", cfg.ingress_vip)):
        if shutil.which("ping") and subprocess.run(["ping", "-c1", "-W1", vip], capture_output=True).returncode == 0:
            log.warning("%s %s answers ping - is it already in use?", label, vip)


def stage_dns(cfg, args) -> None:
    a, ptr = infoblox.records(cfg)
    if args.dry_run:
        for name, ip in a:
            log.info("[dry-run] A    %-40s -> %s", name, ip)
        for ip, name in ptr:
            log.info("[dry-run] PTR  %-40s -> %s", ip, name)
        return
    ib = infoblox.Infoblox(cfg.raw["infoblox"].get("dns_view", "default"), cfg.raw["infoblox"].get("ttl"))
    for name, ip in a:
        log.info("A    %-40s -> %-15s %s", name, ip, ib.ensure_a(name, ip))
    for ip, name in ptr:
        log.info("PTR  %-40s -> %-15s %s", ip, name, ib.ensure_ptr(ip, name))
    verify_dns(cfg, a)


def verify_dns(cfg, a_records, timeout: int = 90) -> None:
    """Wait until the cluster's DNS servers actually answer (needs `dig`; skipped if absent)."""
    if not shutil.which("dig"):
        log.warning("dig not installed - skipping DNS resolution check")
        return
    server = cfg.net["dns_servers"][0]
    pending = {(n.replace("*", "test"), ip) for n, ip in a_records}
    end = time.time() + timeout
    while pending and time.time() < end:
        for name, ip in list(pending):
            out = subprocess.run(["dig", "+short", f"@{server}", name, "A"], capture_output=True, text=True).stdout
            if ip in out.split():
                pending.discard((name, ip))
        if pending:
            time.sleep(5)
    if pending:
        raise RuntimeError(f"DNS server {server} does not resolve yet: {sorted(pending)}")
    log.info("all records resolve via %s", server)


def stage_dns_delete(cfg, args) -> None:
    a, ptr = infoblox.records(cfg)
    if args.dry_run:
        log.info("[dry-run] would delete %d A and %d PTR records", len(a), len(ptr))
        return
    ib = infoblox.Infoblox(cfg.raw["infoblox"].get("dns_view", "default"))
    for name, _ in a:
        log.info("A    %-40s deleted=%d", name, ib.delete_a(name))
    for ip, name in ptr:
        log.info("PTR  %-40s deleted=%d", ip, ib.delete_ptr(ip, name))


def stage_render(cfg, args) -> None:
    """Preview only: write the two installer inputs to work/<cluster>/rendered/."""
    render.write(cfg, pull_secret(), installer.ensure_ssh_key(cfg), [cfg.rendered_dir])
    log.info("rendered install-config.yaml and agent-config.yaml in %s", cfg.rendered_dir)


def stage_iso(cfg, args) -> None:
    iso = cfg.iso_path
    if iso.exists() and not args.force:
        age_h = (time.time() - iso.stat().st_mtime) / 3600
        if age_h < ISO_MAX_AGE_H:
            log.info("reusing %s (%.1fh old; --force to rebuild)", iso, age_h)
            return
        log.info("ISO is %.1fh old (certs expire at 24h) - rebuilding", age_h)
    shutil.rmtree(cfg.install_dir, ignore_errors=True)  # stale installer state would be reused
    render.write(cfg, pull_secret(), installer.ensure_ssh_key(cfg), [cfg.rendered_dir, cfg.install_dir])
    if args.dry_run:
        log.info("[dry-run] would download openshift-install %s and build %s", cfg.version, iso)
        return
    installer.fetch_tools(cfg)
    installer.create_iso(cfg)
    log.info("built %s (%.0f MB)", iso, iso.stat().st_size / 1e6)


def stage_install(cfg, args) -> None:
    if args.dry_run:
        log.info("[dry-run] would serve %s, mount it on %d BMCs, power-cycle, and wait", cfg.iso_url, len(cfg.nodes))
        return
    if not cfg.iso_path.exists():
        raise RuntimeError("no ISO - run the 'iso' stage first")
    clients = bmc_clients(cfg)
    srv = installer.serve_iso(cfg)
    try:
        parallel(lambda c: c.boot_from_iso(cfg.iso_url), clients, "mount ISO + boot")
        installer.wait_for(cfg, "bootstrap-complete")
        installer.wait_for(cfg, "install-complete")
    finally:
        srv.shutdown()
        try:
            parallel(lambda c: c.eject(), clients, "eject ISO")
        except RuntimeError as e:
            log.warning("%s (eject manually)", e)
    installer.oc(cfg, "get", "nodes", "-o", "wide")
    installer.oc(cfg, "get", "clusteroperators")
    auth = cfg.install_dir / "auth"
    log.info("CLUSTER READY")
    log.info("  console    : https://console-openshift-console.apps.%s", cfg.domain)
    log.info("  kubeconfig : export KUBECONFIG=%s", (auth / "kubeconfig").resolve())
    log.info("  kubeadmin  : password in %s", (auth / "kubeadmin-password").resolve())


PIPELINE = [("preflight", stage_preflight), ("dns", stage_dns), ("iso", stage_iso), ("install", stage_install)]
COMMANDS = {**dict(PIPELINE), "render": stage_render, "dns-delete": stage_dns_delete}


def main() -> int:
    ap = argparse.ArgumentParser(prog="ocpdeploy", description=__doc__)
    ap.add_argument("command", choices=["deploy", *COMMANDS],
                    help="deploy = preflight -> dns -> iso -> install")
    ap.add_argument("-c", "--config", default="config/cluster.yaml")
    ap.add_argument("--dry-run", action="store_true", help="validate and print actions; touch nothing remote")
    ap.add_argument("--force", action="store_true", help="rebuild the ISO even if a fresh one exists")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    load_dotenv(".env")

    stages = PIPELINE if args.command == "deploy" else [(args.command, COMMANDS[args.command])]
    t0 = time.time()
    try:
        cfg = config.load(args.config)
        for name, fn in stages:
            t = time.time()
            log.info("===== %s =====", name.upper())
            fn(cfg, args)
            log.info("----- %s done in %.0fs", name, time.time() - t)
    except subprocess.CalledProcessError as e:
        log.error("command failed (exit %s): %s", e.returncode, " ".join(map(str, e.cmd)))
        return 1
    except Exception as e:  # noqa: BLE001 - top-level: print a clean one-line error
        log.error("%s: %s", type(e).__name__, e)
        if args.verbose:
            raise
        return 1
    log.info("total: %.1f min", (time.time() - t0) / 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
