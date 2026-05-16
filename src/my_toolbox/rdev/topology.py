"""Typed topology for rdev: clusters -> instances -> containers.

Joins ``~/.rdev/config.yaml`` (clusters + which host each instance lives on)
with ``~/.ssh/config`` (hostname/user/port/proxy) via the ``host`` field,
which must be a declared ssh Host.

Override deep-merge order:  defaults -> cluster -> instance.
``Instance.{container,setup}`` is the fully resolved spec consumers read.
``Cluster.{container,setup}`` holds (defaults + cluster) only — kept just
so ``rdev doctor`` can render the per-cluster baseline before instance overrides.

A host may live in multiple clusters; in that case bare-host resolution is
ambiguous and the loader raises, requiring the user to name the cluster.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

import yaml


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
    """A single instance in a cluster, with fully resolved spec."""

    ssh: SshSpec
    container: ContainerSpec  # defaults + cluster + instance overrides
    setup: SetupSpec  # defaults + cluster + instance overrides

    @property
    def sync_target_base(self) -> Path:
        return self.container.host_home


@dataclass(frozen=True)
class Cluster:
    name: str
    # container/setup here are (defaults + cluster) only — NOT what consumers
    # should use for actual ssh/docker ops; those go through Instance.container
    # / Instance.setup. These exist for `rdev doctor` to show the per-cluster
    # baseline against which instance overrides are compared.
    container: ContainerSpec
    setup: SetupSpec
    instances: tuple[Instance, ...]
    status_filter: str

    @property
    def sync_target_base(self) -> Path:
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
    # Defaults-only ContainerSpec (no cluster/instance overrides). None if the
    # yaml's defaults.container is too sparse to form a full spec. Used by
    # `rdev doctor` to annotate cluster-vs-defaults overrides.
    defaults_container: Optional[ContainerSpec] = None

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


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _make_container_spec(d: dict) -> ContainerSpec:
    return ContainerSpec(
        name=d["name"],
        image=d["image"],
        shm_size=d["shm_size"],
        host_root=Path(d["host_root"]),
        home_dir=d["home_dir"],
    )


def _make_setup_spec(d: dict) -> SetupSpec:
    return SetupSpec(
        setup_script=d["setup_script"],
        install_worktree_script=d["install_worktree_script"],
        default_worktree=d.get("default_worktree", "sglang"),
    )


def _build_cluster(
    name: str, raw: dict, defaults: dict, ssh_specs: dict[str, SshSpec]
) -> Cluster:
    # cluster level = defaults + cluster overrides (no instance overrides)
    cluster_merged = _deep_merge(defaults, raw)
    cluster_container_dict = cluster_merged["container"]
    cluster_setup_dict = cluster_merged["setup"]
    cluster_container = _make_container_spec(cluster_container_dict)
    cluster_setup = _make_setup_spec(cluster_setup_dict)
    cluster_status_filter = cluster_merged.get("status_filter", cluster_container.name)

    instances: list[Instance] = []
    for entry in cluster_merged.get("instances", []):
        host = entry["host"]
        # instance level = cluster level + instance overrides
        inst_container_dict = _deep_merge(
            cluster_container_dict, entry.get("container", {})
        )
        inst_setup_dict = _deep_merge(cluster_setup_dict, entry.get("setup", {}))
        instances.append(
            Instance(
                ssh=ssh_specs[host],
                container=_make_container_spec(inst_container_dict),
                setup=_make_setup_spec(inst_setup_dict),
            )
        )

    return Cluster(
        name=name,
        container=cluster_container,
        setup=cluster_setup,
        instances=tuple(instances),
        status_filter=cluster_status_filter,
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

    # Best-effort build of defaults-only ContainerSpec (for doctor annotation).
    # If any required field is missing under defaults.container, doctor will
    # skip cluster-vs-defaults annotation — warn so the silent skip is visible.
    try:
        defaults_container = _make_container_spec(defaults.get("container", {}))
    except KeyError as e:
        missing_field = e.args[0] if e.args else "?"
        print(
            f"  [topology] warn: defaults.container missing field {missing_field!r}; "
            f"cluster-vs-defaults annotation in `rdev doctor` will be skipped",
            file=sys.stderr,
        )
        defaults_container = None

    return Topology(
        clusters=clusters,
        by_host=by_host,
        defaults_container=defaults_container,
    )


def with_overrides(
    instance: Instance,
    *,
    container: Optional[str] = None,
    image: Optional[str] = None,
) -> Instance:
    """Return a new Instance with CLI-time --container / --image applied."""
    if container is None and image is None:
        return instance
    new_container = replace(
        instance.container,
        name=container or instance.container.name,
        image=image or instance.container.image,
    )
    return replace(instance, container=new_container)


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
