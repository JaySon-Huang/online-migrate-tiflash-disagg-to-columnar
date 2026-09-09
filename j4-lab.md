# j4 实验室记录：部署结果与 CH-benCHmark 导入

- Date: 2026-09-09
- Cluster: **`j4`**（新建，未改 `j1` / `j3`）
- Status: 起始态已部署并 smoke 通过；**1 warehouse CH 已灌入 ks1 `tpcc`**（replica 2 `AVAILABLE=1`，未 `run` 持续负载）
- 测试计划仍以 [online-migrate-tiflash-write-to-columnar-test.md](./online-migrate-tiflash-write-to-columnar-test.md) 为准。本文只记这次落地的事实和导入命令。

## 部署结果

### 怎么部署的

1. 脚本生成起始态 topo（`--cn-mode disagg`，2 CN + 2 WN）：

```bash
python3 gen_tiflash_cluster_topo.py --cluster j4 \
    --cn-count 2 --wn-count 2 --cn-mode disagg \
    --dfs-prefix /j4/tikv --tiflash-s3-root /j4/tiflash \
    -o topo/j4-tiflash-write.yaml
```

`topo/` 已进 `.gitignore`，不要把带密钥的 YAML 提交进 git。

2. `tiup cluster check` 有实验室常见 Fail（同盘、ulimit、THP、sysctl），与 j3 同类，用 `--ignore-config-check` 继续。
3. 第一次 `deploy` 失败：TiKV `server.labels.host` 存在，PD 没有 `replication.location-labels`。脚本已补 `replication.location-labels: [zone, host]`（commit `5d7648e`）。
4. `tiup cluster deploy j4 v8.5.6 topo/j4-tiflash-write.yaml --ignore-config-check -y`
5. 从最新 next-gen 镜像抽 binary，offline patch 后再 `tiup cluster start j4 -y`。

镜像：`us-docker.pkg.dev/.../*-nextgen` / `cloud-engine-nextgen`。  
`make download` 在无 TTY 下会因 Makefile 的 `docker run -it` 报 `the input device is not a TTY`；实际抽取时去掉了 `-it`。  
gcloud：`/DATA/disk1/ra_common/common_tools/google-cloud-sdk/bin/gcloud`。

包路径：`/DATA/disk1/jaysonhuang/tiflash-2/tests/docker/next-gen-utils/binaries/package/`

### 集群身份

| 项 | 值 |
|---|---|
| 集群名 | `j4` |
| 官方壳 | `v8.5.6`（真实二进制靠 patch） |
| 主机 | `10.2.12.81` |
| `deploy_dir` | `/DATA/disk3/jaysonhuang/clusters` |
| 起始 store-type | `cse.columnar-store-type=tiflash` |
| `kvengine.build-columnar` | `false` |
| CN | `flash.use_columnar=false`，**未设** `TIFLASH_COLUMNAR` |
| WN | 禁止 `TIFLASH_COLUMNAR` |
| 进程 | 四个 TiFlash 都是 `.../binaries/tiflash/tiflash`（classic） |

### 端口

| 组件 | 端口 |
|---|---|
| PD | client **6540** / peer 7040 |
| TiDB SYSTEM | sql **8040** / status 8540 |
| TiDB ks1 | sql **8041** / status 8541（CH 只打这里） |
| TiKV | grpc **7540** / status **16540** |
| tikv-worker | **19040** |
| CN0 / CN1 | tcp **5040** / **5045** |
| WN0 / WN1 | tcp **5060** / **5065** |
| Prometheus | 21040 |
| Grafana | [http://10.2.12.81:21540](http://10.2.12.81:21540)（admin/admin） |

`tiup cluster display j4`：11 个节点全部 **Up \| patched**。

### 对象存储

| 项 | 值 |
|---|---|
| endpoint | `http://10.2.12.81:9000` |
| bucket | `jayson-columnar-test` |
| TiKV / worker `dfs.prefix` | `/j4/tikv` |
| TiFlash `storage.s3.root` | `/j4/tiflash` |

部署前 bucket 为空，prefix 未与 j3 重叠。j3 MinIO 在 `10.2.12.79:9000`。

### Patch 二进制（镜像日期 2026-09-09）

| 组件 | 版本 / commit |
|---|---|
| TiKV / CSE | 26.3.0-cse `6187f22a7fe56032f05c75a4f7aedd42b098f045` |
| PD | v9.0.0-beta.2.pre `5abc379473c20d20273c5fb7af6ff8097a26e8a5` |
| TiDB | CLOUD.202609.0 `5acf6574288567bb473762e42313e25880c02419` |
| TiFlash | v27.0.0-pre `91e13c098ef2b53d28159106626f255d37e10695`（双二进制 wrapper） |

TiFlash tarball 含 `binaries/{tiflash,tiflash-columnar}/` + wrapper `tiflash`。

### Smoke（ks1）

库 `smoke.t`，100 行，`SET TIFLASH REPLICA 2`，`AVAILABLE=1`。

| engine | COUNT | SUM(v) |
|---|---|---|
| tikv | 100 | 49500 |
| tiflash | 100 | 49500 |

```bash
mysql -h 10.2.12.81 -P 8041 -u root
```

## CH-benCHmark 导入（1 warehouse）

### 规模参数

CH-benCHmark **没有 `--sf`**。`--sf=1` 是 `tiup bench tpch` 的 TPC-H scale factor。

本测试的「1 warehouse / sf=1 流程验证」用：

- `--warehouses 1`
- 连 **ks1 `8041`**，不要打 SYSTEM `8040`
- 库名 **`tpcc`**（TP 表和 AP 附加表必须同库）

官方两步灌数：[How to Run CH-benCHmark](https://docs.pingcap.com/tidb/stable/benchmark-tidb-using-ch/)。  
**不能**只跑 `tiup bench ch --sf=1 prepare`：`ch prepare` 只补 nation / region / supplier + `revenue1`，TPC-C 九张表要靠 `tiup bench tpcc prepare`。

`ch prepare --tiflash-replica 2 --analyze` 只覆盖 CH 附加表，不能替代下面步骤 4/5。

### 步骤

已有 `smoke.t` 在 ks1，CH 用独立库 `tpcc`。1 warehouse 灌完后再 `SET TIFLASH REPLICA 2`；**不要**对 CH 表 `SET REPLICA 0`。

1. 灌 TPC-C

```bash
tiup bench tpcc \
  -H 10.2.12.81 -P 8041 -U root \
  -D tpcc --warehouses 1 -T 8 \
  --dropdata prepare
```

2. 校验 TPC-C

```bash
tiup bench tpcc -H 10.2.12.81 -P 8041 -U root -D tpcc --warehouses 1 check
```

3. 灌 CH 附加表

```bash
tiup bench ch \
  -H 10.2.12.81 -P 8041 -U root \
  -D tpcc --warehouses 1 prepare
```

日志应出现 `creating nation` / `region` / `supplier` 和 `creating view revenue1`。

4. 全部 CH 表开 replica 2，等到 `AVAILABLE=1`

```sql
-- mysql -h 10.2.12.81 -P 8041 -u root
ALTER DATABASE tpcc SET TIFLASH REPLICA 2;

SELECT TABLE_SCHEMA, TABLE_NAME, REPLICA_COUNT, AVAILABLE, PROGRESS
FROM information_schema.tiflash_replica
WHERE TABLE_SCHEMA = 'tpcc';
```

确认两个 WN（5060 / 5065）上都有 learner。

5. 收集统计（AP 计划依赖这个）

```sql
SET GLOBAL tidb_analyze_column_options = 'ALL';
ANALYZE TABLE customer, district, history, item, new_order,
  order_line, orders, stock, warehouse, nation, region, supplier;
```

### 导入后不要自动 `run`

测试计划：replica `AVAILABLE=1` 之后才起持续 TP + AP。要压测时再：

```bash
tiup bench ch -H 10.2.12.81 -P 8041 -U root \
  -D tpcc --warehouses 1 run -T 4 -t 1 --time 1h
```

`-S` 默认 10080；prepare 一般不需要。若 bench 报 status 错，加 `-S 8541`。

1500 warehouse：同一套命令把 `--warehouses` 改成 `1500`，并先把集群 `--scale full`（见测试计划）。流程验证通过前不要开 1500。

## 1 warehouse 导入结果（2026-09-09）

已执行上面步骤 1–5，**未** `tiup bench ch run`。

- TPC-C `prepare` + `check` 通过（tiup bench v1.12.0）
- `ch prepare` 建成 `nation` / `region` / `supplier` / view `revenue1`
- `ALTER DATABASE tpcc SET TIFLASH REPLICA 2`：12 张基表 `AVAILABLE=1`、`PROGRESS=1`（view 无 replica 行）
- 两个 WN（store 292 `:9560`、293 `:9565`）各 13 个 region；两个 CN region=0
- `tidb_analyze_column_options=ALL`，已 ANALYZE 12 张基表

| 表 | tikv COUNT |
|---|---|
| warehouse | 1 |
| district | 10 |
| customer | 30000 |
| item | 100000 |
| stock | 100000 |
| orders | 30000 |
| new_order | 9000 |
| order_line | 300029 |
| history | 30000 |
| nation | 25 |
| region | 5 |
| supplier | 10000 |

抽查 tiflash：`order_line` 300029、`customer` 30000、`stock` 100000、`supplier` 10000，与 TiKV 一致。
