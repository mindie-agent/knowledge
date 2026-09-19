# Maintenance run summary — 2026-09-19 weekly topic maintenance

- **Timezone context**: Asia/Shanghai Saturday **10:00** routine window；wall clock UTC may differ
- **Routine**: 「每周专题维护」；**budget ceiling ≤30 minutes**（预算上限，非实测耗时）
- **First-ever weekly run**（routine previously never executed）
- **Constraints this run**: public sources / existing pins only；未改上游、未发评论；未装本地模型；未触 `_raw/`；**未删除**旧笔记；**未 recreate** 已有 case；corpus-nav **保持 HELD**；package pin `cc1a1d76fdee2c90b129f6ba56c171d500c158af`；verified curation-export → org feed `mindie-agent/knowledge` branch `knowledge/vllm-ascend` via locked publish（若变更）

## Goal this run

1. Maintain **≤3** formal **cross-cutting** topics（非 PR 镜像）+ cross-run weekly digest
2. Export → serial locked org publish if corpus changed
3. Quiet path only if truly nothing useful **and** tip unchanged

## Topics touched

| Path | Action | Bind |
|---|---|---|
| `topics/aclgraph-hostside-padding-cluster.md` (+ `.meta.json`) | **created** | #16157/#15645/#16798/#14409 pins from existing metas |
| `topics/dsa-dspark-triton-cp-gather-cluster.md` (+ `.meta.json`) | **created** | #16542/#16629/#16915/#16636 |
| `topics/a5-dcp-mla-sfa-cluster.md` (+ `.meta.json`) | **created** | #16468 OPEN draft DIRTY vs #16656 MERGED `aff1b74b…` |
| `held-topics/vaws-corpus-nav-va-npu-infra.md` | **kept HELD**（未 promote） | cannot bind single PR/revision scope as formal topic |
| `topics/vaws-corpus-nav-va-npu-infra.md` | **left in place**（不删旧知识）；README 链接改为 `held-topics/` | 无 `.meta.json`；非正式 formal topic |

未创建第 4 个 formal topic；周摘要仅写 `maintenance/`（本文件）。

## Cross-run digest（约 2026-09-13 → 2026-09-19）

| 日 | 高亮（来自既有 maintenance） |
|---|---|
| 09-13 | 初建 #16157 ACL Graph padding；VA 知识库起步 |
| 09-14 | #15645/#15816/#15832/#14409 等落地；个人 feed 路径时期 |
| 09-15–17 | #16542/#16468/#16629/#16636/#16731 等增量；#14409 head 连移 |
| 09-18 | #16798/#16656 新建；MindIE acceptance 重 pin #16656/#16468；org 发布切到 `mindie-agent/knowledge` `knowledge/vllm-ascend` |
| 09-19 daily | **new** #16904/#16915/#16853；**revised** #16656→**MERGED**、#16798/#16468/#16542；deferred #16545 MegaMoe / #16848 default MRV2；publish tip `caab5632…` |

**主题簇**：ACL Graph host/padding；DSA/DSpark Triton+CP gather；A5 DCP/MLA/SFA（MERGED vs draft）。

## Conflicts / unverifiable

- 全部交叉专题：`NPU_e2e_unverified`；作者手测 → CLAIM only
- #16468 仍 draft+DIRTY 且体量膨胀；**不可**与 #16656 MERGED 等同
- #16542 rebase 后 DIRTY；#15645 CLOSED unmerged
- 本周未对分案 pin 做新的 `gh` 重验（pins 复制自既有 `.meta.json`）
- README 曾错误把 corpus-nav 指到 `topics/` → 本轮改为 `held-topics/`

## Deferred

- #16545 MiniMax MegaMoe、#16848 default MRV2（daily 已 defer）
- 昨日及更早 defer 列表未在本周专题深挖
- 不新增第 4 formal topic；不 promote corpus-nav

## Artifacts this run

| Path | Role |
|---|---|
| `topics/aclgraph-hostside-padding-cluster.md` (+ meta) | new cross-cutting |
| `topics/dsa-dspark-triton-cp-gather-cluster.md` (+ meta) | new cross-cutting |
| `topics/a5-dcp-mla-sfa-cluster.md` (+ meta) | new cross-cutting |
| `README.md` | index + weekly pointers；corpus-nav → held-topics |
| `maintenance/2026-09-19-weekly-topic-maintenance.md` | this file |
| `meta/sources-2026-09-19-weekly.json` | local ops（**非 feed**） |
| `meta/feed-publish-2026-09-19-weekly.md` | local publish note（**非 feed**） |

## Publish outcome

- **Export**: generation `77952cdd9dba4fa2ab63b6a3ae15f5e3`；manifest `be3eed05812c6d1d8fcf65be183eda997e159dbba2acecf93c82578c18cc42c3`；snapshot `18800ddbb8843848b96ee976605670a5e318d3c6e0ef6884f8a7096c7a577e12`；changes added=4 updated=1；**verified**
- **Publish**: `org-feed-publish-locked.sh` exit 0；tip generation `b5e8f608…` → `77952cdd…`；org tip commit **`363bee7174774b96b2fd51b95f1c5a048bbf1d5f`** on `mindie-agent/knowledge` / `knowledge/vllm-ascend`（prior `caab5632a87516b19957be40172ceda20fbfd768`）
- Package pin confirmed `cc1a1d76fdee2c90b129f6ba56c171d500c158af`

## Retrieval queries

1. weekly topic maintenance 2026-09-19 Asia/Shanghai
2. aclgraph-hostside-padding-cluster 16157 16798
3. dsa-dspark-triton-cp-gather-cluster 16915 16542
4. a5-dcp-mla-sfa-cluster 16468 MERGED 16656
5. held-topics corpus-nav not formal
