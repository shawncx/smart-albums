# Smart Albums 三能力重构实施计划

> 历史计划：下文的旧多相册、迁移、CLI 兼容及验收记录保留原意，不是现行使用合同。当前见 [现行设计](index-design.md)：一个 SQLite 文件一个相册，schema 10、37 张注册表（32 普通 + 1 external-content FTS5 + 4 显式影子表，不计内部 `sqlite_sequence`），`album-snapshot-v2`；v1–v9 原样拒绝且不迁移。原 embedding 六表与 management 静态文件夹合同保留，六项 opt-in feature 属 index，基础三能力不变。第二阶段 OR 搜索/组合排序计划中、尚未实现；metadata/semantic 默认及 embedding-only 展示不变，未核实的新模型性能不作验收声明。

状态：首版代码已实现并通过离线回归。本文保留已确认的实施范围；真实模型下载、推理和正式图库验收仍待单独授权，NaFlex、技术参数及 ONNX/量化仍为后续工作。

## 1. 目标与已确认范围

Skill 对外只提供三个能力，不拆成三个独立 Skill：

| 能力 | 首版职责 | 不包含 |
| --- | --- | --- |
| ingestion | 导入照片，读取基础元数据，保存等比例缩略图及原图路径 | 模型调用、自动 index、删除原图 |
| index | 本地图片 embedding 的安装准备、生成、复用、状态、恢复及配置 | 文字描述中转、技术参数计算、云端模型回退 |
| management | 查看相册/照片；按相册名、文件名查找；中英文语义搜图 | 新增相册写操作、自动选片、删除、聚类、以图搜图 |

已确认的实现基线：

- 首个模型：`google/siglip2-base-patch16-224`。
- 初始固定 revision：`75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2`。
- Transformers + PyTorch CPU、FP32、单图串行；不使用 Ollama。
- index 只读取 SQLite 缩略图。官方 224×224 预处理仅作用于推理输入，
  不改写已保存的等比例缩略图；首版不擅自增加裁剪或填边。
- 每张图片每套配置：有匹配当前输入的有效结果就复用，没有就生成。
- 换模型保留过去的索引；ingestion 发现图片变化后旧输入索引失效。
- 可以替换旧缩略图，不要求保存图片字节历史；程序不删除原图。
- 查询只使用一套明确的编码配置及配套文本编码器，不跨模型补齐结果。
- 建立新模型索引不自动切换默认查询配置。
- 技术参数与 embedding 独立，首版只做 embedding。
- docs 和 Skill 更新属于本次交付，不再沿用旧的描述文本索引方案。

底层 CLI 可以保留已有兼容命令；“三个能力”不等于只能有三个具体操作。
相册写操作和 `search-add` 保留为兼容接口，不作为第四类能力。
后续已确认移除不再需要的 OpenAI/Codex analysis 链路及废弃表；
v7 迁移先完整备份旧库，再删除 9 张旧分析/文本向量表，当前图片索引历史不受影响。
新 management 只读流程不会触发这些兼容写操作。
ingestion 已有的 `--album-name` 绑定行为保留，不借本次重构删除用户能力。

## 2. 当前代码基线

基于提交 `720a437` 加工作区中已完成的旧文本索引清理，而不是恢复提交中的旧链路：

- 已删除旧模型适配、描述拼接、向量生成和可选依赖文件。
- 已撤下旧 `embedding-setup`、`embed`、`embedding-status`、顶层 `search` 命令。
- 保留数据库 v5、历史向量表、已有搜索快照选片和报告渲染。
- 清理后的相关测试共执行 82 项；其中一个帮助文本换行断言修正后单独重跑通过。
  这是前一轮验证记录，不是本轮新功能的测试结果。
- 文档仍描述旧路线，需在本次实现中同步更新；不将旧文档当作现有可用命令。

重点复用：

- `ingest.py` / `images.py` 的增量扫描、EXIF 方向、sRGB 和等比例缩略图处理。
- `thumbnails.stored_preview` 的版本、JPEG 解码及哈希检查。
- `sqlite_storage.py` 的备份、事务、savepoint、只读快照和相册关联。
- 现有稳定 JSON 指纹算法；如抽取通用位置，保持旧指纹字节结果不变。
- 既有错误格式、JSON 输出、退出码及 `exports.export_path` 路径保护。
- 工作流的确认摘要、短事务、逐项任务记录模式；不复用 OpenAI 请求分派。

不直接复用：

- `analyze.prepare/read_preview/assert_current`：它们要求原图在线。
- 旧 `needs_analysis` / 最新描述选择：图片索引不依赖任何分析记录。
- 旧向量表作为新结果表：它强制关联 `analysis_id`，且主键会覆盖同模型的旧输入。

## 3. 对外操作合同

所有命令保留全局 `--state-dir`、UTF-8 JSON 和结构化错误。
新增根入口使用 `ingestion`、`index`、`management`。

### ingestion

```text
ingestion <absolute-photo-directory> [--album-name <name>]
```

- 与原 `ingest` 共用实现；保留 `ingest` 别名和 Python 导入兼容性。
- 缩略图仍默认最长边 1024、JPEG 质量 85、无上采样，比例保持到像素取整精度。
- 无论模型是否安装，ingestion 都可以使用。
- 返回新的 index 摘要和下一步提示，不再引导新 Skill 去跑云端分析。
- 新返回值移除旧观察汇总和分析建议，只提供 index 汇总；历史 JSON 不批量重写。
- 索引失效由当前输入版本/哈希匹配判定，不批量删除历史结果，不在扫描中编码。

### index

```text
index setup
index profiles
index configure --default-profile <profile-id>
index plan --album-id <id> [--profile-id <id>] [--limit N]
index plan --library-id <id> [--profile-id <id>] [--limit N]
index plan --ids-file <path> [--profile-id <id>]
index execute <run-id> --confirm <digest>
index status [--album-id <id> | --library-id <id>] [--profile-id <id>]
index job <run-id>
index resume <run-id>
```

- `setup` 是明确的模型下载/校验动作，普通计划、索引和查询不自动下载。
- `plan` 必须有明确照片范围；不因为没有参数就选整个数据库。
- 默认选中明确指定范围内的全部照片，支持正整数 limit；数量在执行前展示。
- `plan --dry-run` 返回预检但不保存任务；正式 plan 保存输入快照和确认摘要。
- 配置选择顺序：显式 profile ID，其次持久化默认；默认未设置则明确提示配置。
- `setup` 注册配置但不暗中改默认；初次使用文档明确说明 `configure` 一步。
- 首版不提供强制覆盖有效结果的 `--force`，遵循“已有有效结果就复用”。
- 缺失或损坏的结果在已确认范围内重建；已成功持久化的项恢复时不重复编码。
- 纯缓存计划不推理；不加载模型、不修改默认配置。
- 状态查询不读取原图、不加载模型；明确区分元数据检查与完整 BLOB 校验。

### management

```text
management albums [--limit N] [--after <cursor>]
management photos [--album-id <id> | --library-id <id>] [--limit N] [--after <cursor>]
management photo <photo-id> [--html <output>]
management search "<text>" --mode metadata --target albums|photos [...]
management search "<text>" --mode semantic [--album-id <id> | --library-id <id>] [--profile-id <id>] [--limit N]
```

- 未指定照片范围时查看/搜索全库；相册和来源图库过滤互斥。
- metadata 模式必须明确查询相册还是照片；语义模式只返回照片。
- metadata 搜索首版使用 NFC + casefold 后的字面子串匹配：
  相册匹配名称，照片匹配文件名/相对路径；不是正则、SQL 通配符或描述全文检索。
- 普通浏览/metadata 搜索默认页大小 100，范围 1–1000，稳定 ID 游标。
- 语义搜索默认 top 10，范围 1–1000；不混合 metadata 分数、不增加自动模式判断。
- 空白查询或无效参数返回明确错误；无命中返回空结果，不自动切换搜索模式。
- 两类搜索都支持 JSON 导出和只读 HTML 快照；相册/照片查看支持预览。
- metadata 搜索完全不需要模型；语义搜索只编码查询文字，不重跑图片 index。
- 新 management 没有创建/重命名/删除/增删成员操作，也没有写相册的 HTML 控件。
- 读取旧库仍可能触发既有的受保护 schema 升级；“只读”指不会改业务数据、
  生成索引、变更相册成员或执行模型安装。文档说明迁移备份行为。

## 4. 模型安装与编码合同

### 4.1 依赖和部署

- 新增独立的 `requirements-index.txt`，不要恢复旧文本模型依赖文件。
- 锁定在 Windows CPU 环境可安装的 PyTorch、Transformers、tokenizer 及所需
  Hugging Face 组件版本；先验证解释器/wheel 兼容性，再固定到发布依赖中。
- 基础 ingestion/management 不导入可选推理依赖；不改共享 Python。
- 若当前 Python 不受选定依赖支持，给出兼容虚拟环境路径和明确错误，
  不安装不兼容组合、不自动改系统解释器。
- 初始不加入 ONNX、量化、GPU、FlashAttention、HTTP 推理服务器或守护进程。
- 一次执行复用同一个模型实例；模型内部 CPU 线程有明确保守上限。

### 4.2 固定身份与安装位置分离

发布内置模型清单，固定：

- repo、revision、必要的 safetensors、模型/处理器配置和 tokenizer 文件；
- 各文件摘要和大小，以及 Apache-2.0 来源说明；
- 处理器类型/版本、后端和 dtype、图片/文本特征提取方式。

`index setup` 展示模型、下载量和目标缓存位置，只下载清单文件，验证后原子发布。
中断/校验失败保留原有可用模型，重试复用已校验文件。普通运行使用本地路径、
`local_files_only=True`，不允许远端备用模型或执行远端自定义模型代码。
缓存目录迁移不改变 profile ID。

### 4.3 图片与文字编码

- 图片：验证 SQLite JPEG，解码 RGB，沿用固定 revision 的官方
  SiglipImageProcessor（224×224、相应 rescale/normalize/resampling 配置）。
- 不根据读取时机更改预处理，不使用原图路径或自行生成文字描述。
- 采用官方图片/文字特征提取接口，显式归一化；不取任意隐藏层代替全局特征。
- 首版输出 768 维、L2 归一化 float32、小端 BLOB，每条 3072 字节。
- 文本：同一检查点的 tokenizer/文本编码器，固定直接查询模板，不自动翻译，
  不沿用旧文本模型的 query/passage 前缀。
- 根据固定模型 text config 的上下文上限计数，按官方方式 padding；
  超长查询明确报错，不静默截断。配置和测试固定特殊 token 的处理方式。
- 显式验证维度、有限数值、非零范数、归一化、字节长度和校验和。
- 输出时间只记录实测，分开模型加载、图片编码、查询编码和排序；未知值不填零。

## 5. SQLite v7 与当前索引历史保留

独立图片索引存储不依赖旧描述向量。当前 v7 保留 13 张在用表；
已废弃的旧分析/工作流/文本向量 9 张表在完整备份后删除，新库不再创建。
照片、相册、缩略图、扫描和当前图片索引及其历史逐行保留。
复用现有 SQLite mixin 模式，模型调用期间不持有写事务。

### 建议表

| 表 | 职责 |
| --- | --- |
| `image_index_profiles` | profile ID、不可变模型/处理配置 JSON、创建时间 |
| `image_index_results` | 图片 embedding、输入与配置身份、维度/类型/哈希、创建时间 |
| `image_index_runs` | 固定计划、摘要、确认状态、执行状态及统计 |
| `image_index_items` | 每张照片的输入快照、结果关联、状态、尝试记录及错误 |
| `image_index_claims` | 当前输入键的执行占用，防止跨任务重复计算 |
| `image_index_settings` | 独立默认 profile；不混入旧分析配置 |

结果关键列：

```text
result_id
photo_id
profile_id
content_version
thumbnail_profile
input_image_hash
vector / vector_hash
dimensions / dtype / normalized
created_at
```

- 逻辑唯一键是 `(photo_id, profile_id, content_version, thumbnail_profile, input_image_hash)`。
- profile JSON 包含权重、图片/文字处理、池化、归一化、维度、量化及运行时身份。
- 结果不引用 `analysis_id`；照片没有分析记录也可建立索引。
- 外键和结果关联必须防止任务项指向其他照片的结果；不能只靠 UI 检查。
- 正常重跑复用同键有效行。只可修复同键损坏行，不覆盖其他模型或旧输入结果。
- 不扩展“同输入同配置的强制多次试验历史”；任务尝试记录保留诊断信息。
- 图片更新不删除旧结果；查询根据当前输入匹配动态排除旧结果。
- 已删除的旧缩略图字节无法靠向量恢复，不承诺历史原图重放。
- 相册成员关系不进入索引身份。

### 迁移

- v1-v6 先做一致性备份，按依赖顺序删除指定废弃表，验证后才设置版本号 7。
- 初次空库直接创建 v7 的 13 张在用表；保留旧 v1/v2 缩略图迁移链，不读取原图。
- 检查 integrity、外键、全部保留表（包括六张 image_index 表）的行内容/摘要和 BLOB。
  旧分析/文本向量及工作流在备份中保留；DROP 后失败也完整回滚。
- 若其他保留表外键依赖废弃表，明确阻止迁移，不静默删除额外数据。
- 迁移不加载模型、不重建向量。回滚使用旧代码和迁移前备份，不直接降级新库。
- 迁移先在合成库测试；正式图库迁移是单独明确执行的操作。

## 6. 增量执行与一致性

流程：

```text
选择明确范围 → 预览/配置检查 → 确定复用或待生成 → 保存并确认计划
→ 逐图占用 → 读取验证输入 → 编码 → 保存前复核 → 短事务写结果和任务状态
```

- 摘要覆盖照片 ID 集合、输入版本/哈希、profile、处理动作，不允许执行时扩容。
- 不使用“所有其他模型都没分析过”来判断缓存；逐照片逐配置判断。
- 照片来源盘离线或被标记 missing，只要保存预览版本完整，仍可处理保存版本；
  在结果中明确输入范围，不声称已经核实原图当前内容。
- ingestion 明确记录 error 的照片不自动当作当前有效输入；提示修复/重扫。
- 预览损坏、版本不匹配或执行期间变更会使该项失败/过期，不写成当前成功结果。
- 在同一个短事务内再次读取当前输入、写结果、更新任务项并释放占用。
- 并发使用独占输入键和跨进程执行锁；恢复不能仅因时间过去就抢占活跃任务。
  Windows 使用可随进程终止释放的锁；若支持其他系统，提供等价实现和测试。
- 成功写库的项不可重复推理；崩溃前尚未持久化的计算允许在安全恢复后重试。
- 缺权重、配置不兼容、资源不足不盲目循环重试；逐图可恢复失败保留错误原因。
- 状态分层：覆盖状态 `ready/missing/stale/invalid_input/invalid_vector`；
  任务状态和最后失败独立，不能让一次失败抹掉其他仍有效的配置结果。
- 技术参数暂不创建任务或空结果，不把“未实现”伪装为已完成。

## 7. Management 检索与展示

### Metadata 搜索与查看

- 复用现有相册/照片数据访问；补充全库分页和相册详情，不另建相册数据副本。
- 使用 Python 的 Unicode NFC/casefold 或同等确定性规范，不依赖 SQLite
  默认 ASCII 大小写比较；按明确字段做字面匹配，SQL 参数化选范围。
- 照片查看展示元数据、保存预览和目标 profile 的索引状态，不依赖云端观察。
- 原图离线可浏览；预览错误按照片展示，不静默用另一个版本代替。

### 语义搜索

- 在一致读取快照中取当前图片输入、目标 profile 的有效向量和完整覆盖统计。
- 不要求所有照片都有索引；未覆盖范围明确返回，不跨模型补齐。
- 验证结果版本、维度、哈希及归一化；只对有效候选计算。
- 空候选集合直接返回空结果和覆盖率，不加载模型。
- 有候选时只编码一次查询；精确余弦排序，同分按 photo ID 稳定排序。
- 相似度不是概率，首版不使用未经校准的全局“符合条件”阈值。
- 返回 photo ID、result ID、profile ID、输入版本/哈希、分数和缩略图标识，
  不伪造 description、tags 或技术参数。
- 不让普通搜索写默认配置、生成图片向量或改变相册成员。

### 报告兼容性

- 新增只读 management 报告，支持浏览和两类搜索，不依赖旧报告的 description/
  analysis_id/needs_analysis 字段。
- 尽量复用预览、转义、导出路径和布局逻辑；不把两种快照协议混为一种。
- 新快照有独立 schema/version 和 mode，明确是否为 metadata 或 semantic 结果。
- 导出前验证路径；照片/缩略图在查询后变更时提示，不展示替换图冒充原结果。
- 旧 `search_report` / `search-add` 与旧快照继续可用并保留测试；
  新 Skill 不自动引导结果加入相册，这是后续 management 写能力的范围。

## 8. 模块与文件分工

| 文件或组件 | 变更 |
| --- | --- |
| `cli.py`、`__init__.py` | 三能力入口、ingest 兼容别名、惰性导入、输出和退出码 |
| `ingest.py`、`images.py` | 沿用现有预览逻辑，增加新索引摘要；避免无关重构 |
| `storage.py`、`sqlite_storage.py` | 新协议、v7 迁移及废弃表移除 |
| 新 `index_storage.py` | 新表、结果/任务/占用/设置存取，沿用 mixin 约定 |
| 新 `index_profiles.py` | 内置模型清单、稳定配置身份、安装路径分离 |
| 新 `siglip_embedding.py` | 官方检查点 setup、CPU 图片/文字编码、惰性依赖 |
| 新 `indexing.py` | 预检、确认、逐图执行、状态、恢复及输入复核 |
| 新 `index_cli.py` | index 子命令、明确范围及参数校验 |
| 新 `management.py`、`management_cli.py` | 查看、metadata 搜索、语义搜索和命令接线 |
| 新 `management_report.py` | 只读浏览/检索报告与快照 |
| 新 `requirements-index.txt` | 当前图片模型所需的可选、固定版本依赖 |
| 现有/新增 unittest | 兼容、迁移、模型适配、index、management、Skill 合同 |

避免引入插件市场、多种真实后端、向量数据库服务、近似索引或通用大规模任务系统。
可替换性通过明确 adapter/profile/storage 合同实现，不通过每换模型新增一张专属表。

## 9. Docs 与 Skill 交付

- `photography/SKILL.md`：保留一个 `smart-albums` Skill，能力表恰好为
  ingestion、index、management；写清自然语言意图路由、模型授权和不自动执行的边界。
- ingestion 不再推荐旧云端分析作为必要下一步；index 不读原图；
  management 模型缺失时不转云端、不隐式安装、不误报已搜全库。
- `README.md`：三能力快速入门、可选依赖、setup/configure、两类搜索、历史保留，
  新功能与保留兼容命令明确区分。
- `references/ingest.md`：更新入口、索引摘要及版本失效规则。
- 新 `references/index.md`：安装、配置、计划/确认、缓存、错误及恢复。
- 新 `references/management.md`：查看、两类搜索、范围/分页/报告和只读限制。
- 更新 `references/albums.md`、`references/search.md`，删除旧分析 references、
  失效命令和旧模型安装指导，不让多个文档给出冲突流程。
- 以新的 `docs/index-design.md` 替代原描述索引实施稿。
  删除不再适用的 `local-vision-analysis-plan.md`、`local-vision-schema-v6.sql`、
  `local-vision-analysis-examples.json`；调整 README 链接和 `.gitignore` 白名单。
- `docs/TODO.md` 区分当前可用能力与历史验收，不把历史图库计数当成本轮验收。
- 明确加入三个后续 TODO：
  1. NaFlex：新 profile、新输入预算、与 224 对照，保留旧索引，明确切换。
  2. 技术参数：定义字段、单位、输入范围和模型/算法来源，独立状态和重跑。
  3. ONNX/量化：验证运行时、内存/延迟、向量及检索质量，不原地混用结果。
- 文档中的 SQL/JSON 若保留为示例，必须由实际 v7 合同和合成 fixture 校验；
  不发布另一份与迁移代码脱节的可执行草案。

## 10. 验证与验收

沿用 `unittest`，不引入新测试框架。测试以合成图片、临时 SQLite 和显式测试
adapter 为主，禁止自动下载权重、上传图片或调用真实视觉服务。

### 必须通过的离线测试

1. 三能力解析与 Skill 路由；旧兼容命令仍在，旧文本索引入口不恢复。
2. ingestion 与 ingest 等价，横/竖/方图及 EXIF 方向后的比例正确；
   重复扫描、模型依赖缺失、预览修复都不触发推理。
3. v1–v6 到 v7 的升级、完整备份、指定表删除、在用行/BLOB 保留、失败回滚和外键约束。
4. 无分析记录的图片可 index；模型 A/B 共存，A→B→A 按当前输入正确复用。
5. 相同照片输入及配置重复执行不新增编码；图片更新只使对应旧结果过期。
6. 原图离线、预览损坏、执行期间版本变化、输入照片误关联均按合同处理。
7. 未确认零推理；范围或配置变化需要新摘要；缓存全部命中不加载模型。
8. 中断/并发/写入失败后的恢复，不重复成功结果、不抢占活跃任务。
9. 模型 adapter 的图片/文字配套、处理器参数、维度、归一化、
   超长查询错误、本地文件加载和缺模型/坏权重失败路径。
10. metadata 搜索的中文、大小写、组合 Unicode、字面通配字符、分页和作用域。
11. 语义搜索的精确排序、同分稳定性、空索引、部分覆盖、不同配置隔离；
    只编码查询，不生成图片向量。
12. HTML/JSON 快照不依赖描述字段，转义和路径保护，变更预览不冒充旧图；
    旧快照选片不回归，旧分析记录和任务历史不因移除运行接口而丢失。
13. 文档本地链接、示例参数解析、Skill 恰好三个能力以及退休模块引用检查；
    用现有 unittest 承载合同测试，不额外安装校验工具。

按修改范围合并同 runner 的针对性测试。v7 修改会影响多个现有测试中的
当前版本断言：更新预期升级目标，但保留旧 fixture 和旧备份版本断言。
已有“index 尚不存在”的清理测试调整为新入口合同，而不是删除兼容性验证。

### 真实模型验收：单独授权执行

代码审批不等于下载模型或处理正式图库授权。实现离线测试完成后：

- 经明确授权下载固定权重，验证真正的断网运行与安装恢复。
- 从一张非敏感测试图验证真实编码，再用用户选定的代表性图库样本进行检索。
- 人工标注中英文配对查询，覆盖主体/场景/色彩/构图/组合和无匹配情况；
  报告 top-K 命中率、Recall@K、各语言差异与失败例子。
- 记录实际冷启动、连续图片编码、查询编码、排序和内存；不预填速度承诺。
- 模拟测试通过不能替代质量验收；报告结果后再决定整库运行，
  以及是否提前推进 NaFlex 或运行优化。

## 11. 实施 Todo 与依赖

计划批准后才开始以下工作：

1. **固定三能力与数据合同**：配置/向量/结果/快照接口、命令参数及回归 fixture。
2. **实现 v7 存储**：迁移、旧表删除、图片索引历史、任务状态、配置和占用；依赖 1。
3. **实现 SigLIP 224 CPU 适配**：锁定依赖/文件清单、setup、图片与文字编码；依赖 1。
4. **实现 index 执行闭环**：计划/确认、增量、状态、恢复；依赖 2、3。
5. **实现 management 查看与 metadata 搜索**：仅操作保存数据；依赖 1。
6. **实现 management 语义搜索与报告**：有效候选、配套查询编码、快照；
   依赖 2、3、5。
7. **接线三个能力与 ingestion 摘要**：统一 CLI/Skill 边界、保留兼容；
   依赖 4、5、6。
8. **更新 docs 与 Skill**：清除过期指导、记录三个后续 TODO；依赖 7。
9. **完成针对性离线回归与交付核对**：覆盖所有新增行为及兼容数据；依赖 7、8。

真实模型验收、NaFlex、技术参数、ONNX/量化是有明确边界的后续工作，
不由本计划的代码实施审批自动执行。

## 12. 规划依据与风险

- 官方固定检查点：
  https://huggingface.co/google/siglip2-base-patch16-224/tree/75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2
- 官方预处理：
  https://huggingface.co/google/siglip2-base-patch16-224/blob/75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2/preprocessor_config.json
- 特征提取与 padding 约定：
  https://huggingface.co/docs/transformers/model_doc/siglip

官方 224 方形缩放是用户已知并接受的首版权衡，NaFlex 留为明确 TODO。
模型卡的多语言能力不是本图库质量承诺。原有快速 ingestion 仍基于 size/mtime
决定是否重哈希：未重新入库发现的原图变化不可能由只读缩略图的 index 自动得知，
文档须明确此边界，不把本次重构扩大为全原图监控/强制全量哈希项目。
