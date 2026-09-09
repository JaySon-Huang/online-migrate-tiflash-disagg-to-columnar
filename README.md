# Online migrate TiFlash disagg to columnar

在持续 CH-benCHmark（TP + AP）下，把 next-gen **tiflash-write + tiflash-compute** 集群在线迁到 **tikv-worker columnar**（Write Node 全部下线，Compute Node 读 S3 上的 `.col`）。

测试计划与拓扑脚本在本仓库；一次具体部署的端口、二进制和导入命令记在 [j4-lab.md](./j4-lab.md)。

## 文档

- [j4-lab.md](./j4-lab.md)：j4 部署结果 + 1 warehouse CH-benCHmark 导入步骤
- [handoff.md](./handoff.md)：给下一 agent 的交接（进度、约束、建议 skill）
- [online-migrate-tiflash-write-to-columnar-test.md](./online-migrate-tiflash-write-to-columnar-test.md)：测试计划（阶段、门禁、环境参数）
- [gen_tiflash_cluster_topo.py](./gen_tiflash_cluster_topo.py)：生成 tiup topology YAML

## 生成拓扑

部署前先确认目标 S3/MinIO bucket 上的 prefix **不会和已有集群冲突**。同一 endpoint/bucket 上重叠的 `dfs.prefix` 或 `storage.s3.root` 会混写或覆盖对象。

必填：`--cluster`、`--dfs-prefix`。`--tiflash-s3-root` 在 `--cn-mode=disagg` 时必填；`--cn-mode=columnar` 时可省略（默认空）。

起始态（2 CN + 2 WN，classic next-gen CN）：

```bash
python3 gen_tiflash_cluster_topo.py --cluster j4 \
    --cn-count 2 --wn-count 2 --cn-mode disagg \
    --dfs-prefix /j4/tikv --tiflash-s3-root /j4/tiflash \
    -o /tmp/j4-tiflash-write.yaml
```

最终态示例（columnar CN、无 Write Node）：

```bash
python3 gen_tiflash_cluster_topo.py --cluster j4 \
    --cn-count 2 --wn-count 0 --cn-mode columnar \
    --dfs-prefix /j4/tikv -o /tmp/j4-columnar.yaml
```

```bash
tiup cluster check /tmp/j4-tiflash-write.yaml -y
tiup cluster deploy j4 v8.5.6 /tmp/j4-tiflash-write.yaml --ignore-config-check -y
```

脚本默认 host / 端口 / S3 参数对应当前实验室机器，生成前用 `--host`、`--s3-*` 等覆盖。完整参数见 `python3 gen_tiflash_cluster_topo.py -h`。

## 迁移要点

1. 部署：`cse.columnar-store-type=tiflash`，CN 不设 `TIFLASH_COLUMNAR`
2. 双重物化：改 toml 为 `both` + `kvengine.build-columnar=true`，重启 TiKV / TiDB
3. 切 CN 读路径：`flash.use_columnar` + `TIFLASH_COLUMNAR=true` + 重启该 CN（**不要**给 WN 设该变量）
4. 切第一台 CN 之前：相关表 `columnar_status.ready==total`（不是等 L0 全部转成 `.col`）
5. 删 PD `tiflash` placement-rule 之前先改 `columnar-store-type=columnar`
6. 独立用例 `rollback_read_path`：只把 CN 切回 classic，不停双重物化、不删 rule、不缩容 WN
