# 项目待办

当前合同见 [便携相册、本地特征与 AI Review 设计](index-design.md)。[便携相册实施计划](portable-album-plan.md) 保留 schema 8 历史里程碑及 schema 10 后续记录，不是 schema 12 四能力的实时进度；[早期实施计划](ingestion-index-management-plan.md) 的多相册、旧 CLI 和迁移设计亦已被取代。[代码审查后续讨论](code-review-follow-up.zh-CN.md) 保留当时提案，不是当前合同。以下区分实现范围、历史回归及待核实验收，不发布私人路径、照片或报告数据。

## 当前四能力范围

| 能力 | 当前职责 | 边界 |
| --- | --- | --- |
| ingestion | 当前相册内增量导入、基础元数据、双原图路径、等比例 SQLite JPEG 缩略图 | 不调用模型、不自动 index、不删除原图、不创建内部相册 |
| index | 保留 image_embedding；新增显式选择的 ocr/objects/scene/color/composition/perceptual_hash，独立配置、结果/历史、计划/确认/恢复、原型及配对证据 | 不通过描述中转、不自动改默认、不自动补依赖、不改变现有搜索 |
| management | 文件 create/open/backup、手动静态文件夹、范围浏览/搜索、一次性日期整理、original/relink、缩略图与扫描诊断 | 普通浏览/搜索真正只读；文件夹/路径写入需明确请求，不自动补索引、归类或增加网页写控件 |
| review | 本地计划、整任务明确批准、可选 Copilot JPEG 预览摄影评价、结构化分数/历史及明确恢复 | 不自动联系提供方，不发原图、不组内排名、不改变现有搜索；重试须新批准 |

## 已实现代码范围

- **一个相册一个 SQLite 文件。** 先明确选择已有文件或创建新文件；已选择则不重复询问，普通说明性问题无需选库。全局必填 `--database <绝对路径>`，支持 `.sqlite/.sqlite3/.db`；无默认库、`--state-dir`、旧别名或内部 album/library 选择。换文件清空旧 photo/profile/run/folder 和待确认上下文。
- **新格式边界。** schema 12、`application_id=0x53414C42`，40 张注册表：35 张普通表、1 张 external-content FTS5 虚拟表、4 张显式登记的影子表（不计 SQLite 内部 `sqlite_sequence`）。完整清单见现行设计；原六张 `image_embedding_*` 不变，公共 feature 配置/结果/manifest/依赖/任务及类型明细独立存储。新增 `ai_review_results` 单一分数/文本/固定 JSON 路径结果表，及 `ai_review_runs`、`ai_review_batches` 运行表，不新增 FTS。`virtual_folders`、`virtual_folder_photos` 保留。没有旧多相册或空 `technical_*` 表。旧 v1–v10 数据库原样拒绝，不迁移、清空或覆盖；真实旧库及备份保持不动。管理快照仍为 `album-snapshot-v2`。
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

### 第一阶段新增范围

- 六组件必须显式 `--component`；未传仍只做 `image_embedding`，原有 ingestion/index/management 行为和 ingestion 邀请不变。setup/`register-profile` 不设默认，各组件独立 configure。修改有效参数/资产/recipe/dependency 产生新 profile；`input_fingerprint` 保存输入/精确依赖身份而非路径，历史不覆盖。CLI 见 [index](../photography/references/index.md#feature-cli)。
- OCR 用实际验证的原图字节及 EXIF 方向；objects/color/hash 用现有默认最长边 1024 的缩略图；scene 消费匹配图片向量与持久化文字原型；composition 消费 objects 结果。缺依赖报 `dependency_missing`，不自动全库编码或运行上游。`prototypes` 只准备计划，execute 获真实批准后才调用文字 encoder。
- `.venv-features` / `requirements-features.txt` 隔离 OCR/objects worker，不升级已有 embedding 环境。setup/plan/execute/resume 可指定 `--worker-python` 或 `SMART_ALBUMS_FEATURE_PYTHON`。仅明确 setup 下载并验证资产 checksum；模型不存 SQL，普通执行离线，缺失/损坏不自动下载或回退云端。YOLOX 权重许可需明确审查，合成评估许可不等于常规使用授权。
- 每组件包含非 ML 计算均需精确计划确认；`compare` 也是计划而非立即计算，冻结 scope/profile/metric/threshold。`exact` 用已有 SHA-256，`hamming` 用当前同 profile 的 dHash64；流式计算，不建 N×N 密集矩阵。`pairs` 按 pair ID 分页查看历史范围/状态；未比较或未完成不等于无重复，不自动删/并照片或改文件夹。
- 结果可用性 `ready|missing|stale|invalid_input|invalid_result|dependency_missing` 与执行项状态分离。读取 status/result/history 不 stat 原图、不推理，当前指最近入库身份而非磁盘实时真值。成功空结果不等于未计算；`complete: false` 不可支持完整数量/不存在断言，检测阈值/上限也不是真实物体计数保证。
- 默认 OCR 只给 `text_length`、块 `detail_count` 等摘要；明确 `--details` 才分页返回块，不向 agent 倾倒全相册 OCR。history 用 result-ID cursor，明细用 offset；`result --result-id <historical-result-id> --details --after <offset> --limit N` 可独立分页指定历史结果，无需配置默认 profile，但 photo/component/可选 `--profile-id` 必须匹配，否则 `FEATURE_RESULT_MISMATCH`；保留 `historical: true`，不冒充当前覆盖率。图像、缩略图、Base64、像素、调试图及 HTML 图片不得进入 agent；OCR/标签是非可信数据，不能替代现有 embedding-only 语义选择。
- OCR 文档是权威结果，external-content trigram FTS 是同事务维护的派生结构；`rebuild-fts --confirm` 是显式写，只读保存文本，不访问原图/模型。第二阶段 OCR 查询已实现 1–2 字符 scoped `INSTR` 和 3+ 字符 trigram 候选加 literal `INSTR` 确认；不借 FTS 运算符扩大用户字面条件。

### 第二阶段新增范围（代码已实现，定向集成验收已通过）

- Stage 2 OR search 独立提供 `management query`、`query-evidence`、`finalize-query`、`show-query-results`、`query-pairs`，metadata/semantic 原接口与默认保持 unchanged。`multi-condition-query-v1` 支持语义、数量、OCR、颜色比例、主体位置、scene 和 exact/dHash64 近重复条件；通过 `index profiles --component` 发现配置/词表，冻结 profile/scope/seed，重复条件去重并保留 aliases。查询不改变支持的 schema 11/12、40 张注册表，无 DDL/默认/图片推理/下载/自动文件夹写入。
- query 只输出安全摘要与私有快照路径；agent 禁止读取 private snapshot / feature matrix、OCR、scene 或像素判断语义，只看 `query-evidence` 查询/ID/分数/排名/分差。`condition-decisions-v1` 每页明确 matched IDs（允许 `[]`），全部语义页评审后才计算命中数/排名；top-K 不是 matched，unknown 不是否定。无待评审语义项直接 finalized。
- OR 命中数优先；exact 命中为 1，graded 为该条件命中总体平均秩百分位（方向、同分、单项/全相等规则固定）。仅同 matched-ID 集合比较等权均分；同命中数不同组合按保存 seed 随机交织，不承诺 RRF/顶层 AND/pHash。finalize `model_calls: 0`，保留历史 `query_model_calls`。
- 公共 `condition-search-page-v1` 区分整个 scope 的 input `coverage`（语义 `not_reviewed` 表示查询时 eligible）与 candidate pool 的 final `evaluated_coverage`，解释候选上限/部分覆盖。全局 result_rank 与 opaque cursor 在排名后分页；只读历史范围、不查原图、不重查当前成员。所有导出目标先校验，并保护输入/alias/hardlink；用户专用 HTML 只渲染本页匹配身份预览，转义文本/CSP，无脚本/表单/控件/backend，agent 不打开/截图/读取图片。
- 仅用户明确目标/IDs 后 `folders add --query-snapshot` 才在事务内验证 finalized 来源并写成员；与 `--search-snapshot` 互斥，JSON null 报错、`[]` 零变更，返回独立 `source_query`，不重新查询/分类/调用模型。
- `query-pairs <ranked.json> --condition-id D` 独立输出 `condition-duplicate-pairs-v1`：finalized 重复条件的真实 photo-ID pair、距离/metric、历史 scope/input_coverage 与 opaque 分页（默认 100、范围 1–1000，可选 JSON 导出，无 HTML）。校验当前保存来源，不调用模型/原图、不执行 index compare；不把 A–B/B–C 变为传递组，不自动删除/合并/修改文件夹。

初始模型仍固定为 `google/siglip2-base-patch16-224`，revision `75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2`，Transformers + PyTorch CPU FP32。官方 224×224 方形缩放仅用于推理，SQLite 预览保持比例，无自定义裁剪/填边。setup 才可显式下载，第一次也不设置默认；新 profile 语义字段会影响 ID，但缓存路径不影响。当前可选环境仍是标准 GIL CPython 3.14 x86-64 CPU，不承诺 ARM、其他 Python 或任意主机都能跑模型。

普通 browse/search 不 stat 原图、不写数据库、不顺带修复位置或建表。`album-snapshot-v2` 明确 `scope: {"kind":"album"}` 或 `{"kind":"virtual_folders","match":"union","folders":[{"folder_id":"...","name":"..."}]}`（也支持 intersection），`coverage_scope` 为 `entire_album|selected_folders`；候选、show-results 和报告保留查询时范围/名称，文件夹变化不重查，选中照片身份变化仍报 stale。语义决定只用 embedding 分数/排名/分差；文件夹标签只是范围，EXIF 日期是经授权的确定性整理而非语义证据，不向 agent 传图片/缩略图/HTML 像素。HTML 无写控件、无旧 `search-add` 协议兼容。云端使用完整本地文件：下载 → 本地操作 → 全部停止/关闭 → 复制/同步；单用户单设备写入，本地锁不是分布式锁。模型与运行时由每台主机独立准备。

### AI Review 当前合同

- 独立第四能力；CLI/安装说明见 [review](../photography/references/review.md)。仅用户明确选择已有 photo ID 和模型，严格校验保存的 JPEG 预览，不发原图、路径、EXIF 或相册身份。默认批次 4、允许尾批，逐图独立评价，不做组内排名。原有本地 ingestion/index/search 不变；v1 不新增 review 搜索入口，仅为未来查询保留结构化存储。
- `review models --confirm-provider-access` 是独立批准的提供方联系；没有标志须 `CONFIRMATION_REQUIRED`，不得构造 SDK。认证/模型探测也算联系 Copilot。help/plan/rubric/job/result/history 无 SDK 构造；dry-run 不保存。整任务计划显示云传输、确切范围、模型/语言/输入大小、缓存及批次，实际批准后才可构造 SDK、认证、建 session 或上传；旧任务、代码批准和 models 批准不可替代。
- `photo-review-v2` 严格 JSON：顶层只有 `results`，按 manifest 顺序映射；`dimensions` 六维分数为 0–10 的 0.5 步长或 null，`review_status` 必须与空评分一致。改进建议含 `kind/action/rationale/tradeoff`，允许空优点和建议数组；拒绝 Markdown 围栏。仅六维均有分数时本地计算两位 decimal half-up 等权平均，其他情况总分为 null。无效响应整批失败，不保存原始 review；输入/配置匹配才复用，force 新增历史；失败停止后续请求，resume 使用新 state-bound digest，不自动重试/修复/回退。
- 整个 Skill 自包含 prompt 与可选 `requirements-review.txt`，固定 SDK 1.0.13 / runtime 1.0.83；安装和 runtime 下载均需明确授权。默认现有本地 Copilot 登录 `mode="copilot-cli"` / `use_logged_in_user=True`，不需独立 token、不复制凭据、不自动登录；只排除 child 环境覆盖，独立 owned working/session state 不移动 credential home。限制/清理不是 OS sandbox、无日志、无远端保留或精确计费保证。
- `review report <run-id> --output <absolute-new.html>` 只读相册，向新的绝对 HTML 路径不覆盖导出自包含报告；显示保存的预览/结构化结果、实际 active timing 和提供方已报告 token/credit，不访问原图/SDK/外部网络、不写相册。缺失用量为 unknown 而非零，重试覆盖不全须注明；Copilot credits 不是 Azure credits，不编造换算/价格或每图费用。多图批次指标共享，不除以图片数伪造逐图值，也不逐图累加重复计算。独立批次 1 才能归属该请求指标到单图，产品默认 4 不变。

## 离线验证

- [x] **历史：schema 9 文件夹功能合并回归。** 175 项测试中 174 项通过，1 项因 Windows 符号链接权限跳过。使用现有 unittest 合并运行 `test_virtual_folders`、`test_date_folders`、`test_management`、`test_capabilities`、`test_album_files`、`test_skill_contracts`：当时覆盖 13 表/v1–v8 拒绝、手动多重归属、事务回滚/锁竞争、范围先于 top-K、历史快照、日期计划/冲突/过期、备份/移动及 Skill 合同。数据均为合成图和独立测试相册；此记录不是 schema 10 或六组件验收。
- [x] **第一阶段文档/Skill 合同。** `.venv-index` 中运行 `python -m unittest discover -s tests -p test_skill_contracts.py`，25 项通过；覆盖实际内存 DDL 表清单、文档命令参数解析、历史结果独立明细分页、三能力/六组件、只读/隐私/授权、阶段边界及本地链接。未运行模型、访问照片或改写相册，不代替业务集成验收。
- [x] **第一阶段集中回归。** 332 项中 322 通过，10 项为 Windows 符号链接权限或原 embedding 环境没有可选 vision 依赖而跳过；在隔离 vision 环境另跑 27 项全部通过。覆盖 schema 10、v1–v9 原样拒绝、六组件明细/空与不完整结果、依赖失效、批准/恢复、只读/隐私、FTS 重建、配对和旧搜索。审查后补上了配对 checkpoint 回滚不跳过数据、缺原图不阻塞其他 OCR、历史明细独立分页和批量单项校验的线性调用次数回归。
- [x] **授权的真实模型合成验证。** 只对三张自产合成图运行 OCR、YOLOX、颜色、dHash、构图、scene，全部完成并持久化；OCR 命中预期中英文。复用已有 SigLIP 权重生成基础图片向量和 16 个场景文字原型，后续逐图 scene 零模型调用。exact/hamming 比较保存配对；备份并移离原图后，18 份结果仍可只读读取。新资产共 35,408,916 字节（约 35.4 MB），没有处理真实图库。普通 Python 下载入口在复验中被禁止，worker 禁止联网；这不替代 OS 级断网/跨设备测试。
- [x] **第二阶段 OR 搜索定向验收。** 203 项测试中 202 通过，1 项 Windows 符号链接权限跳过，覆盖 predicates、ranker、query/finalize/evidence/show/pairs、显式 folder add 和原 management/能力/Skill 合同。另在既有合成相册上只编码一次文字，以数字证据确认语义命中，最终命中数为 `[3,3,1]`，一对 exact 重复，分页顺序固定，SQLite 字节不变，零图片推理。修复并回归了必要来源/重复见证缺失、畸形 UUID 和 exact 配对分页平方级工作；8,000 照片/4,000 配对的纯分页测试验证线性内容比较次数，不是耗时承诺。

上述记录保留当时 schema/测试数量，不作为 schema 11 或 Copilot 实调验收。
- [x] **AI Review 初轮文档/Skill 合同。** 运行 `.\.venv-index\Scripts\python.exe -m unittest discover -s tests -p test_skill_contracts.py`，48 项通过。覆盖当前四能力、实际内存 DDL 的 schema 11/40 表及完整清单、现行拒绝旧库文案、原有本地搜索合同、批准/重试/结构化结果说明、文档 CLI 解析、JSON 示例校验、可选依赖及复制安装后的 prompt/当时全部 review 帮助入口（禁止 SDK/模型导入）。无提供方联系、依赖安装或 runtime 下载；不代替下述执行/存储业务集成及实调验收。
- [x] **Review report 文档合同扩展。** 同一命令复验 49 项通过，新增 report CLI 参数解析与无 SDK 的独立安装 help、只读相册/不覆盖自包含 HTML 文案、实际/unknown 用量、批次与单图指标区分及随机 10 图任务仍需 frozen-plan 批准。这里只验证合同，不代表已生成真实照片报告或完成实调。
- [x] **AI Review 离线集成验收。** `.\.venv-index\Scripts\python.exe -m unittest discover -s tests -q -k test_review -k test_album_files -k test_capabilities -k test_skill_contracts -k test_image_embedding -k test_feature_storage -k test_management` 共 331 项，329 通过、2 跳过。代码复核后改为直接处理 SDK typed session.error，避免原始提供方异常泄漏，并修复同长度损坏预览的 current 状态；随后 `-k test_review` 专项 97 项全部通过。覆盖 schema 11/40 表、旧库原样拒绝、结构化输出及 SQL 投影、批准/恢复、原子批次、缓存/历史、备份、独立安装及 HTML 转义/不覆盖/只读/真实用量。浏览器已核验合成报告桌面和 390px 手机布局、图片加载及指标展示；合成用量不是实调计费证据。
- [x] **用户指定真实来源的本地准备。** 在新相册中完成 101 张照片的 ingestion 和 101 份 SigLIP2 image_embedding，失败为零，复用原有固定权重、未下载模型。随机 10 张的真实 ID、配置与 digest 已冻结；本地抽样 HTML 的 10 张预览均加载成功，并明确显示 pending / Not executed。SDK 1.0.13 已安装、runtime 1.0.83 已显式准备并检查文件，但尚未启动 Copilot、读取登录状态或发送照片。私有相册/路径/抽样清单不入仓库。

## 必须后续完成

- [x] **随机 10 张真实照片 Review 及 HTML 展示。** 用户随后明确批准每批 4 张及各次重试；使用 Claude Sonnet 5 按 4+4+2 完成，十份结构化结果已重开验证。成功执行约 135.53 秒，输入 26,341 / 输出 8,488 tokens；包括两次失败模型响应的已报告总量为输入 44,243 / 输出 15,053 tokens。Sonnet 任务累计 active time 247.18 秒，另一次 GPT-5.4 数量限制预检 6.06 秒；不含等待确认、单独诊断和报告生成。GPT-5.4 在该账户的声明限制为每次 1 图，不能硬编码所有模型均支持 4 图。修复 Windows 原生工作路径、custom session FS 清理顺序，并获准仅拆除单一 JSON 代码块外包装后继续严格校验；无效内容仍失败。重试批准保留此前失败用量，不重复按图片数累加。网页验证十张预览、六十项评分及五次有 token 记录的批次尝试；owned state 清理后为空。最终执行 `python -m unittest tests.test_review_schema tests.test_review_storage tests.test_review_provider tests.test_review_execution tests.test_review_report tests.test_review_cli tests.test_skill_contracts -q`，145 项全部通过。此验收不代表主观摄影质量、精确账户收费或远端零保留保证；私有文件仍不入仓库。
- [ ] **YOLOX 常规权重使用许可。** 当前只有明确授权的 `synthetic-evaluation` 验证；普通使用仍保留许可审查门槛。不能把测试通过或设置环境变量当成上游权重授权，也不能自动将门槛切为 approved。

- [ ] **真实模型与断网验收。** 以上合成评估授权不扩展为真实照片或正式库授权；此前性能记录不是 schema 10 验收。先核实隔离环境及显式缓存根，在独立新相册验证权重复用、固定清单下载/中断恢复、损坏文件和真正断网推理；普通计划/索引/搜索不得自动联网安装或回退云端。未验证的真实模型性能不作承诺。
- [ ] **真实模型质量与资源验收。** 先用非敏感测试图，再使用用户明确指定的代表性照片；人工标注中英文配对查询，覆盖主体/场景/色彩/构图/组合、精确条件和无匹配情况。报告 top-K、Recall@K、语言差异及失败样例；分别测模型加载、逐图编码、文字编码、排序和内存。不承诺未测的速度、节省量或检索质量。
- [ ] **新相册跨位置/跨设备备份恢复演练，需单独授权。** 使用独立新文件验证一致性备份、UUID/元数据/预览/向量、相对路径 fallback、跨盘 relink、原图离线浏览及 running 任务停机确认。停止操作后再移动/同步；不是旧库自动迁移或生产库演练，也不证明任意网络文件系统安全。
- [ ] **NaFlex 新 profile。** 明确输入分辨率/patch 或 token 预算、处理器和运行时身份；与当前 224 模型比较中英文检索质量、比例相关失败、内存和耗时。保留现有索引，不原地替换向量；仅在明确请求后切换默认。
- [ ] **技术参数：对焦/曝光等测量仍为后续 index 工作，不另加能力。** 不把六组件、embedding ready 或 AI Review 的主观 technical 分数当作原图技术测量已完成；明确输入范围、单位、算法/profile 和独立结果/授权，不预建空表/空结果。缺失数据是未知；缩略图模糊不等于原图对焦失败，主观描述不是测量。
- [ ] **Embedding ONNX / 量化独立评估。** OCR/objects 已有 ONNX worker 不代表 SigLIP 后端已切换。需验证依赖/平台、内存/延迟、向量数值、排序及真实质量；使用新 profile/runtime 身份，不与 FP32 结果原地混用，不因同维度跨配置补齐。

## 暂缓，不扩大首版

- [ ] 跨相册搜索、合并、跨文件共享照片/向量、自动云同步或分布式并发写入。
- [ ] 文件夹层级、动态规则、自动归类和网页写控件；现有手动/一次性 CLI 组织不扩展为这些功能。
- [ ] 聚类、自动发现系列、以图搜图、自动重复合并/删除、自动选片及独立语音识别。第一阶段显式配对证据不授权这些操作。
- [ ] 原图强制全量哈希/变化监控：现有 ingestion 基于 size/mtime 的快速检查可能漏掉保留属性的外部修改；不把读取保存预览描述为实时核验原图。

## Photo review v2 contract

已接入 `photo-review-v2.txt`，严格检查 `results`、manifest 顺序、六维 0.5 分步长、空评分与状态一致性，以及结构化改进建议。新相册使用 schema 12；schema 11 可继续读取，通过 `review upgrade --output <新文件>` 创建升级副本，保留源文件及 v1 历史。结构校验不等于视觉依据、输出语言及预览限制说明的语义验收。历史真实云端试验只适用于 v1，v2 本次验证使用合成数据与模拟 provider。
