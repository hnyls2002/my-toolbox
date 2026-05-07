"""Typed topology model for rdev: clusters -> instances -> containers.

Loads two files and produces a single immutable Topology that the rest of
rdev consumes:

  ~/.rdev/config.yaml   declarative cluster/instance config
  ~/.ssh/config         network-layer info (hostname/user/port/proxy)

The split is deliberate: ssh config owns "how to reach a host", rdev yaml
owns "what container runs there". The loader joins the two via ssh_alias.
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
    alias: str
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
    """Result of `topology.resolve(name)`. is_specific=True if user named an alias."""

    cluster: Cluster
    instances: tuple[Instance, ...]
    is_specific: bool


@dataclass(frozen=True)
class Topology:
    clusters: dict[str, Cluster]
    by_alias: dict[str, tuple[str, Instance]]

    def is_cluster(self, name: str) -> bool:
        return name in self.clusters

    def is_alias(self, name: str) -> bool:
        return name in self.by_alias

    def resolve(self, name: str) -> Target:
        if name in self.clusters:
            cluster = self.clusters[name]
            return Target(cluster, cluster.instances, is_specific=False)
        if name in self.by_alias:
            cluster_name, inst = self.by_alias[name]
            return Target(self.clusters[cluster_name], (inst,), is_specific=True)
        raise KeyError(f"Unknown cluster or ssh alias: {name!r}")

    @property
    def all_aliases(self) -> list[str]:
        return list(self.by_alias.keys())

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
        alias = entry["ssh_alias"]
        instances.append(Instance(ssh=ssh_specs[alias]))

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
            referenced.add(entry["ssh_alias"])

    missing = referenced - declared
    if missing:
        raise ValueError(
            f"ssh aliases referenced in {config_path} but not declared in "
            f"{ssh_config_path}: {sorted(missing)}"
        )

    ssh_specs = {alias: parse_ssh_alias(alias) for alias in referenced}

    clusters: dict[str, Cluster] = {}
    by_alias: dict[str, tuple[str, Instance]] = {}
    for cname, raw_c in raw_clusters.items():
        cluster = _build_cluster(cname, raw_c, defaults, ssh_specs)
        clusters[cname] = cluster
        for inst in cluster.instances:
            if inst.ssh.alias in by_alias:
                prev = by_alias[inst.ssh.alias][0]
                raise ValueError(
                    f"ssh alias {inst.ssh.alias!r} is in both cluster "
                    f"{prev!r} and {cname!r}"
                )
            by_alias[inst.ssh.alias] = (cname, inst)

    return Topology(clusters=clusters, by_alias=by_alias)


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


def unreferenced_ssh_aliases(
    topo: Topology, ssh_config_path: Optional[Path] = None
) -> list[str]:
    """ssh aliases declared in ssh config but not referenced by any cluster."""
    ssh_config_path = ssh_config_path or Path.home() / ".ssh" / "config"
    declared = _declared_ssh_aliases(ssh_config_path)
    return sorted(declared - set(topo.by_alias.keys()))
