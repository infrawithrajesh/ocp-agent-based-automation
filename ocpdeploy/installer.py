"""Download openshift-install / oc, build the agent ISO, serve it, wait for the install."""
from __future__ import annotations

import functools
import http.server
import logging
import os
import subprocess
import tarfile
import threading
from pathlib import Path

import requests

log = logging.getLogger("installer")
MIRROR = "https://mirror.openshift.com/pub/openshift-v4/x86_64/clients/ocp"


def run(cmd: list[str], **kw) -> None:
    log.info("$ %s", " ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def fetch_tools(cfg) -> None:
    """Download openshift-install and oc for cfg.version into work/<cluster>/bin (cached)."""
    cfg.bin_dir.mkdir(parents=True, exist_ok=True)
    marker = cfg.bin_dir / ".version"
    if marker.exists() and marker.read_text() == cfg.version and (cfg.bin_dir / "openshift-install").exists():
        log.info("tools for %s already cached", cfg.version)
        return
    for tarball, binary in (("openshift-install-linux.tar.gz", "openshift-install"),
                            ("openshift-client-linux.tar.gz", "oc")):
        url = f"{MIRROR}/{cfg.version}/{tarball}"
        log.info("downloading %s", url)
        dest = cfg.bin_dir / tarball
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        with tarfile.open(dest) as t:
            t.extract(binary, cfg.bin_dir)
        dest.unlink()
        (cfg.bin_dir / binary).chmod(0o755)
    marker.write_text(cfg.version)
    run([cfg.bin_dir / "openshift-install", "version"])


def ensure_ssh_key(cfg) -> str:
    pub = os.environ.get("SSH_PUBKEY_FILE", "").strip()
    if pub:
        return Path(pub).expanduser().read_text()
    key = cfg.workdir / "id_ed25519"
    if not key.exists():
        cfg.workdir.mkdir(parents=True, exist_ok=True)
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"core@{cfg.domain}", "-f", key])
    return key.with_suffix(".pub").read_text()


def create_iso(cfg) -> None:
    run([cfg.bin_dir / "openshift-install", "agent", "create", "image",
         "--dir", cfg.install_dir, "--log-level", "info"])
    if not cfg.iso_path.exists():
        raise RuntimeError(f"openshift-install did not produce {cfg.iso_path}")


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logging.getLogger("iso-http").debug(fmt, *args)


def serve_iso(cfg) -> http.server.ThreadingHTTPServer:
    """Serve install/ over HTTP so BMCs can pull the ISO. Must stay up for the whole install."""
    port = int(cfg.raw["bastion"].get("iso_port", 8080))
    handler = functools.partial(_QuietHandler, directory=str(cfg.install_dir))
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("serving ISO at %s", cfg.iso_url)
    return srv


def wait_for(cfg, what: str) -> None:
    run([cfg.bin_dir / "openshift-install", "agent", "wait-for", what,
         "--dir", cfg.install_dir, "--log-level", "info"])


def oc(cfg, *args: str) -> None:
    env = {**os.environ, "KUBECONFIG": str(cfg.install_dir / "auth" / "kubeconfig")}
    run([cfg.bin_dir / "oc", *args], env=env)
