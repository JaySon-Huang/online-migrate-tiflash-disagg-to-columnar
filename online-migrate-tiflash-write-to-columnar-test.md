# 在线将 TiFlash Write Node 迁移到 tikv-worker Columnar 的测试计划

- Status: Draft（讨论已结算，尚未落地执行）
- Last Updated: 2026-09-09
- Harness: **tiup cluster only**（不用 docker compose）
- 本文只描述测试怎么做。不包含 topo YAML、操作脚本或一次具体跑出来的数据。

## Table of Contents

- [背景与目标](#背景与目标)
- [非目标](#非目标)
- [术语](#术语)
- [已结算决策](#已结算决策)
- [关键产品事实](#关键产品事实)
- [集群拓扑](#集群拓扑)
- [用例](#用例)
- [规模](#规模)
- [正确性与观察](#正确性与观察)
- [forward 阶段](#forward-阶段)
- [rollback_read_path 阶段](#rollback_read_path-阶段)
- [旁路表 replica 矩阵](#旁路表-replica-矩阵)
- [断言与观测](#断言与观测)
- [明确不做](#明确不做)
- [生产切流（不在本自动化内）](#生产切流不在本自动化内)
- [已确认环境参数](#已确认环境参数)

## 背景与目标

现有 next-gen 集群是 **tiflash-write + tiflash-compute**（Write Node 持有 learner，Compute Node 从 Write Node 拉数据）。最终态是：

- columnar 副本由 **tikv-worker Compaction** 生成并写到 S3
- **Write Node 全部下线**
- Compute Node 从 S3 读 tikv-worker 生成的 columnar 数据

本测试验证这条路径可以在 **持续 CH-benCHmark（TP + AP）** 下走通，并且：

1. 双重物化阶段读仍走 Write Node 时，columnar 文件能按表就绪
2. Compute Node 滚动切到 columnar 读路径后，查询结果与 TiKV 一致
3. **1 classic CN + 1 columnar CN** 的混合窗口中，按 region 分发的读请求仍然正确
4. 改 `cse.columnar-store-type=columnar` 并删除 PD placement-rule 后，Write Node 上的 learner 能被抽空并缩容
5. 在观察期结束、删除 placement-rule 之前，可以把读路径退回 Write Node

`ENABLE_NEXT_GEN` / `ENABLE_NEXT_GEN_COLUMNAR` 是 **编译开关**，不是运行时配置。next-gen 镜像里同时带两套二进制，运行时用 `TIFLASH_COLUMNAR` 选择：

- 未设或 `false` → `/tiflash/binaries/tiflash`
- `true` → `/tiflash/binaries/tiflash-columnar`

切读路径 = 改 `flash.use_columnar` + 设/清 `TIFLASH_COLUMNAR` + **重启该 CN**。`use_columnar` 只在进程启动时生效。

## 非目标

- 不测 classic（非 next-gen）TiFlash 迁到 disagg
- 不在本仓库 fullstack docker compose 上跑这条迁移
- 不把「观察 X 天」做成 CI 门禁；自动化用有限轮对账代替
- 不把 CH-benCHmark 耗时回归当成 fail 条件（只出阶段性报告）
- 不在现有 `j3` 集群上做（j3 已是 columnar CN 拓扑）

## 术语

**Write Node (WN)**：
`flash.disaggregated_mode=tiflash_write` 的 TiFlash 节点。PD label 为 `engine=tiflash`、`engine_role=write`，持有表的 learner peer。
_Avoid_: tiflash 节点（含糊）、升级后的 columnar 节点

**Compute Node (CN)**：
`flash.disaggregated_mode=tiflash_compute` 的 TiFlash 节点。分为 classic CN（读 WN）和 columnar CN（读 CSE `.col`）。同一台机器上通过二进制 + `use_columnar` 区分，不是两种角色名。
_Avoid_: 只说「改配置就能切」——必须换二进制并重启

**Columnar 副本**：
tikv-worker compaction 写到对象存储上的 CSE `.col` 文件。不是 PD learner。
_Avoid_: TiFlash replica、learner、tiflash 副本（后三者指 schema / PD 侧）

**TiFlash replica（schema 标记）**：
`ALTER TABLE … SET TIFLASH REPLICA n` 写进表元数据的 `tiflash_replica.count`。schema-manager 用 `count > 0` 决定是否把该表编进 schema file，从而触发 columnar 构建。WN 路径上同一标记还驱动 PD placement-rule。
_Avoid_: 把它当成 columnar 已就绪

**双重物化**：
`cse.columnar-store-type=both` 且 TiKV `kvengine.build-columnar=true` 时，同一张 `count > 0` 的表同时有 WN learner 和 `.col`。读路径仍由 CN 的 `use_columnar` 决定。
_Avoid_: 双写（TiKV 行存本来就在写）

**读路径回退**：
只把 CN 从 columnar 二进制切回 classic。不停 `build-columnar`，不改回 `tiflash` store-type，不删 placement-rule，不缩容 WN。

## 已结算决策

| 项 | 选择 |
|---|---|
| 产物 | 可重复的 tiup 实验室测试 + 阶段耗时报告；生产逐步切流只写 runbook |
| 双重物化如何验收文件 | metrics / 日志 / `columnar_status`，不上影子 CN |
| 正确性 | 每个干净读路径阶段，以及混合 CN 窗口，同一组 AP 聚合 SQL：`tidb_isolation_read_engines=tiflash` 与 `tikv` 必须一致 |
| 耗时 | 只记录，不因变慢 fail |
| CN 切换 | 镜像双二进制 + `TIFLASH_COLUMNAR` + `flash.use_columnar` + 重启 |
| store-type | 起始缺省/`tiflash` → 双重物化开始改为 `both` → 观察期过后、删 placement-rule **之前**改为 `columnar` |
| 打开双重物化 | 改 toml，**重启 TiKV 和 TiDB**，不用 `POST /build_columnar` |
| 回退 | 只退读路径；独立用例，不插在 forward 曲线中间 |
| 负载 | CH-benCHmark 持续 TP + AP；WN replica 可用后立刻起，重启打在曲线中间 |
| replica 加减 | 三个 epoch 都测，对象是旁路库，不动 CH 表 |
| 拓扑 | 2 WN + 2 CN |
| replica count | 全部相关表 `SET TIFLASH REPLICA 2` |
| 混合 CN | 滚动切换；1 classic + 1 columnar 时读请求必须正确（按 region 分片） |
| 数据面 | 用户 keyspace（如 `ks1`）；SYSTEM 只做集群必需 |
| 旁路 replica 与 CH | 同一集群、CH 不停，旁路库并行做 SET 0/1 |
| 规模 | 1 warehouse 只验证流程；1500 warehouse 跑 `forward` + `rollback_read_path`，作为正式结果 |

## 关键产品事实

### 读路径与混合窗口

TiDB 在 disagg 下把 cop/MPP **按 region（key range）** 打到某个 `tiflash_compute`（consistent hash 或 RR）。`flash.use_columnar` 是 **进程级** 的，不能在同一个 CN 内按 region 切换读路径。

因此「1 classic + 1 columnar」的含义是：

- 同一条 SQL 的不同 region 片段可能分别打到 classic CN（读 WN）和 columnar CN（读 CSE）
- TiDB 合并各片段后得到最终结果
- **切第一台 CN 之前**，所有 `tiflash_replica.count > 0` 的表必须 `columnar_status.ready==total` 且 `total > 0`，表示各 shard 已启用该表的 columnar 元数据。这不是「L0 已全部转成 `.col`」

持续 TP 时，tikv-worker 把行存 L0 转成 `.col` 可能落后 WN，**不影响 columnar 读的正确性**。columnar CN 向 TiKV leader 拉 kvengine snapshot（`/kvengine/snapshot/...`）：snapshot 带上 **memtable** 以及尚未转换成 columnar 的 **L0**，再与已有 `.col` 合并，在该 snapshot 上算出正确结果。混合窗口对账 **不要** 等待 `unconverted-l0-count` 收敛。

### store-type 与 placement-rule

TiDB `cse.columnar-store-type`：

- `tiflash` / `both`：`IsTiFlashEnabled()==true`，DDL 协程会扫描缺失的 WN placement-rule 并补写
- `columnar`：停止补写，**不会自动删除**已有规则

因此下线 WN 的顺序必须是：

1. `both` → `columnar`（关掉修复循环）
2. 删除 PD group `tiflash` 的规则
3. 等两个 WN 的 `regions/store/<id>` 都为 0
4. `tiup cluster scale-in` WN

若仍停在 `both` 就删 rule，Replica Manager 会把规则修回来。

禁止用 `SET TIFLASH REPLICA 0` 抽 CH 表的 WN learner：`count=0` 会让 schema-manager 不再为该表生成 columnar schema。

### schema-manager 与小 keyspace

当前 CSE **没有** `schema-refresh-threshold` 配置项（已在 `#2791` 删除）。过期文档仍写「小于 256MB 不同步」，不要再配这个键（serde 会忽略）。

`validate_keyspace_for_refresh` 会 skip 的情况：

- default keyspace
- blacklist
- shard stats 未覆盖完整 keyspace range
- 单 shard 且 write sequence 未变
- **`keyspace_total_size == 0`**
- restore 进行中

实验室只要 `schema-manager.enabled=true`，并且 CH 灌数后 keyspace 不是空的。`keyspace-refresh-interval` 默认 60s，实验室可调到约 `1s`，否则 SET REPLICA 后要等很久才进 schema。这是测试偏差，需写进报告。

`kvengine.build-columnar` 标了 `online_config(skip)`。本测试按结算选择 **改 toml 并重启 TiKV**，不用 HTTP 热开。

## 集群拓扑

新建 tiup 集群 **`j4`**，避开 `j3`。主机、端口、S3 prefix 见文末「已确认环境参数」。

| 组件 | 数量 / 要求 |
|---|---|
| PD | ≥1 |
| TiDB | 2：`keyspace-name=SYSTEM` 与 `ks1` |
| TiKV | ≥1（实验室可 1；1500 warehouse 再加） |
| tikv-worker | ≥1，`schema-manager.enabled=true` |
| TiFlash WN | 2，`tiflash_write`，禁止 `TIFLASH_COLUMNAR=true` |
| TiFlash CN | 2，起始 `use_columnar=false` |
| 对象存储 | 与 TiKV `dfs.*`、TiFlash `storage.s3.*` 一致 |

起始配置（部署时）：

```text
tidb:   cse.columnar-store-type 缺省或 "tiflash"
        disaggregated-tiflash = true
tikv:   kvengine.build-columnar = false
        不设 build-fts-index
CN:     flash.disaggregated_mode = tiflash_compute
        flash.use_columnar = false
WN:     flash.disaggregated_mode = tiflash_write
```

CH-benCHmark 与旁路表一律：

```sql
ALTER TABLE <t> SET TIFLASH REPLICA 2;
```

## 用例

| 用例 | 路径 | 结束条件 |
|---|---|---|
| `forward` | 部署 → 灌数起 CH → 双重物化 → 滚动切 CN（含混合窗）→ 稳态对账 → store-type=columnar → 删 placement-rule → 两 WN region=0 → 缩容 WN | CH 仍打在 2 个 columnar CN 上，对账通过 |
| `rollback_read_path` | 与 forward 共用到「两 CN 已是 columnar、store-type 仍是 both、WN 仍在」 | 两 CN 切回 classic，对账回到 WN；**不**改 store-type、不删 rule、不缩容 |

两条用例独立跑，不要在同一条 CH 曲线中间插入回退。两个规模档都要跑这两条用例。

## 规模

| 档 | warehouse | 用途 |
|---|---|---|
| 流程验证 | 1 | 确认 `forward` 与 `rollback_read_path` 整条流程能跑通（阶段顺序、配置切换、门禁、回退）。结果不作数。 |
| 正式结果 | 1500 | 1 warehouse 流程验证通过后执行。同样跑 `forward` 与 `rollback_read_path`。阶段耗时、对账、混合窗正确性以这一档为准。 |

1500 warehouse 需要更大的 TiKV / worker / S3 / 磁盘。流程验证通过之前不要开 1500。

go-tpc（或等价 CH-benCHmark）在 WN replica `AVAILABLE=1` 后立刻起，持续到该用例结束。双重物化重启会打在曲线中间，报告里标出该窗口。

耗时与对账的**正式报告以 1500 warehouse 为准**（`forward` 与 `rollback_read_path` 各一份）。1 warehouse 的曲线只用于确认切窗和门禁脚本没写错。

耗时报告按阶段切窗，至少包括：

1. 部署后（WN 读）
2. 双重物化重启窗口
3. 切 CN0 后（混合）
4. 切 CN1 后（全 columnar CN）
5. 稳态对账
6. 改 store-type / 删 rule / 缩容（仅 `forward`；`rollback_read_path` 切到「切回 classic 后的稳态对账」为止）

## 正确性与观察

**对账 SQL**：固定一组聚合查询（CH AP 中的 `COUNT` / `SUM` / 若干 join 聚合即可），同一 snapshot 语义下：

```sql
SET SESSION tidb_isolation_read_engines = 'tikv';
-- run
SET SESSION tidb_isolation_read_engines = 'tiflash';
-- run；结果必须一致
```

对账相位：

| 相位 | 读路径 | 是否对账 |
|---|---|---|
| 双重物化后、切 CN 前 | 两 CN 都是 classic → WN | 是 |
| 1 classic + 1 columnar | 按 region 分到两种 CN | **是**（Q19） |
| 两 CN 都是 columnar | 全 columnar | 是 |
| 读路径回退后 | 全 classic → WN | 是 |

**观察期（对应原 5.1）**：切完两个 CN 后，连续 **K=10 轮**（或约 1 分钟）对账，一轮失败即 fail。生产「观察 X 天」只属于 runbook。

混合窗与全 columnar 窗：直接做 K 轮对账，不要等 `unconverted-l0-count` 收敛；不允许 AP 持续报错。

## forward 阶段

### 0. 部署

`tiup cluster deploy` + 离线 patch（PD / TiDB / TiKV / tikv-worker / TiFlash）。TiFlash 使用带 wrapper 的 next-gen 镜像，**不要**用单次 cmake 装出来的单二进制目录盖掉 `/tiflash`。

Smoke（ks1）：建表、插入、`SET TIFLASH REPLICA 2`、TiKV 与 tiflash `COUNT` 一致。

### 1. 灌数并起负载

- 在 ks1 灌入当前规模档（1 或 1500 warehouse）的全部 CH 表
- 全部 CH 表 `SET TIFLASH REPLICA 2`
- 等 `information_schema.tiflash_replica` 目标表 `AVAILABLE=1`
- 确认两个 WN 上都能看到 learner（`REPLICA 2`）
- 启动持续 TP + AP

### 2. 打开双重物化

1. `tiup cluster edit-config`：TiDB `cse.columnar-store-type=both`；TiKV `kvengine.build-columnar=true`
2. reload/restart **TiDB 与 TiKV**
3. 在 CH 报告里标注这次中断

门禁：对每张 `tiflash_replica.count > 0` 的表（含 CH 表与已创建的旁路表）：

```text
GET http://<tikv-status>/kvengine/columnar_status?keyspace_id=<ks>&table_id=<id>
```

`ready==total` 且 `total>0`。

然后做 WN 路径对账。旁路库进入 [epoch A](#旁路表-replica-矩阵)。

### 3. 滚动切 CN

**前置**：步骤 2 的 `ready==total` 仍成立。

1. CN0：`flash.use_columnar=true`，`scripts/run_tiflash.sh` 增加 `export TIFLASH_COLUMNAR=true`，重启该实例。进程应显示 `.../tiflash-columnar/tiflash`
2. **混合窗口**：K 轮对账必须通过；CH AP 不得持续报错。不要等 `unconverted-l0-count` 收敛（columnar 读路径会读 snapshot 中的 memtable / 未转换 L0）
3. 旁路库 [epoch B](#旁路表-replica-矩阵)
4. CN1 同样切换
5. 两 CN 都是 columnar 后再 K 轮对账（观察期）

WN 禁止设置 `TIFLASH_COLUMNAR=true`。patch 若重生 `run_tiflash.sh`，必须重新加上环境变量。

### 4. 下线 WN

1. TiDB `cse.columnar-store-type=columnar`，restart TiDB
2. 删除 PD group `tiflash` 的 placement-rule  
   - 查询：`GET /pd/api/v1/config/placement-rule/tiflash`  
   - 按 rule id 删除：`DELETE /pd/api/v1/config/rule/tiflash/{rule_id}`
3. 两个 WN 均 `GET /pd/api/v1/regions/store/<wn_store_id>` 长度为 0（超时则 fail）
4. `tiup cluster scale-in` 两个 WN；必要时 `tiup cluster prune`
5. CH 与对账只打剩余 CN；旁路库 [epoch C](#旁路表-replica-矩阵)

## rollback_read_path 阶段

复用 forward 的 0 → 3（两 CN 已是 columnar，store-type 仍为 `both`，WN 仍在，placement-rule 仍在）。然后：

1. 两 CN：`use_columnar=false`，去掉 `TIFLASH_COLUMNAR`，重启
2. 进程回到 `.../binaries/tiflash/tiflash`
3. K 轮对账必须再次与 TiKV 一致（读 WN）
4. 两个 WN 的 region 数都不为 0
5. **停止该用例**

## 旁路表 replica 矩阵

旁路库（例如 `mig_side`）与 CH 同时存在。CH 表全程保持 `REPLICA 2`，直到 forward 按 runbook 下线 WN。

| Epoch | 集群状态 | `SET REPLICA 2` | `SET REPLICA 0` |
|---|---|---|---|
| A | `both`，两 CN classic | 出现 WN learner **且** `columnar_status` 就绪 | WN learner 与该表 columnar 都消失 |
| B | `both`，1 classic + 1 columnar | 仍应建 WN learner + columnar（store-type 还是 both） | 两边都消失；不要对 CH 表做 |
| C | `columnar`，WN 已抽空或已缩容 | **不得**再出现 WN learner，只应有 columnar | 只停该表 columnar |

每步用：

- `information_schema.tiflash_replica`
- `/kvengine/columnar_status`
- PD `regions/store/<wn>` 是否出现该表 region / learner

## 断言与观测

| 信号 | 用途 | 不能当成 |
|---|---|---|
| `information_schema.tiflash_replica.AVAILABLE` / `PROGRESS` | TiDB 认为 replica 在 | columnar 就绪 |
| `GET /kvengine/columnar_status` `ready==total` | 该表各 shard 已启用 columnar 元数据 | L2 填满；也不是「未转换 L0 已清空」 |
| `unconverted-l0-count` / `unconverted_l0s` | 辅助观察 L0→columnar 是否在追 | **对账前置条件**；columnar 读会覆盖 memtable 与未转换 L0 |
| `GET /kvengine/<region_id>` 的 `columnar-levels` | 抽查 L0/L1/L2 | 主门禁 |
| 固定 AP SQL，tiflash vs tikv | 正确性门 | 耗时门 |
| go-tpc 分阶段耗时 | 报告 | fail 条件 |
| PD `GET /pd/api/v1/regions/store/<wn>` | 抽 learner、缩容 | — |
| `ps` / 进程路径含 `tiflash-columnar` | CN 二进制是否切对 | — |
| metrics：`kv_engine_columnar_files_count`、compaction trigger | 辅助观察双重物化 | 单独过门 |

TiKV HTTP 走 **status** 端口，不要打 gRPC。keyspace_id / table_id 从 ks1 的 `information_schema` 取。

## 明确不做

- docker compose / fullstack-test-next-gen 作为本迁移的执行环境
- `POST /build_columnar?switch=true` 热开
- 影子 CN
- 耗时超过基线 20% 就 fail
- 在现有 `j3` 上回滚出 WN
- CH 打在 SYSTEM keyspace
- 对 CH 表 `SET TIFLASH REPLICA 0` 来下线 WN
- 仍为 `both` 时删除 placement-rule
- 给 WN 设置 `TIFLASH_COLUMNAR=true`
- 打开 `kvengine.build-fts-index`（本测试不需要）

## 生产切流（不在本自动化内）

线上意图是从小集群到大集群逐步切。本文件覆盖的是 **实验室可重复过程**。生产 runbook 另外写，应复用同一阶段顺序和同一组门禁，把 K 轮对账换成约定的观察窗口，并按集群分批切 CN。

## 已确认环境参数

本机 `10.2.12.81`（`10-2-12-81`）上已有 `j1`、`j3`，**新建独立集群，不改 j3**。下列端口已用 `ss` 探测为空闲；MinIO `http://10.2.12.81:9000` 健康检查为 200。

### 集群身份

| 项 | 值 |
|---|---|
| 集群名 | `j4` |
| tiup | 1.17.0（满足 `tikv_worker_servers`） |
| 官方壳版本 | `v8.5.6`（与 j3/j1 相同，真实二进制靠 patch） |
| 部署用户 | `jaysonhuang` |
| SSH | 22 / builtin |
| `deploy_dir` | `/DATA/disk3/jaysonhuang/clusters`（与 j3 同盘，实例目录按端口区分） |
| 主机 | 组件全部放 **`10.2.12.81`**；对象存储也在 **`10.2.12.81:9000`** |

### 端口块（避开 j3 `*30`、j1 `*20`，以及本机已占用的 6550/7550 等）

| 组件 | Host | 端口 |
|---|---|---|
| PD | 10.2.12.81 | client **6540** / peer **7040** |
| TiDB SYSTEM | 10.2.12.81 | sql **8040** / status **8540**，`keyspace-name=SYSTEM` |
| TiDB ks1 | 10.2.12.81 | sql **8041** / status **8541**，`keyspace-name=ks1`（CH 只打这里） |
| TiKV | 10.2.12.81 | grpc **7540** / status **16540** |
| tikv-worker | 10.2.12.81 | **19040**（`dfs.remote-compactor-addr=http://10.2.12.81:19040/compact`） |
| CN0 classic→columnar | 10.2.12.81 | tcp **5040** / http 4540 / flash 9540 / proxy 9040 / proxy-status 20040 / metrics 20540 |
| CN1 classic→columnar | 10.2.12.81 | tcp **5045** / 4545 / 9545 / 9045 / 20045 / 20545 |
| WN0 | 10.2.12.81 | tcp **5060** / 4550 / 9550 / 9050 / 20050 / 20550（不用 5050：disk2 上有遗留 `tiflash-5050` 目录） |
| WN1 | 10.2.12.81 | tcp **5065** / 4555 / 9555 / 9055 / 20055 / 20555 |
| Prometheus | 10.2.12.81 | **21040** / ng **23040** |
| Grafana | 10.2.12.81 | **21540**（admin/admin） |
| node_exporter / blackbox | 10.2.12.81 | **9740** / **9840**（j3=9730/9830，j1=9720/9820） |

PD `keyspace.pre-alloc: [ks1]`。TiDB `instance.tidb_service_scope: dxf_service`，`tikv-worker-url: 10.2.12.81:19040`。

### 对象存储

与 j3 **不要共用同一套 prefix**。j3 的 MinIO 在 `10.2.12.79:9000`、`dfs.prefix=tikv`、`storage.s3.root=/tiflash`。`j4` 改用本机 `10.2.12.81:9000`，prefix 如下。

| 项 | 值 |
|---|---|
| endpoint | `http://10.2.12.81:9000` |
| bucket | `jayson-columnar-test` |
| key / secret | `rustfsadmin` / `rustfsadmin` |
| region | `local` |
| TiKV / worker `dfs.prefix` | **`/j4/tikv`** |
| TiFlash `storage.s3.root` | **`/j4/tiflash`** |
| `storage.api-version` / `api-version` | 2 |

### 机器余量（动手前再看一眼）

| 资源 | 10.2.12.81 | 10.2.12.79 |
|---|---|---|
| CPU | 72 | 72 |
| 内存 | 376 Gi，当前约 225 Gi available | 376 Gi，当前约 206 Gi available |
| 部署盘 | disk3：3.5T，约 **2.3T free**（j3 已占用同盘）；MinIO 在本机 `:9000` | disk3 约 2.7T free（j1 在 79，与 j4 S3 无关） |
| 同机已有 | j3 全套 + j1 的 TiFlash 5022 + 本机 MinIO :9000 + 他人若干服务 | j1 PD/TiDB/TiKV |

**1 warehouse** 流程验证：单机 81 足够。  
**1500 warehouse**：数据在 S3 + 2 个 WN learner + 2 个 CN cache + 与 j3 抢 CPU/内存。CN `storage.remote.cache.capacity` 不要照抄 j3 的 500GB；流程验证用较小 cache（例如 50GB），1500 档再提高到 200GB 量级，并盯 disk3 剩余。若 1500 跑不起来，再考虑把 WN 或 TiKV 拆到 79 的 `/DATA/disk3`，而不是挤爆 81。

### Patch 来源

部署后、start 前离线 patch，与 j3 相同流程。

| 组件 | 推荐 |
|---|---|
| 默认 | `tiflash-2/tests/docker/next-gen-utils`：`make download PULL=1 && make package`，再 `tiup cluster patch -R pd,tidb,tikv,tikv-worker,tiflash --offline` |
| 镜像 | Makefile 默认 `*-nextgen` / `cloud-engine-nextgen`（需 gcloud 登录 `us-docker.pkg.dev`） |
| 本机已有（可能过期） | `/DATA/disk1/jaysonhuang/my-patches/tiflash-patch.tar.gz`（2026-09-07，147M）；`tests/docker/next-gen-utils/binaries/package/tiflash.tar.gz`（2026-09-07，710M，更像镜像整树）；`my-patches/tikv.tar.gz`（2026-09-04） |
| 本地改代码时 | skill `local-compile-patch.md`；TiFlash 必须 `source /data1/ra_common/.tiflash_env_17` |

TiFlash 必须是 **双二进制 wrapper 包**（`/tiflash/tiflash` + `binaries/{tiflash,tiflash-columnar}`）。不要用单次 cmake install 目录当 patch。

CN 切 columnar 时在该实例 `scripts/run_tiflash.sh` 加 `export TIFLASH_COLUMNAR=true`；**WN 禁止加**。patch 若重生脚本要重加。

### 仓库路径（本机）

| 组件 | 路径 |
|---|---|
| TiFlash | `/DATA/disk1/jaysonhuang/tiflash-2` |
| CSE / TiKV | `/DATA/disk1/jaysonhuang/cloud-storage-engine` |
| TiDB | `/DATA/disk1/jaysonhuang/tidb` |
| PD | `/DATA/disk1/jaysonhuang/pd` |
| patch 输出 | `/DATA/disk1/jaysonhuang/my-patches` |

未生成 topo YAML，也未执行 `tiup cluster deploy`。生成脚本：

```bash
python3 gen_tiflash_cluster_topo.py --cluster j4 \
    --cn-count 2 --wn-count 2 --cn-mode disagg \
    --dfs-prefix /j4/tikv --tiflash-s3-root /j4/tiflash \
    -o /tmp/j4-tiflash-write.yaml
python3 gen_tiflash_cluster_topo.py --cluster j4 \
    --cn-count 2 --wn-count 2 --cn-mode disagg --scale full \
    --dfs-prefix /j4/tikv --tiflash-s3-root /j4/tiflash \
    -o /tmp/j4-tiflash-write.yaml
```

