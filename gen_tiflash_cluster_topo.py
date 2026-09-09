#!/usr/bin/env python3
# Copyright 2026 PingCAP, Inc.
"""Generate a tiup topology YAML for a next-gen TiFlash cluster.

Required: `--cluster`, `--dfs-prefix`.
`--tiflash-s3-root` is required when `--cn-mode=disagg` (Write Node / classic
CN objects). When `--cn-mode=columnar` it defaults to empty and may be omitted
(columnar CN reads tikv-worker objects under `--dfs-prefix`).
CN/WN counts and CN binary mode are explicit.

Before deploy, confirm the S3/MinIO bucket + prefixes do not collide with
any existing cluster (same endpoint/bucket with overlapping dfs.prefix or
storage.s3.root will mix or overwrite objects). List the prefixes on the
target bucket and pick unused paths.

CN mode:
  disagg    ENABLE_NEXT_GEN=ON, ENABLE_NEXT_GEN_COLUMNAR=OFF
            flash.use_columnar=false; do not set TIFLASH_COLUMNAR
  columnar  ENABLE_NEXT_GEN=ON, ENABLE_NEXT_GEN_COLUMNAR=ON
            flash.use_columnar=true; set TIFLASH_COLUMNAR=true in run_tiflash.sh
            after deploy (not in this YAML)

Examples:
  python3 gen_tiflash_cluster_topo.py --cluster j4 \\
      --cn-count 2 --wn-count 2 --cn-mode disagg \\
      --dfs-prefix /j4/tikv --tiflash-s3-root /j4/tiflash -o /tmp/j4.yaml
  python3 gen_tiflash_cluster_topo.py --cluster j4 \\
      --cn-count 2 --wn-count 0 --cn-mode columnar --scale full \\
      --dfs-prefix /j4/tikv -o /tmp/j4.yaml

  tiup cluster check /tmp/j4.yaml -y
  tiup cluster deploy j4 v8.5.6 /tmp/j4.yaml --ignore-config-check -y
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

GIB = 1024**3
TIFLASH_PORT_STRIDE = 5


@dataclass(frozen=True)
class Ports:
    pd_client: int = 6540
    pd_peer: int = 7040
    tidb_system: int = 8040
    tidb_system_status: int = 8540
    tidb_ks1: int = 8041
    tidb_ks1_status: int = 8541
    tikv: int = 7540
    tikv_status: int = 16540
    tikv_worker: int = 19040
    prometheus: int = 21040
    prometheus_ng: int = 23040
    grafana: int = 21540
    node_exporter: int = 9740
    blackbox: int = 9840


@dataclass(frozen=True)
class TiFlashPorts:
    tcp: int
    http: int
    flash_service: int
    flash_proxy: int
    flash_proxy_status: int
    metrics: int
    role: str  # tiflash_write | tiflash_compute


@dataclass(frozen=True)
class Scale:
    name: str
    cn_cache_bytes: int
    tiflash_main_bytes: int
    tikv_capacity: str


SCALES = {
    "smoke": Scale("smoke", 50 * GIB, 50 * GIB, "1TiB"),
    "full": Scale("full", 200 * GIB, 200 * GIB, "2TiB"),
}


def ports_from_tcp(tcp: int, role: str) -> TiFlashPorts:
    # Matches the j4 table: 5040 -> 4540/9540/9040/20040/20540.
    return TiFlashPorts(
        tcp=tcp,
        http=tcp - 500,
        flash_service=tcp + 4500,
        flash_proxy=tcp + 4000,
        flash_proxy_status=tcp + 15000,
        metrics=tcp + 15500,
        role=role,
    )


def tiflash_instances(cn_count: int, wn_count: int, cn_tcp_base: int, wn_tcp_base: int) -> list[TiFlashPorts]:
    insts = [ports_from_tcp(cn_tcp_base + i * TIFLASH_PORT_STRIDE, "tiflash_compute") for i in range(cn_count)]
    insts += [ports_from_tcp(wn_tcp_base + i * TIFLASH_PORT_STRIDE, "tiflash_write") for i in range(wn_count)]
    tcps = [i.tcp for i in insts]
    if len(tcps) != len(set(tcps)):
        raise SystemExit(f"TiFlash tcp_port collision: {tcps}. Raise --wn-tcp-base or lower --cn-count.")
    return insts


def store_type(cn_mode: str, wn_count: int) -> str:
    if cn_mode == "columnar":
        return "columnar" if wn_count == 0 else "both"
    return "tiflash"


def _tiflash_block(host: str, deploy_dir: str, inst: TiFlashPorts, scale: Scale, cn_mode: str) -> str:
    data_dir = f"{deploy_dir}/tiflash-{inst.tcp}/data"
    lines = [
        f"  - host: {host}",
        f"    tcp_port: {inst.tcp}",
        f"    http_port: {inst.http}",
        f"    flash_service_port: {inst.flash_service}",
        f"    flash_proxy_port: {inst.flash_proxy}",
        f"    flash_proxy_status_port: {inst.flash_proxy_status}",
        f"    metrics_port: {inst.metrics}",
        "    config:",
        f"      tcp_port: {inst.tcp}",
        f"      flash.disaggregated_mode: {inst.role}",
        f"      storage.main.dir: [{data_dir}]",
        f"      storage.main.capacity: [{scale.tiflash_main_bytes}]",
    ]
    if inst.role == "tiflash_compute":
        use_columnar = cn_mode == "columnar"
        lines += [
            f"      flash.use_columnar: {'true' if use_columnar else 'false'}",
            f"      storage.remote.cache.dir: {data_dir}/cache",
            f"      storage.remote.cache.capacity: {scale.cn_cache_bytes}",
        ]
    return "\n".join(lines)


def render(
    *,
    cluster: str,
    host: str,
    user: str,
    deploy_dir: str,
    s3_endpoint: str,
    s3_bucket: str,
    s3_key: str,
    s3_secret: str,
    s3_region: str,
    dfs_prefix: str,
    tiflash_s3_root: str,
    scale: Scale,
    ports: Ports,
    cn_count: int,
    wn_count: int,
    cn_mode: str,
    cn_tcp_base: int,
    wn_tcp_base: int,
) -> str:
    if cn_count < 0 or wn_count < 0:
        raise SystemExit("--cn-count and --wn-count must be >= 0")
    if cn_count == 0 and wn_count == 0:
        raise SystemExit("need at least one TiFlash node: --cn-count or --wn-count")
    if cn_mode == "disagg" and wn_count == 0:
        raise SystemExit("disagg CN requires at least one Write Node (--wn-count >= 1)")

    insts = tiflash_instances(cn_count, wn_count, cn_tcp_base, wn_tcp_base)
    columnar_store = store_type(cn_mode, wn_count)
    build_columnar = cn_mode == "columnar"
    worker_compact = f"http://{host}:{ports.tikv_worker}/compact"
    worker_url = f"{host}:{ports.tikv_worker}"
    tiflash_blocks = "\n".join(_tiflash_block(host, deploy_dir, inst, scale, cn_mode) for inst in insts)
    tiflash_note = (
        "set TIFLASH_COLUMNAR=true on each CN run_tiflash.sh after deploy; never on WN"
        if cn_mode == "columnar"
        else "do not set TIFLASH_COLUMNAR on any node"
    )

    return f"""# tiup topology for cluster {cluster}
# Generated by gen_tiflash_cluster_topo.py
#   --cluster {cluster} --cn-count {cn_count} --wn-count {wn_count} --cn-mode {cn_mode} --scale {scale.name}
# cse.columnar-store-type={columnar_store} kvengine.build-columnar={str(build_columnar).lower()}
# CN binary: {cn_mode}; {tiflash_note}
# S3 dfs.prefix={dfs_prefix} storage.s3.root={tiflash_s3_root} — confirm unused on the bucket before deploy.
#
#   tiup cluster check <this-file> -y
#   tiup cluster deploy {cluster} v8.5.6 <this-file> --ignore-config-check -y

global:
  user: "{user}"
  ssh_port: 22
  deploy_dir: "{deploy_dir}"

monitored:
  node_exporter_port: {ports.node_exporter}
  blackbox_exporter_port: {ports.blackbox}

server_configs:
  tidb:
    binlog.enable: false
    binlog.ignore-error: false
    log.slow-threshold: 300
    performance.txn-total-size-limit: 53687091200
  tikv:
    dfs.prefix: "{dfs_prefix}"
    dfs.s3-bucket: {s3_bucket}
    dfs.s3-endpoint: {s3_endpoint}
    dfs.s3-key-id: {s3_key}
    dfs.s3-region: {s3_region}
    dfs.s3-secret-key: {s3_secret}
    log.file.max-backups: 30
    raftstore.apply-pool-size: 4
    raftstore.capacity: {scale.tikv_capacity}
    raftstore.store-pool-size: 4
    storage.api-version: 2
    storage.block-cache.capacity: 8GB
    storage.enable-ttl: true
  pd:
    replication.location-labels: [zone, host]
    replication.max-replicas: 1
  tiflash:
    logger.level: info
    storage.api_version: 2
    storage.s3.access_key_id: {s3_key}
    storage.s3.bucket: {s3_bucket}
    storage.s3.endpoint: {s3_endpoint}
    storage.s3.root: "{tiflash_s3_root}"
    storage.s3.secret_access_key: {s3_secret}
  tiflash-learner:
    dfs.prefix: "{dfs_prefix}"
    dfs.s3-bucket: {s3_bucket}
    dfs.s3-endpoint: {s3_endpoint}
    dfs.s3-key-id: {s3_key}
    dfs.s3-region: {s3_region}
    dfs.s3-secret-key: {s3_secret}
    log.file.max-backups: 10
    raftstore.apply-low-priority-pool-size: 4
    raftstore.apply-pool-size: 4
    raftstore.capacity: 500GiB
    raftstore.store-pool-size: 4
    server.snap-max-write-bytes-per-sec: 400MB
    storage.api-version: 2
    storage.enable-ttl: true

pd_servers:
  - host: {host}
    client_port: {ports.pd_client}
    peer_port: {ports.pd_peer}
    config:
      keyspace.pre-alloc:
        - ks1

tidb_servers:
  - host: {host}
    port: {ports.tidb_system}
    status_port: {ports.tidb_system_status}
    config:
      cse.columnar-store-type: {columnar_store}
      disaggregated-tiflash: true
      instance.tidb_service_scope: dxf_service
      keyspace-name: SYSTEM
      tikv-worker-url: "{worker_url}"
  - host: {host}
    port: {ports.tidb_ks1}
    status_port: {ports.tidb_ks1_status}
    config:
      cse.columnar-store-type: {columnar_store}
      disaggregated-tiflash: true
      instance.tidb_service_scope: dxf_service
      keyspace-name: ks1
      tikv-worker-url: "{worker_url}"

tikv_servers:
  - host: {host}
    port: {ports.tikv}
    status_port: {ports.tikv_status}
    config:
      dfs.remote-compactor-addr: "{worker_compact}"
      kvengine.build-columnar: {str(build_columnar).lower()}
      server.labels:
        host: tidb-host-machine-1

tikv_worker_servers:
  - host: {host}
    port: {ports.tikv_worker}
    config:
      dfs.prefix: "{dfs_prefix}"
      dfs.s3-bucket: {s3_bucket}
      dfs.s3-endpoint: {s3_endpoint}
      dfs.s3-key-id: {s3_key}
      dfs.s3-region: {s3_region}
      dfs.s3-secret-key: {s3_secret}
      schema-manager.enabled: true
      schema-manager.keyspace-refresh-interval: 1s

tiflash_servers:
{tiflash_blocks}

monitoring_servers:
  - host: {host}
    port: {ports.prometheus}
    ng_port: {ports.prometheus_ng}

grafana_servers:
  - host: {host}
    port: {ports.grafana}
    username: admin
    password: admin
    anonymous_enable: true
"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cluster", required=True, help="tiup cluster name (required)")
    parser.add_argument("--cn-count", type=int, default=2, help="tiflash-compute count")
    parser.add_argument("--wn-count", type=int, default=2, help="tiflash-write count")
    parser.add_argument(
        "--cn-mode",
        choices=("disagg", "columnar"),
        default="disagg",
        help="disagg: classic next-gen CN (COLUMNAR=OFF). columnar: columnar CN (COLUMNAR=ON)",
    )
    parser.add_argument("--cn-tcp-base", type=int, default=5040, help="first CN tcp_port; later CNs add 5")
    parser.add_argument("--wn-tcp-base", type=int, default=5060, help="first WN tcp_port; later WNs add 5")
    parser.add_argument("-o", "--output", type=Path, help="Write YAML here (default: stdout)")
    parser.add_argument(
        "--scale",
        choices=sorted(SCALES),
        default="smoke",
        help="smoke=1 warehouse / 50GiB CN cache; full=1500 warehouse / 200GiB CN cache",
    )
    parser.add_argument("--host", default="10.2.12.81")
    parser.add_argument("--user", default="jaysonhuang")
    parser.add_argument("--deploy-dir", default="/DATA/disk3/jaysonhuang/clusters")
    parser.add_argument("--s3-endpoint", default="http://10.2.12.81:9000")
    parser.add_argument("--s3-bucket", default="jayson-columnar-test")
    parser.add_argument("--s3-key", default="rustfsadmin")
    parser.add_argument("--s3-secret", default="rustfsadmin")
    parser.add_argument("--s3-region", default="local")
    parser.add_argument(
        "--dfs-prefix",
        required=True,
        help="TiKV / worker / tiflash-learner dfs.prefix (required). Confirm it is unused on the target bucket.",
    )
    parser.add_argument(
        "--tiflash-s3-root",
        default="",
        help="TiFlash storage.s3.root. Required when --cn-mode=disagg. "
        "Defaults to empty when --cn-mode=columnar. Confirm it is unused on the target bucket.",
    )
    args = parser.parse_args(argv)
    if args.cn_mode == "disagg" and not args.tiflash_s3_root.strip():
        parser.error("--tiflash-s3-root is required when --cn-mode=disagg")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        text = render(
            cluster=args.cluster,
            host=args.host,
            user=args.user,
            deploy_dir=args.deploy_dir.rstrip("/"),
            s3_endpoint=args.s3_endpoint,
            s3_bucket=args.s3_bucket,
            s3_key=args.s3_key,
            s3_secret=args.s3_secret,
            s3_region=args.s3_region,
            dfs_prefix=args.dfs_prefix,
            tiflash_s3_root=args.tiflash_s3_root,
            scale=SCALES[args.scale],
            ports=Ports(),
            cn_count=args.cn_count,
            wn_count=args.wn_count,
            cn_mode=args.cn_mode,
            cn_tcp_base=args.cn_tcp_base,
            wn_tcp_base=args.wn_tcp_base,
        )
    except SystemExit:
        raise
    if args.output is None:
        sys.stdout.write(text)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
