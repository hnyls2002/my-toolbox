"""Typed topology model for rdev: clusters -> instances -> containers.

Loads two files and produces a single immutable Topology that the rest of
rdev consumes:

  ~/.rdev/config.yaml   declarative cluster/instance config
  ~/.ssh/config         network-layer info (hostname/user/port/proxy)

The split is deliberate: ssh config owns how to reach a host, rdev yaml
owns which clusters exist and what container runs on each instance. The
loader joins the two via the `host` field, which must match a Host
declared in ~/.ssh/config.

Clusters may share hosts. When a host appears in multiple clusters, an
unqualified resolution by host alone is ambiguous and the loader requires
the user to specify a cluster name.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

import yaml

# ---------- ssh config ----------


@dataclass(frozen=True)
class SshSpec:
    alias: str  # the Host name in ~/.ssh/config
    hostname: str
    user: str
    port: int = 22
    proxy_jump: Optional[str] = None
    identity_file: Optional[str] = None


def _declared_ssh_aliases(ssh_config_path: Path) -> set[str]:
    """Return Host aliases explicitly declared in ssh config (skips wildcards)."""
    if not ssh_config_path.exists():
        return set()
    aliases: set[str] = set()
    for line in ssh_config_path.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if not s.lower().startswith("host "):
            continue
        for token in s.split()[1:]:
            if "*" in token or "?" in token:
                continue
            aliases.add(token)
    return aliases


def parse_ssh_alias(alias: str) -> SshSpec:
    """Run ``ssh -G <alias>`` and parse effective config into SshSpec."""
    result = subprocess.run(
        ["ssh", "-G", alias], capture_output=True, text=True, check=True
    )
    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        k, _, v = line.partition(" ")
        if k:
            fields[k.lower()] = v.strip()
    return SshSpec(
        alias=alias,
        hostname=fields.get("hostname", alias),
        user=fields.get("user", os.environ.get("USER", "")),
        port=int(fields.get("port", "22")),
        proxy_jump=fields.get("proxyjump") or None,
        identity_file=fields.get("identityfile") or None,
    )


# ---------- cluster model ----------


@dataclass(frozen=True)
class ContainerSpec:
    name: str
    image: str
    shm_size: str
    host_root: Path
    home_dir: str

    @property
    def host_home(self) -> Path:
        """Bind-mount source for /host_home (host_root / home_dir)."""
        return self.host_root / self.home_dir


@dataclass(frozen=True)
class SetupSpec:
    setup_script: str
    install_worktree_script: str
    default_worktree: str = "sglang"


@dataclass(frozen=True)
class Instance:
    ssh: SshSpec


@dataclass(frozen=True)
class Cluster:
    name: str
    container: ContainerSpec
    setup: SetupSpec
    instances: tuple[Instance, ...]
    status_filter: str

    @property
    def sync_target_base(self) -> Path:
        """rsync destination parent (e.g. /data/lsyin)."""
        return self.container.host_home


@dataclass(frozen=True)
class Target:
    """Result of `topology.resolve(name)`. is_specific=True if user named a host."""

    cluster: Cluster
    instances: tuple[Instance, ...]
    is_specific: bool


@dataclass(frozen=True)
class Topology:
    clusters: dict[str, Cluster]
    # host -> list of (cluster_name, instance). Multiple entries means the host
    # appears in more than one cluster; resolution by host alone is then ambiguous.
    by_host: dict[str, list[tuple[str, Instance]]]

    def is_cluster(self, name: str) -> bool:
        return name in self.clusters

    def is_host(self, name: str) -> bool:
        return name in self.by_host

    def resolve(self, name: str) -> Target:
        if name in self.clusters:
            cluster = self.clusters[name]
            return Target(cluster, cluster.instances, is_specific=False)
        if name in self.by_host:
            entries = self.by_host[name]
            if len(entries) > 1:
                cnames = sorted(c for c, _ in entries)
                raise KeyError(
                    f"host {name!r} is in multiple clusters {cnames}; "
                    f"specify a cluster name instead"
                )
            cluster_name, inst = entries[0]
            return Target(self.clusters[cluster_name], (inst,), is_specific=True)
        raise KeyError(f"Unknown cluster or host: {name!r}")

    @property
    def all_hosts(self) -> list[str]:
        return list(self.by_host.keys())

    @property
    def all_cluster_names(self) -> list[str]:
        return list(self.clusters.keys())


# ---------- yaml loading ----------


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _build_cluster(
    name: str, raw: dict, defaults: dict, ssh_specs: dict[str, SshSpec]
) -> Cluster:
    merged = _deep_merge(defaults, raw)
    container_dict = merged["container"]
    setup_dict = merged["setup"]

    instances: list[Instance] = []
    for entry in merged.get("instances", []):
        host = entry["host"]
        instances.append(Instance(ssh=ssh_specs[host]))

    return Cluster(
        name=name,
        container=ContainerSpec(
            name=container_dict["name"],
            image=container_dict["image"],
            shm_size=container_dict["shm_size"],
            host_root=Path(container_dict["host_root"]),
            home_dir=container_dict["home_dir"],
        ),
        setup=SetupSpec(
            setup_script=setup_dict["setup_script"],
            install_worktree_script=setup_dict["install_worktree_script"],
            default_worktree=setup_dict.get("default_worktree", "sglang"),
        ),
        instances=tuple(instances),
        status_filter=merged.get("status_filter", container_dict["name"]),
    )


def load_topology(
    config_path: Optional[Path] = None,
    ssh_config_path: Optional[Path] = None,
) -> Topology:
    config_path = config_path or Path.home() / ".rdev" / "config.yaml"
    ssh_config_path = ssh_config_path or Path.home() / ".ssh" / "config"

    if not config_path.exists():
        raise FileNotFoundError(f"rdev config not found: {config_path}")

    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}

    defaults = raw.get("defaults", {})
    raw_clusters = raw.get("clusters", {})

    declared = _declared_ssh_aliases(ssh_config_path)
    referenced: set[str] = set()
    for c in raw_clusters.values():
        merged = _deep_merge(defaults, c)
        for entry in merged.get("instances", []):
            referenced.add(entry["host"])

    missing = referenced - declared
    if missing:
        raise ValueError(
            f"hosts referenced in {config_path} but not declared in "
            f"{ssh_config_path}: {sorted(missing)}"
        )

    ssh_specs = {h: parse_ssh_alias(h) for h in referenced}

    clusters: dict[str, Cluster] = {}
    by_host: dict[str, list[tuple[str, Instance]]] = {}
    for cname, raw_c in raw_clusters.items():
        cluster = _build_cluster(cname, raw_c, defaults, ssh_specs)
        clusters[cname] = cluster
        for inst in cluster.instances:
            by_host.setdefault(inst.ssh.alias, []).append((cname, inst))

    return Topology(clusters=clusters, by_host=by_host)


# ---------- override ----------


def with_overrides(
    cluster: Cluster,
    *,
    container: Optional[str] = None,
    image: Optional[str] = None,
) -> Cluster:
    """Return a new Cluster with CLI-time --container / --image applied."""
    if container is None and image is None:
        return cluster
    new_container = replace(
        cluster.container,
        name=container or cluster.container.name,
        image=image or cluster.container.image,
    )
    return replace(cluster, container=new_container)


_topology_cache: Optional[Topology] = None


def get_topology() -> Topology:
    """Cached load_topology()."""
    global _topology_cache
    if _topology_cache is None:
        _topology_cache = load_topology()
    return _topology_cache


def unreferenced_hosts(
    topo: Topology, ssh_config_path: Optional[Path] = None
) -> list[str]:
    """ssh aliases declared in ssh config but not referenced by any cluster."""
    ssh_config_path = ssh_config_path or Path.home() / ".ssh" / "config"
    declared = _declared_ssh_aliases(ssh_config_path)
    return sorted(declared - set(topo.by_host.keys()))
