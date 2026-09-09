# Handoff: 在线将 TiFlash disagg 迁到 tikv-worker columnar

**Date:** 2026-09-09  
**From session:** [online migrate plan](b82293f5-1288-42d3-8d18-d4abdf1e43c0)  
**Next session focus:** `j4` 已过 forward 阶段 2 门禁（`columnar_status` + WN 路径对账）。下一步是旁路库 epoch A，然后才切第一台 CN。不要重开设计。不要动 `j1` / `j3`。

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

## 当前进度（2026-09-09 17:00 左右）

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
- 四个 TiFlash 进程仍是 `.../binaries/tiflash/tiflash`（classic CN + WN）

**未做（下一 agent 的工作面）**

- 旁路库 epoch A（`mig_side` 一类；不要对 CH 表 `SET REPLICA 0`）
- 切第一台 CN（混合窗）以及之后的 `forward` 步骤
- 独立用例 `rollback_read_path`
- 1500 warehouse、Grafana import、把对账收成仓库脚本

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
3. **连 ks1 `8041`，不要打 SYSTEM `8040`。** 覆盖全部 `tiflash_replica.count > 0` 的表：`tpcc.*` 12 张 + `smoke.t`。
4. 本阶段不要求 K=10。K=10 是切完两台 CN 之后的观察期。这次实验室跑了 3 轮（轮间 sleep 5s）；一轮失败即应停下查，不要切 CN。
5. 轮次之间绝对值会涨（例如 `order_line` 从导入时的 30 万涨到对账时约 100 万），只要 **同一 snapshot 内** tikv 与 tiflash 相同即可。
6. mysql `-N -B` 会把结果里的 TAB 转义成字面 `\t`。不要用 `CHAR(9)` / 真 TAB 当分隔符再 `split('\t')`；用 `|` 或固定列更省事。
7. 切第一台 CN 之前仍要再确认一遍 `columnar_status.ready==total`。**不要**等 `unconverted-l0-count` 收敛。

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

`information_schema.tables.TIDB_TABLE_ID` 与 `information_schema.tiflash_replica.TABLE_ID` 一致。`KEYSPACE_ID=1`。

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

2026-09-09 实验室：3 轮均 PASS。第 1 轮快照下 `order_line` COUNT=997559、`SUM(ol_amount)=639557515.52`，tikv 与 tiflash 相同；`smoke.t` 仍是 100 / 49500。

## 执行时必须遵守（文档里有，这里只标容易踩的）

- **集群 `j4`，2 WN + 2 CN。不要动已有 `j1` / `j3`。** j3 已是 columnar CN。
- CH 打在用户 keyspace（`ks1`），不要打 SYSTEM。
- 起始：`--cn-mode disagg`，`cse.columnar-store-type=tiflash`，**不要**给任何节点设 `TIFLASH_COLUMNAR`。WN 全程禁止该变量。当前已是 `both` + `build-columnar=true`，CN 仍 classic。
- 打开双重物化：toml + **重启 TiKV 和 TiDB**，不用 `POST /build_columnar`。
- store-type 顺序：`tiflash` → `both` →（观察后、**删 PD rule 之前**）`columnar`。仍为 `both` 时删 PD group `tiflash` 规则会被 Replica Manager 修回来。
- 切第一台 CN 前：相关表 `columnar_status.ready==total`。**不要**等 `unconverted-l0-count` 收敛；columnar 读会拉 TiKV snapshot（memtable + 未转换 L0 + `.col`）。
- 切 CN = `flash.use_columnar` + `TIFLASH_COLUMNAR` + **重启该 CN**。`use_columnar` 只在启动生效。混合窗口 = 1 classic + 1 columnar，TiDB 按 region 分发后合并。
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
2. 旁路库进入 [epoch A](./online-migrate-tiflash-write-to-columnar-test.md#旁路表-replica-矩阵)。CH 表保持 `REPLICA 2`。
3. 切 CN0 前再确认一遍 `columnar_status`。混合窗用同一套 snapshot 对账语句；K 轮失败即停。不要等 L0 转完。
4. 1 warehouse 走完整 `forward` 后再考虑 1500 和独立 `rollback_read_path`。不要在这条 CH 曲线中间插入回退。
5. TP/AP 仍在跑就不要无故杀掉。对账脚本若要长期保留，放到本仓库，不要只留在 tiflash-2 `docs/`。

## Suggested skills

下一 agent 应先 `Read` 这些 skill 再动手：

1. **`tiup-columnar-deploy`**（`/DATA/disk1/jaysonhuang/.claude/skills/tiup-columnar-deploy/SKILL.md`）— 部署、patch、start、grafana、PD/TiKV HTTP、`columnar_status`。必读 `references/topo-modes.md`、`patch-sources.md`、`http-and-sys-tables.md`、`local-defaults.md`。
2. **不要**再跑 **`grilling`**：设计已结算；除非用户明确要改决策。
3. 若用户要「按文档写操作 runbook / 脚本」：可用 **`proposal-to-impl`**，输入就是上述测试计划，输出应是 tiup 操作步骤而不是改 TiFlash 产品代码。
