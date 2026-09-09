# Handoff: 在线将 TiFlash disagg 迁到 tikv-worker columnar

**Date:** 2026-09-09  
**From session:** [online migrate plan](b82293f5-1288-42d3-8d18-d4abdf1e43c0)  
**Next session focus:** `j4` 已切 **CN0（5040）**，混合窗 K=10 对账已过。下一步是旁路库 epoch B，然后切 CN1（5045）。不要重开设计。不要动 `j1` / `j3`。不要对 CH 表 `SET REPLICA 0`。WN 禁止 `TIFLASH_COLUMNAR`。

## Goal

验证 next-gen **tiflash-write + classic CN** 可以在持续 CH-benCHmark（TP+AP）下在线迁到 **tikv-worker columnar**：CN 读 S3 上的 `.col`，Write Node 全部下线。这不是 classic→disagg 升级。

`ENABLE_NEXT_GEN` / `ENABLE_NEXT_GEN_COLUMNAR` 是编译开关。运行时靠 `flash.disaggregated_mode`、`flash.use_columnar`、`TIFLASH_COLUMNAR`、TiDB `cse.columnar-store-type`、TiKV `kvengine.build-columnar`。

## Canonical artifacts（不要把正文抄进回复）

| 内容 | 位置 |
|---|---|
| 本交接文档 | [handoff.md](./handoff.md) |
| j4 部署结果与 CH 导入 | [j4-lab.md](./j4-lab.md) |
| 测试计划（阶段、门禁、环境、明确不做） | [online-migrate-tiflash-write-to-columnar-test.md](./online-migrate-tiflash-write-to-columnar-test.md) |
| 拓扑生成脚本 | [gen_tiflash_cluster_topo.py](./gen_tiflash_cluster_topo.py) |
| GitHub repo | https://github.com/JaySon-Huang/online-migrate-tiflash-disagg-to-columnar |
| 本地 clone | `/DATA/disk1/jaysonhuang/online-migrate-tiflash-disagg-to-columnar` |
| tiflash-2 工作副本（文件名带日期，改 CLI 时两边一起改） | `/DATA/disk1/jaysonhuang/tiflash-2/docs/design/2026-09-09-online-migrate-tiflash-write-to-columnar-test.md`、`.../gen_tiflash_cluster_topo.py` |

两份脚本/文档应视为同源。本仓库示例命令用仓库根路径；tiflash-2 里脚本 `__doc__` 仍写 `docs/design/gen_tiflash_cluster_topo.py`。

## 当前进度（2026-09-09 17:24 左右）

**已完成**

- 设计 grilling 已收口；测试计划 Status=Draft。
- **`j4` 已 deploy / offline patch / start**，起始态 2 classic CN + 2 WN。细节：[j4-lab.md](./j4-lab.md)。
- 1 warehouse CH 已灌入 ks1 `tpcc`（12 张基表 replica 2，`AVAILABLE=1`，已 ANALYZE）。`smoke.t` 仍在，table_id 17。
- 持续负载已拆成 **两个 tiup bench 进程**（ks1 `8041`，库 `tpcc`，`--time 24h` 只是上限）：
  - TP：`tiup bench tpcc run -T 4 --ignore-error --interval 10s` → [`logs/tpcc-tp.log`](logs/tpcc-tp.log)
  - AP：`tiup bench ch run -T 0 -t 1`，`tidb_isolation_read_engines=tiflash` → [`logs/ch-ap.log`](logs/ch-ap.log)
  - `-T 0` 才是 AP-only（Q1–Q22）。**不要**给 TP 进程加 tiflash isolation。`--ignore-error` 是为了扛双重物化重启。
  - `logs/*.log` 已 gitignore，不要把持续输出提交进去。
- 双重物化已打开并 reload **TiDB + TiKV**（未 reload TiFlash / worker，未设 `TIFLASH_COLUMNAR`）：
  - TiDB `cse.columnar-store-type=both`（8040 与 8041）
  - TiKV `kvengine.build-columnar=true`（必须重启，`online_config(skip)`）
- **forward 阶段 2 门禁已过**（切 CN 之前）：
  - ks1 `keyspace_id=1` 上 13 张 `tiflash_replica.count>0` 的表全部 `columnar_status.ready==total==1`
  - S3 `jayson-columnar-test/j4/tikv` 已有 `.col`；部分 region（如 364）`columnar-levels` 非空。这是辅助证据，不是主门禁
  - WN 路径对账：同一 `tidb_snapshot` 下 tikv vs tiflash，连续 3 轮 19 条聚合全部一致（见下节）
- **旁路库 epoch A 已过**（`mig_side`，ks1 `8041`）。CH 表全程未 `SET REPLICA 0`。见下节。
- **CN0 已切到 columnar，混合窗 K=10 对账已过**（见下节）。当前进程：
  - CN0 `10.2.12.81:5040` → `.../tiflash-columnar/tiflash`，`use_columnar=true`，`TIFLASH_COLUMNAR=true`
  - CN1 `10.2.12.81:5045` → `.../binaries/tiflash/tiflash`，`use_columnar=false`
  - WN `5060` / `5065` → classic，**没有** `TIFLASH_COLUMNAR`

**未做（下一 agent 的工作面）**

- 旁路库 epoch B（混合窗下对 `mig_side` SET 2/0）
- 切 CN1（5045）→ 全 columnar CN → 观察期 K=10
- 之后 `forward`：`store-type=columnar`、删 PD tiflash rules、WN region=0、scale-in WN
- 独立用例 `rollback_read_path`
- 1500 warehouse、Grafana import、把对账收成仓库脚本

## 旁路库 epoch A（2026-09-09）

集群状态当时：`cse.columnar-store-type=both`，两 CN classic。库名 **`mig_side`**，不要对 `tpcc` / `smoke` 做 SET 0/1。

| 表 | table_id | 操作 | 结束态 |
|---|---|---|---|
| `mig_side.keep2` | 75 | `SET TIFLASH REPLICA 2`，留下 | replica 2 / AVAILABLE=1；region **410** 在 WN 292+293 有 learner；`columnar_status.ready=1,total=1` |
| `mig_side.flip` | 77 | 先 SET 2 再 SET 0 | replica 行消失；region **415** 只剩 TiKV voter；`ready=0,total=1`；行存仍在（COUNT=1000） |

两张表各 1000 行，`SUM(v)=5005000`。`keep2` 同一 snapshot 下 tikv / tiflash COUNT/SUM 一致。WN region 数：13（CH+smoke）→ SET 2 后 15 → `flip` SET 0 后 **14**。CH + `smoke.t` 仍是 13 张 `REPLICA 2 AVAILABLE=1`。

### 怎么看 WN learner

`SHOW TABLE <t> REGIONS` 的 `PEERS` 列是 **peer id**，不是 store id。要用 PD：

```text
GET http://10.2.12.81:6540/pd/api/v1/region/id/<region_id>
```

learner：`store_id` ∈ {**292** (`:9560`), **293** (`:9565`)} 且 `role_name=Learner`。CN store 294/295 的 region 数应仍为 0。

### SET 0 时不要把 `total==0` 当消失

`GET /kvengine/columnar_status` 的语义（CSE `collect_columnar_status`）：

- **`total`**：key range 覆盖该表的 shard 数（行存 region 还在就会 ≥1）
- **`ready`**：这些 shard 里 `has_columnar_table(table_id)` 为真的数量（schema 已安装）

因此：

- SET 2 就绪：`ready==total` 且 `total>0`（与 CH 表门禁相同）
- SET 0 「columnar 消失」：等 **`ready==0`**（实验室约 3s）。**不要**等 `total==0`，表没 DROP 的话 `total` 会一直是 1。曾经按 `total==0` 空等 180s 超时，那是误判。

对照：`GET /kvengine/<region_id>` 的 `columnar-tables`，`flip` SET 0 后为 0，`keep2` 仍为 1。

epoch B 应复用这两张表：`keep2` 已是 replica 2；`flip` 再 SET 2 测混合窗下新建 learner+columnar。仍不要动 CH 表。

## CN0 混合窗（2026-09-09）

CN0 = **`10.2.12.81:5040`**（测试计划端口表）。CN1 = `5045`，先不要切。

### 切 CN 的正确顺序（实验室踩过）

`tiup cluster reload` **会重生** `scripts/run_tiflash.sh`，写在 reload 之前的 `TIFLASH_COLUMNAR` 会被抹掉。

1. 切前再确认当前 `count>0` 表 `columnar_status.ready==total`（含 `keep2`，不含 `flip`）。
2. `tiup cluster show-config j4` 改 **仅该 CN** 的 `flash.use_columnar: true`，`tiup cluster edit-config j4 --topology-file … -y`。不要用 python `yaml.dump` 回写整份 topo（会制造大段空白 diff；只改那一个键）。
3. `tiup cluster reload j4 -N 10.2.12.81:<tcp> --ignore-config-check -y`  
   若此时还没有 env，classic 二进制会带着 `use_columnar=true` 起来然后立刻退出：`Columnar storage is not supported in current build`（systemd status 48，端口 9540 等 2min 超时）。这是预期失败，不是集群坏了。
4. **reload 之后立刻** 在该 CN 的 `scripts/run_tiflash.sh` 里 `export RUST_BACKTRACE=1` 后面加一行：
   ```bash
   export TIFLASH_COLUMNAR=true
   ```
   只加这一台。确认 `tiflash-5045` / `5060` / `5065` 的脚本里 **没有** 这行。
5. `tiup cluster start j4 -N 10.2.12.81:<tcp> -y`（若 reload 已把实例打进 crash loop，先 `stop -N` 再 `start -N`）。
6. 进程必须是 `.../tiflash-columnar/tiflash`。`use_columnar` 只在启动时生效。

WN 全程禁止该变量。patch / reload 若再重生脚本，必须重新加上。

### 混合窗对账结果

日志标记：`# ===== CN0 mixed-window restart start/end =====`（`logs/tpcc-tp.log`、`logs/ch-ap.log`）。

K=10 轮，同一套 snapshot SQL（含 `mig_side.keep2`），全部 PASS。不要等 `unconverted-l0-count`。第 1 轮 `order_line` COUNT=2523297，第 10 轮 2637978，同一 snapshot 内 tikv=tiflash。

CN0 宕窗里 AP 继续出 Q*（打在仍活着的 classic CN1 上）；起来后没有持续报错。`--ignore-error` 就是为这段准备的。混合窗里部分 Q 变慢（例如 Q15 ~3.5s）只记报告，不当 fail。

## 对账：注意点

这是 **双重物化后、切 CN 前** 的 WN 路径对账（两 CN 都 classic → 读 WN）。测试计划：`tidb_isolation_read_engines=tiflash` 与 `tikv` 必须一致。`columnar-levels` / `unconverted-l0-count` **不是**对账前置条件，也不是本阶段主门禁。

1. **必须同一 snapshot。** TP 一直在写，先打 tikv 再打 tiflash 而不钉 TSO，COUNT/SUM 一定会漂。不要拿「两次实时查询结果不同」当 fail。
2. **`@@tidb_current_ts` 在事务外是 0。** 要先 `BEGIN` 再取 TSO：
   ```sql
   BEGIN;
   SET @ts = @@tidb_current_ts;
   COMMIT;
   SET SESSION tidb_snapshot = @ts;
   ```
   `SELECT @@tidb_current_ts INTO @ts` 在这版 TiDB 会语法错误。`SET SESSION tidb_snapshot = @ts` 接受这个 TSO。
3. **连 ks1 `8041`，不要打 SYSTEM `8040`。** 覆盖全部当前 `tiflash_replica.count > 0` 的表：`tpcc.*` 12 张 + `smoke.t` + `mig_side.keep2`。不要把已 SET 0 的 `flip` 算进 `ready==total` 门禁。
4. 切 CN 前不要求 K=10。混合窗与切完两台 CN 后的观察期才是 **K=10**。一轮失败即应停下查，不要切下一台 CN。
5. 轮次之间绝对值会涨（例如 `order_line` 从导入时的 30 万涨到混合窗约 250 万），只要 **同一 snapshot 内** tikv 与 tiflash 相同即可。
6. mysql `-N -B` 会把结果里的 TAB 转义成字面 `\t`。不要用 `CHAR(9)` / 真 TAB 当分隔符再 `split('\t')`；用 `|` 或固定列更省事。
7. 切第一台 CN 之前仍要再确认一遍当前 `count>0` 表的 `columnar_status.ready==total`。**不要**等 `unconverted-l0-count` 收敛。SET 0 后看 `ready==0`，不要等 `total==0`。

`columnar_status`（TiKV **status** 端口 16540，不要打 gRPC 7540）：

```text
GET http://10.2.12.81:16540/kvengine/columnar_status?keyspace_id=1&table_id=<id>
```

| 表 | table_id |
|---|---|
| smoke.t | 17 |
| tpcc.warehouse | 23 |
| tpcc.district | 25 |
| tpcc.customer | 27 |
| tpcc.history | 29 |
| tpcc.new_order | 31 |
| tpcc.orders | 33 |
| tpcc.order_line | 35 |
| tpcc.stock | 37 |
| tpcc.item | 39 |
| tpcc.nation | 41 |
| tpcc.region | 43 |
| tpcc.supplier | 45 |
| mig_side.keep2 | 75 |
| mig_side.flip | 77（SET 0 后无 replica 行；`ready` 应为 0） |

`information_schema.tables.TIDB_TABLE_ID` 与 `information_schema.tiflash_replica.TABLE_ID` 一致。`KEYSPACE_ID=1`。切 CN 前 `ready==total` 的对象是 **当前 `count>0` 的表**（CH + `smoke.t` + `keep2`），不要把已 SET 0 的 `flip` 算进去。

## 对账：使用的语句

同一连接、同一 snapshot，先 `tikv` 再 `tiflash`：

```sql
BEGIN;
SET @ts = @@tidb_current_ts;
COMMIT;
SET SESSION tidb_snapshot = @ts;

SET SESSION tidb_isolation_read_engines = 'tikv';
-- 下面全部查询跑一遍

SET SESSION tidb_isolation_read_engines = 'tiflash';
-- 同样的查询再跑一遍

SET SESSION tidb_snapshot = '';
```

查询集合（结果按字符串比，两边必须完全相同）：

```sql
SELECT CONCAT_WS(',', COUNT(*), IFNULL(SUM(v),0)) FROM smoke.t;
SELECT CONCAT_WS(',', COUNT(*), IFNULL(SUM(v),0)) FROM mig_side.keep2;
SELECT COUNT(*) FROM tpcc.warehouse;
SELECT COUNT(*) FROM tpcc.district;
SELECT COUNT(*) FROM tpcc.item;
SELECT COUNT(*) FROM tpcc.nation;
SELECT COUNT(*) FROM tpcc.region;
SELECT COUNT(*) FROM tpcc.supplier;
SELECT COUNT(*) FROM tpcc.customer;
SELECT COUNT(*) FROM tpcc.stock;
SELECT COUNT(*) FROM tpcc.orders;
SELECT COUNT(*) FROM tpcc.new_order;
SELECT COUNT(*) FROM tpcc.order_line;
SELECT COUNT(*) FROM tpcc.history;
SELECT IFNULL(SUM(ol_amount),0) FROM tpcc.order_line;
SELECT IFNULL(SUM(ol_quantity),0) FROM tpcc.order_line;
SELECT IFNULL(SUM(c_balance),0) FROM tpcc.customer;
SELECT IFNULL(SUM(s_ytd),0) FROM tpcc.stock;
SELECT CONCAT_WS(',', COUNT(*), IFNULL(SUM(s_acctbal),0))
FROM tpcc.supplier JOIN tpcc.nation ON s_nationkey = n_nationkey;
SELECT IFNULL(SUM(ol_amount),0) FROM tpcc.order_line
WHERE ol_delivery_d IS NOT NULL AND ol_quantity BETWEEN 1 AND 10;
```

2026-09-09 实验室：WN 路径 3 轮 PASS。混合窗（CN0 columnar + CN1 classic）K=10 轮 PASS。第 1 轮混合窗 `order_line` COUNT=2523297、`SUM(ol_amount)=1055434134.93`；`keep2` 仍是 1000 / 5005000；`smoke.t` 仍是 100 / 49500。

## 执行时必须遵守（文档里有，这里只标容易踩的）

- **集群 `j4`，2 WN + 2 CN。不要动已有 `j1` / `j3`。** j3 已是 columnar CN。
- CH 打在用户 keyspace（`ks1`），不要打 SYSTEM。
- 起始：`--cn-mode disagg`，`cse.columnar-store-type=tiflash`，**不要**给任何节点设 `TIFLASH_COLUMNAR`。WN 全程禁止该变量。当前已是 `both` + `build-columnar=true`；**只有 CN0（5040）** 是 columnar。
- 打开双重物化：toml + **重启 TiKV 和 TiDB**，不用 `POST /build_columnar`。
- store-type 顺序：`tiflash` → `both` →（观察后、**删 PD rule 之前**）`columnar`。仍为 `both` 时删 PD group `tiflash` 规则会被 Replica Manager 修回来。
- 切 CN 前：相关表 `columnar_status.ready==total`。**不要**等 `unconverted-l0-count` 收敛；columnar 读会拉 TiKV snapshot（memtable + 未转换 L0 + `.col`）。
- 切 CN = `flash.use_columnar` + `TIFLASH_COLUMNAR` + **重启该 CN**。`reload` 会抹掉 `run_tiflash.sh` 里的 env，必须 **reload 之后再写 env 再 start**。`use_columnar` 只在启动生效。混合窗口 = 1 classic + 1 columnar，TiDB 按 region 分发后合并。
- **永远不要**对 CH 表 `SET TIFLASH REPLICA 0` 来抽 WN（会拆掉 schema-manager）。旁路库另做 replica 矩阵。
- `schema-refresh-threshold` 已从 CSE 删除（`#2791`）；只开 `schema-manager.enabled=true` 和短 `keyspace-refresh-interval`。
- Patch 必须是 **双二进制 wrapper** 包。不要用单次 cmake install 目录。流程见 skill `tiup-columnar-deploy` 的 `references/patch-sources.md`。
- S3 prefix：`dfs.prefix=/j4/tikv`，disagg 时 `storage.s3.root=/j4/tiflash`。部署前确认 bucket 上这两条路径空闲。j3 在**另一台** MinIO，prefix 不同，仍不要混用。
- 规模：1 warehouse 只验证流程；通过后再跑 1500，且 `forward` 与 `rollback_read_path` 都要跑。耗时只出报告，不当 fail gate。
- 端口 / deploy_dir / CN cache 不要照抄 j3（j3 CN cache 500GB 太大）。细节以测试计划「已确认环境参数」为准；脚本默认值已按该实验室填好。

生成 j4 起始拓扑：

```bash
python3 gen_tiflash_cluster_topo.py --cluster j4 \
    --cn-count 2 --wn-count 2 --cn-mode disagg \
    --dfs-prefix /j4/tikv --tiflash-s3-root /j4/tiflash \
    -o /tmp/j4-tiflash-write.yaml
```

## 建议下一跳（等用户明确说再动手）

1. 读本文件、[j4-lab.md](./j4-lab.md) 和测试计划，再读 `tiup-columnar-deploy`。
2. 旁路库 [epoch B](./online-migrate-tiflash-write-to-columnar-test.md#旁路表-replica-矩阵)：`keep2` 已是 replica 2；`flip` 再 SET 2 应同时出现 WN learner + `columnar_status.ready==total`，再 SET 0 看 `ready==0` 且 learner 消失。不要动 CH 表。
3. 切 CN1（5045）：同一套「edit-config → reload → **再写** `TIFLASH_COLUMNAR` → start」；进程变成 `tiflash-columnar`。WN 禁止该变量。
4. 两 CN 都是 columnar 后再 K=10 对账（观察期）。不要等 L0 转完。
5. 1 warehouse 走完整 `forward` 后再考虑 1500 和独立 `rollback_read_path`。不要在这条 CH 曲线中间插入回退。
6. TP/AP 仍在跑就不要无故杀掉。对账脚本若要长期保留，放到本仓库，不要只留在 tiflash-2 `docs/`。

## Suggested skills

下一 agent 应先 `Read` 这些 skill 再动手：

1. **`tiup-columnar-deploy`**（`/DATA/disk1/jaysonhuang/.claude/skills/tiup-columnar-deploy/SKILL.md`）— 部署、patch、start、grafana、PD/TiKV HTTP、`columnar_status`。必读 `references/topo-modes.md`、`patch-sources.md`、`http-and-sys-tables.md`、`local-defaults.md`。
2. **不要**再跑 **`grilling`**：设计已结算；除非用户明确要改决策。
3. 若用户要「按文档写操作 runbook / 脚本」：可用 **`proposal-to-impl`**，输入就是上述测试计划，输出应是 tiup 操作步骤而不是改 TiFlash 产品代码。
