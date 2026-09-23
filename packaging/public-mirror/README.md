# public-mirror 多 profile 说明

本目录是私仓 → 公开仓确定性镜像的策略与清单真源。`scripts/build_public_mirror.py --profile` 选择产物：

| profile | 目标仓（policy 内 `public_repository`） | 内容 | 状态 |
|---|---|---|---|
| `assembled`（默认） | `hty8870/biodata-agent` | 装配体 = 现行公开仓全部内容 | **现行使用中**（唯一现行公开仓） |
| `skeleton` | `hty8870/knowledge-agent-skeleton` | 通用知识库 agent 骨架（不含领域包/语料/评测/教程/LTR 制品） | **计划中，仓未创建；仓名为待确认占位** |
| `biodata-pack` | `hty8870/biodata-domain-pack` | biodata 领域包（词表/维度/提示词/语料/评测集/教程/LTR 制品） | **计划中，仓未创建；仓名为待确认占位** |

- `skeleton ∪ biodata-pack == assembled`（交集仅 `.gitignore`，由 `tests/test_public_mirror_profiles.py` 机械钉死）。
- 装配 = 同时拉取 skeleton + biodata-pack，领域包目录经 `BIODATA_DOMAIN_DIR` 挂载（或并入源码树 `domains/`）。
- 真实建仓与拆分执行是发布决策，未经明确授权不建仓；在那之前两个非默认 profile 仅用于本地验证。
