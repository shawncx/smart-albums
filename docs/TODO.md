# 项目待办

当前合同见 [便携相册与图片向量设计](index-design.md)，依据 [便携相册实施计划](portable-album-plan.md)。计划原文不是实时进度；[早期实施计划](ingestion-index-management-plan.md) 的多相册、旧 CLI 和迁移设计已被新合同取代，保留为历史记录。以下区分已实现代码、待集中验证和未进行的真实验收，不发布私人路径、照片或报告数据。

## 当前三能力范围

| 能力 | 当前职责 | 边界 |
| --- | --- | --- |
| ingestion | 当前相册内增量导入、基础元数据、双原图路径、等比例 SQLite JPEG 缩略图 | 不调用模型、不自动 index、不删除原图、不创建内部相册 |
| index | 显式 setup/configure、图片 embedding 的 plan/execute/status/job/resume；保留历史配置与结果 | 不通过描述中转、不自动改默认、不做技术参数 |
| management | 文件 create/open/backup、照片浏览、文件名/路径查找、中英文语义搜索、original/relink、缩略图与扫描诊断 | 普通浏览/搜索真正只读；创建和路径维护需明确请求，不自动补索引或增加选片控件 |

## 已实现代码范围，集中验收仍待完成

- **一个相册一个 SQLite 文件。** 先明确选择已有文件或创建新文件；已选择则不重复询问，普通说明性问题无需选库。全局必填 `--database <绝对路径>`，支持 `.sqlite/.sqlite3/.db`；无默认库、`--state-dir`、旧别名或内部 album/library 选择。换文件清空旧 photo/profile/run 和待确认上下文。
- **新格式边界。** schema 8、`application_id=0x53414C42`，严格 11 张表：`album_metadata`、`photos`、`thumbnails`、`scans`、`scan_events` 和 `image_embedding_profiles/results/runs/items/claims/settings`。没有旧多相册、analysis/text-vector、`image_index_*` 或空 `technical_*` 表。旧 v1–v7 数据库原样拒绝，不自动迁移、清空或覆盖；用户真实旧库及备份保持不动。
- **生命周期。** create 仅排他创建明确的新路径；open 只读检查，无 DDL/自动修复。回显 UUID、文件名显示名和绝对数据库路径。backup 是新目标上的 SQLite 一致性快照，不覆盖文件；不包含原图、模型或 Python 环境。
- **双路径。** 绝对路径优先，缺失才尝试相对于当前数据库父目录的路径；可含 `..`，不同盘符/UNC share 存 `NULL` 并警告。绝对路径命中不自动重写相对路径。两处都缺失才是 source missing；权限/I/O 故障另报 unavailable，不能借故选另一份。
- **明确路径维护。** original 可写入定位修复，relink 必须 SHA-256 匹配才更新双路径；只读/锁定修复失败明确报告，不能假装成功。路径变化保留 ID、内容版本、预览和向量；只有 ingestion 更新内容版本。`original_status` 与 `ingest_state` 分离，定位/重连不能清除入库错误。
- **稳定导入身份。** 移动后已有绝对/相对路径仍能无歧义匹配才沿用 photo ID；绝对位置仍有效而相对副本指向别处时报冲突，不按 hash 自动去重。完整扫描仅检查本来源范围内缺失，其他目录保留；不完整扫描不推断 missing。
- **保留 index 邀请。** `index_prompt` 给出本次成功且尚未 ready 的确切 ID；用用户语言解释无索引可浏览/按文件名路径查找，有索引及兼容模型可按中英文画面语义搜候选，再询问是否准备索引。不自动下载、建计划或执行；确认邀请不等于批准未展示的执行摘要。
- **精确计划和恢复。** `index plan` 必须 `--all` 或 `--ids-file`，可选 profile/limit/dry-run；execute 需真实批准的 digest。resume 复用既有推理批准；原状态 `running` 时还须确认所有设备旧 worker 已停止并传 `--confirm-stopped`。该标志不抢占活跃 OS 锁，也不授予推理批准；纯缓存任务不加载模型、不扩展成推理。
- **共享本机模型缓存。** `Config.model_cache_root` 与相册文件分离；Windows 默认 `%LOCALAPPDATA%\SmartAlbums\models`，其他平台用应用/XDG 缓存。可选全局 `--model-cache-dir` 在 setup/index/semantic search 中一致。已知旧权重可显式指定缓存根复用，不自动搬移或删除。
- **图片向量的明确含义。** `image-embedding-profile-v1`，`embedding_kind: image_text_semantic`、`stored_modality: image`、`input_scope: stored_thumbnail`、`granularity: whole_image`。持久化 768 维整图语义图片向量；配套文字查询向量只在查询内使用，不落表。输出 `component: image_embedding` ready 不代表对焦/模糊等技术分析已完成。

初始模型仍固定为 `google/siglip2-base-patch16-224`，revision `75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2`，Transformers + PyTorch CPU FP32。官方 224×224 方形缩放仅用于推理，SQLite 预览保持比例，无自定义裁剪/填边。setup 才可显式下载，第一次也不设置默认；新 profile 语义字段会影响 ID，但缓存路径不影响。当前可选环境仍是标准 GIL CPython 3.14 x86-64 CPU，不承诺 ARM、其他 Python 或任意主机都能跑模型。

普通 browse/search 不 stat 原图、不写数据库、不顺带修复位置或建表。新 `album-snapshot-v1` 为只读快照，无 `search-add`/旧选片协议兼容。云端使用完整本地文件：下载 → 本地操作 → 全部停止/关闭 → 复制/同步；单用户单设备写入，本地锁不是分布式锁。模型与运行时由每台主机独立准备。

## 必须后续完成

- [ ] **集中回归验收。** 代码已实现，不等于全部测试已经通过。统一运行已有 unittest，检查严格 11 表/格式拒绝、不覆盖/只读无 DDL、任意位置与跨盘警告、移动重导 ID 稳定、路径冲突/只读修复错误、扫描范围、导出保护、索引摘要/缓存/锁恢复以及三能力 Skill 合同。测试数据用合成图/独立新文件，不触碰用户真实旧库。
- [ ] **新格式真实模型与断网验证，需单独授权。** 本轮没有执行新格式真实模型试跑；此前 v7 性能记录不是 schema 8 验收。代码批准不包含下载、真实照片处理或正式库修改授权。先确认兼容环境及显式缓存根，在单独新相册验证旧权重复用、固定清单下载/中断恢复、损坏文件和真正断网推理；普通计划/索引/搜索不得自动联网安装或回退云端。
- [ ] **真实模型质量与资源验收。** 先用非敏感测试图，再使用用户明确指定的代表性照片；人工标注中英文配对查询，覆盖主体/场景/色彩/构图/组合、精确条件和无匹配情况。报告 top-K、Recall@K、语言差异及失败样例；分别测模型加载、逐图编码、文字编码、排序和内存。不承诺未测的速度、节省量或检索质量。
- [ ] **新相册跨位置/跨设备备份恢复演练，需单独授权。** 使用独立新文件验证一致性备份、UUID/元数据/预览/向量、相对路径 fallback、跨盘 relink、原图离线浏览及 running 任务停机确认。停止操作后再移动/同步；不是旧库自动迁移或生产库演练，也不证明任意网络文件系统安全。
- [ ] **NaFlex 新 profile。** 明确输入分辨率/patch 或 token 预算、处理器和运行时身份；与当前 224 模型比较中英文检索质量、比例相关失败、内存和耗时。保留现有索引，不原地替换向量；仅在明确请求后切换默认。
- [ ] **技术参数：index 内独立组件，不新增第四能力。** 定义字段、单位、适用输入范围（缩略图/原图/裁剪）、输入哈希、算法/模型来源及版本。独立 technical 配置、结果、状态、任务、恢复、失效规则和授权；不借用 `image_embedding_*` 表，不预建空表/空结果。更新技术算法不重算 embedding，缺失技术结果是未知；缩略图模糊不等于原图对焦失败，也不能把主观描述当测量值。
- [ ] **ONNX / 量化独立评估。** 验证目标平台运行时和依赖兼容性，测量内存/延迟，检查向量数值与归一化、排序变化和真实检索质量。使用新 profile/runtime 身份；不与 FP32 结果原地混用，不因同维度跨配置补齐。

## 暂缓，不扩大首版

- [ ] 跨相册搜索、合并、跨文件共享照片/向量、自动云同步或分布式并发写入。
- [ ] 基于选择的选片/组织工作流：需新的明确范围、权限和快照合同，当前只读 HTML 不增加写控件。
- [ ] 聚类、自动发现系列、以图搜图、重复检测/删除、自动选片及独立语音识别。
- [ ] 原图强制全量哈希/变化监控：现有 ingestion 基于 size/mtime 的快速检查可能漏掉保留属性的外部修改；不把读取保存预览描述为实时核验原图。
