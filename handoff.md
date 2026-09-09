# Handoff: 在线将 TiFlash disagg 迁到 tikv-worker columnar

**Date:** 2026-09-09  
**From session:** [online migrate plan](b82293f5-1288-42d3-8d18-d4abdf1e43c0)  
**Next session focus:** 1 warehouse `forward` **已走完**（含 epoch C）。两 CN columnar（store **430** / **431**），WN 已 prune，PD `tiflash` rule=0。旁路库 `mig_side.keep2` 仍 replica 2；`flip` 已 SET 0。**TP/AP 已于 2026-09-09 22:34 停掉。** 下一步才是 1500 warehouse / Grafana / 把对账收成脚本。不要重开设计。不要动 `j1` / `j3`。不要对 CH 表 `SET REPLICA 0`。reload / scale-in / prune 的 generate 会重生全部 `run_tiflash.sh`，CN 的 `TIFLASH_COLUMNAR` 要当场补回。

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

## 当前进度（2026-09-09 22:15）

**已完成**

- 设计 grilling 已收口；测试计划 Status=Draft。
- **`j4` 已 deploy / offline patch / start**，起始态 2 classic CN + 2 WN。细节：[j4-lab.md](./j4-lab.md)。
- 1 warehouse CH 已灌入 ks1 `tpcc`（12 张基表 replica 2，`AVAILABLE=1`，已 ANALYZE）。`smoke.t` 仍在，table_id 17。
- 持续负载已拆成 **两个 tiup bench 进程**（ks1 `8041`，库 `tpcc`，`--time 24h` 只是上限）：
  - TP：`tiup bench tpcc run -T 4 --ignore-error --interval 10s` → [`logs/tpcc-tp.log`](logs/tpcc-tp.log)
  - AP：`tiup bench ch run -T 0 -t 1`，`tidb_isolation_read_engines=tiflash` → [`logs/ch-ap.log`](logs/ch-ap.log)
  - `-T 0` 才是 AP-only（Q1–Q22）。**不要**给 TP 进程加 tiflash isolation。`--ignore-error` 是为了扛双重物化重启。
  - `logs/*.log` 已 gitignore，不要把持续输出提交进去。
  - **已于 2026-09-09 22:34 SIGTERM 停掉**（`tiup-bench` 637228 / 637251 均 `Got signal terminated`）。不要无故再拉起来。
- 双重物化已打开并 reload **TiDB + TiKV**（未 reload TiFlash / worker，未设 `TIFLASH_COLUMNAR`）：
  - TiDB 当时 `cse.columnar-store-type=both`（8040 与 8041）；**现已改为 `columnar`**
  - TiKV `kvengine.build-columnar=true`（必须重启，`online_config(skip)`）
- **forward 阶段 2 门禁已过**（切 CN 之前）：
  - ks1 `keyspace_id=1` 上 13 张 `tiflash_replica.count>0` 的表全部 `columnar_status.ready==total==1`
  - S3 `jayson-columnar-test/j4/tikv` 已有 `.col`；部分 region（如 364）`columnar-levels` 非空。这是辅助证据，不是主门禁
  - WN 路径对账：同一 `tidb_snapshot` 下 tikv vs tiflash，连续 3 轮 19 条聚合全部一致（见下节）
- **旁路库 epoch A 已过**（`mig_side`，ks1 `8041`）。CH 表全程未 `SET REPLICA 0`。见下节。
- **CN0 已切到 columnar，混合窗 K=10 对账已过**（见下节）。
- **旁路库 epoch B 已过**（混合窗下 `mig_side.flip` SET 2/0 仍同时建/拆 WN learner + columnar）。CH 表未动。
- **CN1 已切到 columnar**，两 CN 当时都是 `tiflash-columnar`，观察期 K=10 对账已过。
- store-type 已是 **`columnar`**。TiDB `columnar` 之后观察 TP+AP **10 min** 已过。
- **`rollback_read_path` 已过**（2026-09-09 21:05–21:12）：两 CN 切回 classic，K=10 对账走 WN 全部 PASS。
- **回滚后再滚回 columnar 已过**（2026-09-09 21:16–21:21）：`reload -N` + 注入 `TIFLASH_COLUMNAR`，不必清 data。K=10 对账 PASS。
  - CN0 `5040` store **430** → `.../tiflash-columnar/tiflash`，`use_columnar=true`，`TIFLASH_COLUMNAR=true`
  - CN1 `5045` store **431** → 同上
  - 回滚时的 classic store 428/429 已被 tombstone
- **`forward` 下线 WN 已过**（2026-09-09 21:26–21:30）：见下节。PD group `tiflash` 0 条 rule；WN store 292/293 已从 PD 消失；拓扑里只剩两台 columnar CN。
- **旁路库 epoch C 已过**（2026-09-09 22:14）：`store-type=columnar` 且 WN 已缩容后，`flip` SET 2 **只出 columnar、不出 WN learner**；SET 0 只停该表 columnar。CH 表未动。

**未做（下一 agent 的工作面）**

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
   **永远**加 `-R <component>` 或 `-N host:port`，禁止无限定的整集群 reload。即便 `-R tidb` 也会重生所有 TiFlash `run_tiflash.sh`。  
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

## 旁路库 epoch B（2026-09-09，混合窗）

集群状态：`both`，CN0 columnar + CN1 classic。对象仍是 **`mig_side`**，不要对 `tpcc` / `smoke` 做 SET 0/1。

| 表 | table_id | 操作 | 结束态 |
|---|---|---|---|
| `mig_side.keep2` | 75 | 对照，保持 replica 2 | region 410 仍在 WN 292+293；`ready=1,total=1`；tikv/tiflash 1000 / 5005000 |
| `mig_side.flip` | 77 | SET 2 再 SET 0 | SET 2 约 6s：AVAILABLE=1，region **423**（不再是 epoch A 的 415）两边 learner，`ready=1`；SET 0：replica 行与 learner 立刻没了，约 3s 后 `ready=0`（`total` 仍为 1）；行存 COUNT=1000 |

SET 2 时 WN region 15/15；SET 0 后回到 14/14。CH + `smoke.t` 仍是 13 张 `REPLICA 2 AVAILABLE=1`。

结论：混合窗下 store-type 还是 `both`，新建 replica **仍然**会同时出 WN learner 和 columnar。SET 0 两边都消失。判定仍是 `ready==0`，不要等 `total==0`。

epoch C 已在 WN 缩容后做过：SET 2 **没有**再出 WN learner。

## CN1 与全 columnar 观察期（2026-09-09）

CN1 = **`10.2.12.81:5045`**。切之前再确认当前 `count>0` 表 `ready==total`（含 `keep2`，不含 `flip`）。

### 切法改进（相对 CN0）

`tiup cluster reload -N 10.2.12.81:5045` 仍会 **重生所有 TiFlash 的 `run_tiflash.sh`**（包括已经在跑的 CN0）。所以：

1. `show-config` 后 **只改 5045 那一段** 的 `flash.use_columnar: false` → `true`（不要 `yaml.dump` 整份）。
2. `edit-config --topology-file`。
3. reload 的 generate 阶段一开始，就轮询 5040/5045 的 `run_tiflash.sh`；一旦没有 `TIFLASH_COLUMNAR` 立刻写回去。WN 脚本禁止写入。
4. 这次在 Restart 之前补上了 env，CN1 **一次 reload 成功**，进程直接是 `.../tiflash-columnar/tiflash`。没有再走 CN0 那种 2min 超时 + crash loop。
5. reload 结束后仍要确认：5040 和 5045 脚本都有 env；5060/5065 没有。

日志标记：`# ===== CN1 restart window start/end =====`。

### 观察期对账

两 CN 都是 columnar 后 K=10 轮同一套 snapshot SQL，全部 PASS。不要等 `unconverted-l0-count`。第 1 轮 `order_line` COUNT=10944855、`SUM(ol_amount)=3350563943.81`；第 10 轮 COUNT=11057523。`keep2` 仍是 1000 / 5005000。AP 无持续报错。

## TiDB `columnar` + 10min 观察（2026-09-09）

已执行：`cse.columnar-store-type=columnar`（8040 / 8041），`tiup cluster reload j4 -R tidb --ignore-config-check`。日志标记：`# ===== store-type=columnar restart start/end =====`、`# ===== store-type=columnar observe 10min start/end =====`。

**故意没做：** 没有删 PD group `tiflash` rules，没有 scale-in WN。留给 `rollback_read_path` 用。

`-R tidb` 的副作用：generate 会重生 **全部** TiFlash `run_tiflash.sh`，CN 上的 `TIFLASH_COLUMNAR` 被抹掉。当时进程仍是内存里的 columnar；脚本已重新注入 5040/5045，WN 5060/5065 保持干净。

10 min 负载（20:16–20:26，每 2 min 采样 6 次）：

| t | NEW_ORDER TPM | Avg(ms) | AP Q* | WN 292 / 293 |
|---|---|---|---|---|
| 0s | 3639 | 10.6 | 22 | 14 / 14 |
| 2min | 4571 | 11.0 | 22 | 14 / 14 |
| 4min | 2606 | 10.9 | 22 | 14 / 14 |
| 6min | 3637 | 10.6 | 22 | 14 / 14 |
| 8min | 2294 | 10.2 | 22 | 14 / 14 |
| 10min | 2932 | 10.4 | 22 | 14 / 14 |

TP/AP 全程在跑，日志尾部无 Error/FAIL。TPM 在 2.3k–4.6k 波动，不是停跑。

### 当前集群快照（观察结束时，仍有效）

| 项 | 值 |
|---|---|
| 集群 | **只动 `j4`**。不要动 `j1` / `j3` |
| TiDB | `cse.columnar-store-type=columnar`（SYSTEM 8040 / ks1 **8041**） |
| TiKV | `kvengine.build-columnar=true`，grpc 7540，status **16540** |
| CN0 | `10.2.12.81:5040` store **430**，`tiflash-columnar`，`use_columnar=true`，`TIFLASH_COLUMNAR=true` |
| CN1 | `10.2.12.81:5045` store **431**，同上 |
| WN | **已缩容并 prune**。原 `5060`/`5065` store 292/293 已从 PD 消失（`ErrStoreNotFound`），deploy 目录已删 |
| PD | client **6540**；group `tiflash` **0 条 rule** |
| 负载 | **已停**（2026-09-09 22:34）。日志仍在 `logs/tpcc-tp.log`、`logs/ch-ap.log`（ks1 `8041` / `tpcc`），gitignored |
| 旁路 | `mig_side.keep2` id 75 replica 2；`flip` id 77 无 replica 行 |

已删的 PD `tiflash` rule id（当时）：`keyspace-1-table-{17,23,25,27,29,31,33,35,37,39,41,43,45,75}-r`（`smoke.t` + CH 12 表 + `keep2`）。CH 表未 `SET REPLICA 0`，`tiflash_replica.count` 仍是 2 / AVAILABLE=1。

### `rollback_read_path`（2026-09-09，已完成）

CN 从 columnar 切回 classic **不能**只改 `use_columnar=false` 再 reload：columnar 启动时会把旧 disagg store_id tombstone，再以新 store_id 注册。本地还留着旧 ident 时，PD 会报 `StoreTombstone`，进程起不来（CN0 第一次就是这样，store 295）。

正确顺序（每台 CN，`-N`）：

1. `edit-config` 只把该 CN 的 `flash.use_columnar: false`（不要 `yaml.dump` 整份）
2. `tiup cluster stop j4 -N 10.2.12.81:<tcp> -y`
3. **删掉该 CN 的 data 目录**（都是缓存）：`rm -rf /DATA/disk3/jaysonhuang/clusters/tiflash-<tcp>/data && mkdir -p ...`
4. 落地 conf：`use_columnar = false`，去掉 `export TIFLASH_COLUMNAR=true`
5. `tiup cluster start j4 -N 10.2.12.81:<tcp> -y`
6. 进程必须是 `.../binaries/tiflash/tiflash`；PD 上该地址出现 **新 store_id 且 Up**

实验室结果：

| CN | 旧 disagg | 旧 columnar | 回滚后 classic | 二进制 |
|---|---|---|---|---|
| 5040 | 295 Tombstone | 420 Tombstone | **428 Up** | classic |
| 5045 | 294 Tombstone | 427 Tombstone | **429 Up** | classic |

K=10 对账（同一 snapshot，tikv vs tiflash，读 WN）全部 PASS。WN 292/293 始终各 14 region。第 1 轮 `order_line` COUNT=13216442、`SUM(ol_amount)=3969628729.87`；第 10 轮 COUNT=13258806。`keep2` 仍是 1000 / 5005000。store-type 仍是 `columnar`，rule 未删，WN 未缩容。

### 回滚后再滚回 columnar（2026-09-09，已完成）

classic → columnar **不必**清 data：产品会 tombstone 旧 classic store，再注册新 id。每台：`use_columnar: true`，`reload -N`，generate 期间给 **正在切 / 已经是 columnar 的 CN** 注入 `TIFLASH_COLUMNAR`，WN 禁止。

| CN | 回滚 classic | 再切 columnar | 二进制 |
|---|---|---|---|
| 5040 | 428 Tombstone | **430 Up** | `tiflash-columnar` |
| 5045 | 429 Tombstone | **431 Up** | `tiflash-columnar` |

K=10 对账 PASS。第 1 轮 `order_line` COUNT=13525410、`SUM(ol_amount)=4053983529.55`；第 10 轮 COUNT=13593690。当时 WN 仍各 14 region，14 条 `tiflash` rule 仍在。

### 下线 WN（2026-09-09 21:26–21:30，已完成）

前置：TiDB 已是 `columnar`，否则 Replica Manager 会把 rule 修回来。CH / `smoke` **没有** `SET REPLICA 0`。

1. `GET /pd/api/v1/config/placement-rule/tiflash`，按 id `DELETE /pd/api/v1/config/rule/tiflash/{rule_id}`。14 条全部 200，随后 group 规则数为 0。
2. `GET /pd/api/v1/regions/store/292` 和 `/293` **立刻**长度为 0（约 0s），rule 没有被补回。
3. `tiup cluster scale-in j4 -N 10.2.12.81:5060,10.2.12.81:5065 -y`：拓扑标 Tombstone，但 systemd 当时还在跑。generate 会重生 CN `run_tiflash.sh`，必须当场补 `TIFLASH_COLUMNAR`。
4. PD store 292/293 已是 Tombstone、region 0 后：`tiup cluster prune j4 -y`。Destroy success；PD 再查 292/293 是 `ErrStoreNotFound`；`5060`/`5065` 目录删除。prune 同样会 generate CN 脚本，再补一次 env。

结束后 display 只剩 CN `5040`/`5045`（compute, Up）。进程是 `tiflash-columnar`。当时 TP/AP 仍在跑，现已停。

## 旁路库 epoch C（2026-09-09 22:14，已完成）

集群状态：`cse.columnar-store-type=columnar`，两 CN columnar（430/431），**无 WN**，PD group `tiflash` 0 条 rule。对象仍是 **`mig_side`**，不要对 `tpcc` / `smoke` 做 SET 0/1。

WN 抽空后 `keep2`/`flip` 的 region 已合并进 **404**（start `t_40_`，peers 只剩 TiKV store 1 voter）。epoch A/B 的 410/423 不再单独存在。看 learner 仍用 PD `region/id`，不要看 `SHOW TABLE REGIONS` 的 PEERS。

| 表 | table_id | 操作 | 结束态 |
|---|---|---|---|
| `mig_side.keep2` | 75 | 对照，保持 replica 2 | replica 2 / AVAILABLE=1；`ready=1,total=1`；region 404 **无** learner；同一 snapshot tikv/tiflash `1000 / 5005000` |
| `mig_side.flip` | 77 | SET 2 再 SET 0 | SET 2 约 6s：AVAILABLE=1，`ready=1`，region 404 `columnar-tables` 4→5，**无** write store、**无** `tiflash` rule、**无** learner；tikv/tiflash `1000 / 5005000`。SET 0：replica 行立刻消失，约 2s `ready=0`（`total` 仍为 1），`columnar-tables` 回到 4；行存 COUNT=1000 |

SET 2 时 replica 行 14→15；SET 0 后回到 14。CH + `smoke.t` 仍是 13 张 `REPLICA 2 AVAILABLE=1`。PD 活 store 始终只有 TiKV 1 + CN 430/431。

结论：`store-type=columnar` 且 WN 已缩容后，新建 replica **只建 columnar**，不会再写 PD `tiflash` rule，也不会出 learner。SET 0 只停该表 columnar。判定仍是 `ready==0`，不要等 `total==0`。

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

2026-09-09 实验室：WN 路径 3 轮 PASS。混合窗（CN0 columnar + CN1 classic）K=10 PASS。全 columnar CN 观察期 K=10 PASS。第 1 轮观察期 `order_line` COUNT=10944855、`SUM(ol_amount)=3350563943.81`；`keep2` 仍是 1000 / 5005000；`smoke.t` 仍是 100 / 49500。

## 执行时必须遵守（文档里有，这里只标容易踩的）

- **集群 `j4`，现在是 2 台 columnar CN、无 WN。不要动已有 `j1` / `j3`。** j3 已是 columnar CN。
- CH 打在用户 keyspace（`ks1`），不要打 SYSTEM。
- 起始曾是 `--cn-mode disagg` + 2 WN。当前已是 **`columnar`** + `build-columnar=true`；**两台 CN 都是 columnar**，WN 已 prune。
- 打开双重物化：toml + **重启 TiKV 和 TiDB**，不用 `POST /build_columnar`。
- store-type 顺序：`tiflash` → `both` →（观察后、**删 PD rule 之前**）`columnar`。仍为 `both` 时删 PD group `tiflash` 规则会被 Replica Manager 修回来。rule / WN 现已删完。
- `tiup cluster reload` **必须** `-R` 或 `-N`。无限定 reload 会把 CN env 一并冲掉。`scale-in` / `prune` 的 generate 同样会重生全部 TiFlash `run_tiflash.sh`。
- 切 CN 前：相关表 `columnar_status.ready==total`。**不要**等 `unconverted-l0-count` 收敛；columnar 读会拉 TiKV snapshot（memtable + 未转换 L0 + `.col`）。
- 切 CN = `flash.use_columnar` + `TIFLASH_COLUMNAR` + **重启该 CN**。`reload -N` 一台也会重生 **所有** TiFlash 的 `run_tiflash.sh`，CN0/CN1 的 env 都要在 generate 之后立刻补回。`use_columnar` 只在启动生效。
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
2. 1 warehouse `forward` + `rollback_read_path` + epoch A/B/C 已走完。若要放大，再考虑 1500 warehouse。
3. Grafana import、把对账收成仓库脚本。
4. **不要**把 TP/AP 再拉起来，除非用户明确要求。不要对 `tpcc` / `smoke` 做 SET 0/1。

## Suggested skills

下一 agent 应先 `Read` 这些 skill 再动手：

1. **`tiup-columnar-deploy`**（`/DATA/disk1/jaysonhuang/.claude/skills/tiup-columnar-deploy/SKILL.md`）— 部署、patch、start、grafana、PD/TiKV HTTP、`columnar_status`。必读 `references/topo-modes.md`、`patch-sources.md`、`http-and-sys-tables.md`、`local-defaults.md`。
2. **不要**再跑 **`grilling`**：设计已结算；除非用户明确要改决策。
3. 若用户要「按文档写操作 runbook / 脚本」：可用 **`proposal-to-impl`**，输入就是上述测试计划，输出应是 tiup 操作步骤而不是改 TiFlash 产品代码。
