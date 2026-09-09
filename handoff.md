# Handoff: 在线将 TiFlash disagg 迁到 tikv-worker columnar

**Date:** 2026-09-09  
**From session:** [online migrate plan](b82293f5-1288-42d3-8d18-d4abdf1e43c0)  
**Next session focus:** 接手已结算的实验室测试：不要重开设计讨论；用户确认后按测试计划部署 `j4`、跑 `forward` / `rollback_read_path`。

## Goal

验证 next-gen **tiflash-write + classic CN** 可以在持续 CH-benCHmark（TP+AP）下在线迁到 **tikv-worker columnar**：CN 读 S3 上的 `.col`，Write Node 全部下线。这不是 classic→disagg 升级。

`ENABLE_NEXT_GEN` / `ENABLE_NEXT_GEN_COLUMNAR` 是编译开关。运行时靠 `flash.disaggregated_mode`、`flash.use_columnar`、`TIFLASH_COLUMNAR`、TiDB `cse.columnar-store-type`、TiKV `kvengine.build-columnar`。

## Canonical artifacts（不要把正文抄进回复）

| 内容 | 位置 |
|---|---|
| 本交接文档 | [handoff.md](./handoff.md) |
| 测试计划（阶段、门禁、环境、明确不做） | [online-migrate-tiflash-write-to-columnar-test.md](./online-migrate-tiflash-write-to-columnar-test.md) |
| 拓扑生成脚本 | [gen_tiflash_cluster_topo.py](./gen_tiflash_cluster_topo.py) |
| GitHub repo | https://github.com/JaySon-Huang/online-migrate-tiflash-disagg-to-columnar |
| 本地 clone | `/DATA/disk1/jaysonhuang/online-migrate-tiflash-disagg-to-columnar` |
| tiflash-2 工作副本（文件名带日期，改 CLI 时两边一起改） | `/DATA/disk1/jaysonhuang/tiflash-2/docs/design/2026-09-09-online-migrate-tiflash-write-to-columnar-test.md`、`.../gen_tiflash_cluster_topo.py` |

两份脚本/文档应视为同源。本仓库示例命令用仓库根路径；tiflash-2 里脚本 `__doc__` 仍写 `docs/design/gen_tiflash_cluster_topo.py`。

## 当前进度

**已完成**

- 设计 grilling 已收口；测试计划 Status=Draft，尚未落地执行。
- 生成脚本可用：`--cluster`、`--dfs-prefix` 必填；`--tiflash-s3-root` 仅 `--cn-mode=disagg` 必填，`--cn-mode=columnar` 默认空。
- 已建 public repo `JaySon-Huang/online-migrate-tiflash-disagg-to-columnar`。
- 用户明确：**未授权则不要** `tiup cluster check` / `deploy` / `start`。

**未做（下一 agent 的工作面）**

- 未生成最终 yaml、未 deploy、未 patch、未 start。
- 无 CH-benCHmark 驱动脚本、对账脚本、Grafana import。
- 未跑 1 warehouse 流程，更未跑 1500 warehouse。
- 混合 CN 窗口正确性是产品要求，实验室尚未实证。

## 执行时必须遵守（文档里有，这里只标容易踩的）

- **集群 `j4`，2 WN + 2 CN。不要动已有 `j1` / `j3`。** j3 已是 columnar CN。
- CH 打在用户 keyspace（`ks1`），不要打 SYSTEM。
- 起始：`--cn-mode disagg`，`cse.columnar-store-type=tiflash`，**不要**给任何节点设 `TIFLASH_COLUMNAR`。WN 全程禁止该变量。
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

## 建议下一跳（等用户说「部署 / 开跑」）

1. 读测试计划全文，再读 `tiup-columnar-deploy`。
2. 生成 yaml → `tiup cluster check` → deploy 官方壳 `v8.5.6` → offline patch → **确认 CN 未设 `TIFLASH_COLUMNAR`** → start。
3. 1 warehouse 走完整 `forward` 流程（含混合 CN 对账）后再考虑 1500 和独立 `rollback_read_path`。
4. 操作脚本若要长期保留，放到本仓库，不要只留在 tiflash-2 `docs/`。

## Suggested skills

下一 agent 应先 `Read` 这些 skill 再动手：

1. **`tiup-columnar-deploy`**（`/DATA/disk1/jaysonhuang/.claude/skills/tiup-columnar-deploy/SKILL.md`）— 部署、patch、start、Grafana、PD/TiKV HTTP、`columnar_status`。必读 `references/topo-modes.md`、`patch-sources.md`、`http-and-sys-tables.md`、`local-defaults.md`。
2. **不要**再跑 **`grilling`**：设计已结算；除非用户明确要改决策。
3. 若用户要「按文档写操作 runbook / 脚本」：可用 **`proposal-to-impl`**，输入就是上述测试计划，输出应是 tiup 操作步骤而不是改 TiFlash 产品代码。
