# 项目待办

当前合同见 [便携相册、静态文件夹与图片向量设计](index-design.md)。[便携相册实施计划](portable-album-plan.md) 保留 schema 8 历史里程碑，不是 schema 9 文件夹功能的实时进度；[早期实施计划](ingestion-index-management-plan.md) 的多相册、旧 CLI 和迁移设计亦已被取代。以下区分已实现代码、待集中验证和未进行的真实验收，不发布私人路径、照片或报告数据。

## 当前三能力范围

| 能力 | 当前职责 | 边界 |
| --- | --- | --- |
| ingestion | 当前相册内增量导入、基础元数据、双原图路径、等比例 SQLite JPEG 缩略图 | 不调用模型、不自动 index、不删除原图、不创建内部相册 |
| index | 显式 setup/configure、图片 embedding 的 plan/execute/status/job/resume；保留历史配置与结果 | 不通过描述中转、不自动改默认、不做技术参数 |
| management | 文件 create/open/backup、手动静态文件夹、范围浏览/搜索、一次性日期整理、original/relink、缩略图与扫描诊断 | 普通浏览/搜索真正只读；文件夹/路径写入需明确请求，不自动补索引、归类或增加网页写控件 |

## 已实现代码范围

- **一个相册一个 SQLite 文件。** 先明确选择已有文件或创建新文件；已选择则不重复询问，普通说明性问题无需选库。全局必填 `--database <绝对路径>`，支持 `.sqlite/.sqlite3/.db`；无默认库、`--state-dir`、旧别名或内部 album/library 选择。换文件清空旧 photo/profile/run/folder 和待确认上下文。
- **新格式边界。** schema 9、`application_id=0x53414C42`，严格 13 张表：`album_metadata`、`photos`、`thumbnails`、`scans`、`scan_events`、`image_embedding_profiles/results/runs/items/claims/settings`、`virtual_folders(folder_id, name, name_key, description, created_at, updated_at)` 和 `virtual_folder_photos(folder_id, photo_id, added_at)`。没有旧多相册、analysis/text-vector、`image_index_*` 或空 `technical_*` 表。旧 v1–v8 数据库原样拒绝，不迁移、清空或覆盖；用户真实旧库及备份保持不动。
- **手动文件夹是基础功能。** `management folders` 支持自定义空文件夹、名称查询、创建/查看/重命名/删除及单张或批量 add/remove，均无需 index。Skill 把用户指定单张照片写成单元素真实 ID 数组；`[]` 是零变更而非全相册。名称去空白后非空、拒绝控制字符，NFC + casefold 唯一；重命名保留稳定 ID。一个 SQLite 内静态、平铺、多对多成员；全量验证 ID 后事务写入，错误整体回滚，重复/无变化明确计数。移出/删文件夹只删关系，不删照片/原图/缩略图/向量，不影响其他归属；关系随备份/移动保存。
- **范围浏览与搜索。** photos 和两种 search 均支持重复 `--folder-id`；多个不同 ID 必须明确 `--folder-match union|intersection`。不指定即全相册，未知文件夹报错不兜底，空范围无结果不加载 encoder（语义模式仍需有效显式/默认 profile）。先范围过滤再检查向量/排名/top-K，并集去重；计数、分页、覆盖率及分差按范围。metadata 的 `album_total` 为真实全库数，另报 `scope_total`。
- **可选一次性整理。** 搜索结果 add 使用明确目标/ID 和 `--search-snapshot`，复用 `management.select_search_results` 验证，不重查、不编码图片、不默认加入全部 top-K 或移除来源关系。日期计划严格 `--all` 或 `--ids-file`，按已保存且日历合法的 EXIF `datetime_original` 相机本地日期，无 UTC 转换/文件时间兜底；缺失/非法/不可用导入元数据跳过并计数。CLI 回传 `plan/digest/output/album/model_calls: 0` envelope，文件是 raw plan；展示创建/复用和确切成员范围后，以 `apply-date-plan --confirm` 明确应用，冲突/过期报错、原子写入。不加任务表/动态规则，不自动补入后续导入或手动移出的照片。
- **生命周期。** create 仅排他创建明确的新路径；open 只读检查，无 DDL/自动修复。回显 UUID、文件名显示名和绝对数据库路径。backup 是新目标上的 SQLite 一致性快照，不覆盖文件；不包含原图、模型或 Python 环境。
- **双路径。** 绝对路径优先，缺失才尝试相对于当前数据库父目录的路径；可含 `..`，不同盘符/UNC share 存 `NULL` 并警告。绝对路径命中不自动重写相对路径。两处都缺失才是 source missing；权限/I/O 故障另报 unavailable，不能借故选另一份。
- **明确路径维护。** original 可写入定位修复，relink 必须 SHA-256 匹配才更新双路径；只读/锁定修复失败明确报告，不能假装成功。路径变化保留 ID、内容版本、预览和向量；只有 ingestion 更新内容版本。`original_status` 与 `ingest_state` 分离，定位/重连不能清除入库错误。
- **稳定导入身份。** 移动后已有绝对/相对路径仍能无歧义匹配才沿用 photo ID；绝对位置仍有效而相对副本指向别处时报冲突，不按 hash 自动去重。完整扫描仅检查本来源范围内缺失，其他目录保留；不完整扫描不推断 missing。
- **保留 index 邀请。** `index_prompt` 给出本次成功且尚未 ready 的确切 ID；用用户语言解释无索引可浏览/按文件名路径查找、管理手动文件夹/名称搜索/经确认的日期整理，有索引及兼容模型可按中英文画面语义搜候选，再询问是否准备索引。不自动下载、建计划或执行；确认邀请不等于批准未展示的执行摘要。
- **精确计划和恢复。** `index plan` 必须 `--all` 或 `--ids-file`，可选 profile/limit/dry-run；execute 需真实批准的 digest。resume 复用既有推理批准；原状态 `running` 时还须确认所有设备旧 worker 已停止并传 `--confirm-stopped`。该标志不抢占活跃 OS 锁，也不授予推理批准；纯缓存任务不加载模型、不扩展成推理。
- **共享本机模型缓存。** `Config.model_cache_root` 与相册文件分离；Windows 默认 `%LOCALAPPDATA%\SmartAlbums\models`，其他平台用应用/XDG 缓存。可选全局 `--model-cache-dir` 在 setup/index/semantic search 中一致。已知旧权重可显式指定缓存根复用，不自动搬移或删除。
- **图片向量的明确含义。** `image-embedding-profile-v1`，`embedding_kind: image_text_semantic`、`stored_modality: image`、`input_scope: stored_thumbnail`、`granularity: whole_image`。持久化 768 维整图语义图片向量；配套文字查询向量只在查询内使用，不落表。输出 `component: image_embedding` ready 不代表对焦/模糊等技术分析已完成。

初始模型仍固定为 `google/siglip2-base-patch16-224`，revision `75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2`，Transformers + PyTorch CPU FP32。官方 224×224 方形缩放仅用于推理，SQLite 预览保持比例，无自定义裁剪/填边。setup 才可显式下载，第一次也不设置默认；新 profile 语义字段会影响 ID，但缓存路径不影响。当前可选环境仍是标准 GIL CPython 3.14 x86-64 CPU，不承诺 ARM、其他 Python 或任意主机都能跑模型。

普通 browse/search 不 stat 原图、不写数据库、不顺带修复位置或建表。`album-snapshot-v2` 明确 `scope: {"kind":"album"}` 或 `{"kind":"virtual_folders","match":"union","folders":[{"folder_id":"...","name":"..."}]}`（也支持 intersection），`coverage_scope` 为 `entire_album|selected_folders`；候选、show-results 和报告保留查询时范围/名称，文件夹变化不重查，选中照片身份变化仍报 stale。语义决定只用 embedding 分数/排名/分差；文件夹标签只是范围，EXIF 日期是经授权的确定性整理而非语义证据，不向 agent 传图片/缩略图/HTML 像素。HTML 无写控件、无旧 `search-add` 协议兼容。云端使用完整本地文件：下载 → 本地操作 → 全部停止/关闭 → 复制/同步；单用户单设备写入，本地锁不是分布式锁。模型与运行时由每台主机独立准备。

## 离线验证

- [x] **文件夹功能合并回归。** 175 项测试中 174 项通过，1 项因 Windows 符号链接权限跳过。使用现有 unittest 合并运行 `test_virtual_folders`、`test_date_folders`、`test_management`、`test_capabilities`、`test_album_files`、`test_skill_contracts`：覆盖严格 13 表/v1–v8 拒绝、手动多重归属、事务回滚/锁竞争、范围先于 top-K、历史快照、日期计划/冲突/过期、备份/移动及 Skill 合同。数据均为合成图和独立临时相册；没有真实照片推理、模型下载或修改真实旧库。

## 必须后续完成

- [ ] **新格式真实模型与断网验证，需单独授权。** 本轮没有执行新格式真实模型试跑；此前性能记录不是 schema 9 验收。代码批准不包含下载、真实照片处理或正式库修改授权。先确认兼容环境及显式缓存根，在单独新相册验证旧权重复用、固定清单下载/中断恢复、损坏文件和真正断网推理；普通计划/索引/搜索不得自动联网安装或回退云端。
- [ ] **真实模型质量与资源验收。** 先用非敏感测试图，再使用用户明确指定的代表性照片；人工标注中英文配对查询，覆盖主体/场景/色彩/构图/组合、精确条件和无匹配情况。报告 top-K、Recall@K、语言差异及失败样例；分别测模型加载、逐图编码、文字编码、排序和内存。不承诺未测的速度、节省量或检索质量。
- [ ] **新相册跨位置/跨设备备份恢复演练，需单独授权。** 使用独立新文件验证一致性备份、UUID/元数据/预览/向量、相对路径 fallback、跨盘 relink、原图离线浏览及 running 任务停机确认。停止操作后再移动/同步；不是旧库自动迁移或生产库演练，也不证明任意网络文件系统安全。
- [ ] **NaFlex 新 profile。** 明确输入分辨率/patch 或 token 预算、处理器和运行时身份；与当前 224 模型比较中英文检索质量、比例相关失败、内存和耗时。保留现有索引，不原地替换向量；仅在明确请求后切换默认。
- [ ] **技术参数：index 内独立组件，不新增第四能力。** 定义字段、单位、适用输入范围（缩略图/原图/裁剪）、输入哈希、算法/模型来源及版本。独立 technical 配置、结果、状态、任务、恢复、失效规则和授权；不借用 `image_embedding_*` 表，不预建空表/空结果。更新技术算法不重算 embedding，缺失技术结果是未知；缩略图模糊不等于原图对焦失败，也不能把主观描述当测量值。
- [ ] **ONNX / 量化独立评估。** 验证目标平台运行时和依赖兼容性，测量内存/延迟，检查向量数值与归一化、排序变化和真实检索质量。使用新 profile/runtime 身份；不与 FP32 结果原地混用，不因同维度跨配置补齐。

## 暂缓，不扩大首版

- [ ] 跨相册搜索、合并、跨文件共享照片/向量、自动云同步或分布式并发写入。
- [ ] 文件夹层级、动态规则、自动归类和网页写控件；现有手动/一次性 CLI 组织不扩展为这些功能。
- [ ] 聚类、自动发现系列、以图搜图、重复检测/删除、自动选片及独立语音识别。
- [ ] 原图强制全量哈希/变化监控：现有 ingestion 基于 size/mtime 的快速检查可能漏掉保留属性的外部修改；不把读取保存预览描述为实时核验原图。
