# Smart Albums：便携相册与图片语义向量实施计划

状态：新接口与 schema 8 已实现，离线合并回归完成（198 项，197 通过，1 项因 Windows 符号链接权限跳过）。现有 v7 数据库保持不变；新格式的真实照片/模型验收仍需另行授权。

修订：2026-09-06。本版明确 `image_embedding_*` 命名、向量的输入/用途，以及未来技术参数的独立边界。

> 后续合同说明：本文保留 schema 8 便携相册计划及其验收历史，不追改为新功能记录。当前见 [现行设计](index-design.md)：schema 10、37 张注册表（32 普通 + 1 external-content FTS5 + 4 显式影子表，不计内部 `sqlite_sequence`），v1–v9 原样拒绝、不迁移。`virtual_folders`、`virtual_folder_photos`、`album-snapshot-v2` 及原 embedding 六表保留；六项 opt-in feature 属 index，基础三能力不变。第二阶段 OR 搜索/组合排序计划中、尚未实现，不改变现有 metadata/semantic 默认或 embedding-only 展示。此说明不是新模型性能验收记录。

## 1. 已确认目标

1. **一个 album 对应一个 SQLite 文件**，不再有“选择图库后再选择内部相册”。
2. 用户明确选择已有文件或创建新文件；三个能力都操作该文件：
   - `ingestion`：导入照片信息和等比例缩略图。
   - `index`：索引处理入口；首版只建立、复用和更新本地图片语义向量，技术参数以后独立扩展。
   - `management`：相册信息、照片查看、名称检索和语义搜索。
3. SQLite 与照片没有强制目录关系。每张照片保存：
   - 原图绝对路径；
   - 相对于 **SQLite 文件所在目录** 的路径，可含 `..`。
4. 绝对路径优先；绝对路径找不到且相对路径能找到时，使用相对路径定位的文件并更新绝对路径。
5. 两条路径都找不到才标记原图丢失。Windows 跨盘/不同 UNC share 无法生成相对路径时存 `NULL`，明确提示迁移限制。
6. 路径修复不改变照片 ID、内容版本或已有图片索引。图片内容/预览变化后，旧输入索引不再参与当前查询，但保留向量历史。
7. 模型缓存和 Python 环境留在机器本地，与相册文件分离。
8. 单用户、同一时刻一个设备写入；停止操作后移动或同步文件，不实现分布式写入。
9. 项目尚未发布，**不保留旧格式、旧 CLI 参数或旧多相册 API 的兼容层**。
10. 保留导入后的 index 邀请：说明没有索引/有索引各能做什么，询问是否准备索引，不自动执行。
11. 当前存储产物明确为：**从已入库缩略图生成、代表整张图片、用于多语言图文语义检索的图片向量**。
    六张相关表统一使用 `image_embedding_*`，不把模型名写入表名。
12. 技术参数不写入图片向量表，必须有独立的配置、输入身份、结果及状态；
    本轮不实现技术参数算法，也不提前创建 technical 空表。

不在本轮扩展：NaFlex、技术参数、ONNX/量化、跨相册搜索、自动去重、以图搜图、相册间共享向量、自动云同步。

## 2. 当前实现需要改变的地方

- `Config.state_dir` 同时承载数据库和模型，数据库固定为 `photography.db`。
- `SQLiteStorage` 会自动创建目录/数据库，并执行旧版本迁移；不能用于明确的“只读打开”。
- `libraries`、`albums`、`album_photos` 及相关 CLI 把一个数据库当作多相册容器。
- 当前 `relative_path` 是相对于扫描根目录，不是相对于 SQLite。
- ingest 依赖绝对扫描根目录身份；迁移后扫描新路径可能产生另一组照片记录。
- `exports.py` 和导入入口禁止整个状态目录与原图目录重叠，阻碍 SQLite 与照片任意放置。
- 模型目录、执行锁、查询筛选和报告均间接依赖 state directory / 内部 album ID。
- 当前 `image_index_*` 表实际只保存 embedding 的配置、向量和执行状态，名称过于泛化；
  新格式应直接创建语义明确的 `image_embedding_*` 表，而不是保留两套表或兼容视图。

已存在但未提交的 `index_prompt` 及 Skill 入口说明属于本计划基线，实施时保留并适配，不撤回。

## 3. 明确的用户入口与 CLI

统一要求显式 `--database <absolute-sqlite-file>`，接受 `.sqlite`、`.sqlite3`、`.db`，
推荐 `.sqlite`。不设置隐藏的默认数据库，不根据当前目录、环境变量或文件名猜测用户选择。
`--help` 和说明性对话不要求选择数据库。
`--model-cache-dir` 是可选的统一全局参数，位于能力名之前；它不选择或创建相册。

### 3.1 相册生命周期

```text
python <skill>\scripts\photography.py --database <file.sqlite> management create
python <skill>\scripts\photography.py --database <file.sqlite> management open
```

- `create`：仅在用户明确选择“创建”后使用，目标必须不存在，不能覆盖文件。
- `open`：只读验证文件和格式，返回相册身份、文件路径、照片数量及索引概况；不创建、不迁移、不保持后台连接。
- 相册显示名初版取文件名去掉扩展名；SQLite 内保存稳定 UUID，文件移动/改名不产生新身份。
- “创建相册”是 management 的明确生命周期写操作，不是额外的第四个 Skill。
- 只读浏览/search 不会顺带创建相册、改变默认 profile 或修复原图路径。

### 3.2 导入与索引

```text
... --database <file.sqlite> ingestion <absolute-photo-directory>
... --database <file.sqlite> [--model-cache-dir <local-cache-root>] index setup
... --database <file.sqlite> index profiles
... --database <file.sqlite> index configure --default-profile <id>
... --database <file.sqlite> index plan --all [--profile-id <id>] [--limit N]
... --database <file.sqlite> index plan --ids-file <json> [--profile-id <id>]
... --database <file.sqlite> index execute <run-id> --confirm <digest>
... --database <file.sqlite> index status [--profile-id <id>]
... --database <file.sqlite> index job <run-id>
... --database <file.sqlite> index resume <run-id>
```

- 取消 `--album-id`、`--library-id`、`--album-name`；当前文件就是当前相册。
- index 必须显式 `--all` 或提供确切 IDs，选择文件本身不等于授权处理全相册。
- 保留分页、状态过滤、dry-run、模型配置隔离、精确方案确认及缓存复用。
- 配套模型缓存覆盖参数在 index 和 semantic search 中一致；缓存位置不影响 profile ID。
- 用户仍调用 `index`，不增加第四个 `image_embedding` Skill 能力。
  首版计划、状态及执行输出带 `component: image_embedding`，不能把图片向量 ready 描述成技术参数也已完成。
  组件标识与照片范围、输入身份和 profile 一起进入不可变确认摘要。

### 3.3 查看、搜索和原图定位

```text
... --database <file.sqlite> management photos [--limit N] [--after <id>]
... --database <file.sqlite> management photo <photo-id>
... --database <file.sqlite> management search "<query>" --mode metadata
... --database <file.sqlite> management search "<query>" --mode semantic [--profile-id <id>]
... --database <file.sqlite> management original <photo-id>
... --database <file.sqlite> management relink <photo-id> --path <absolute-photo-file>
... --database <file.sqlite> management backup --output <new-backup.sqlite>
```

- metadata 只查当前相册中的文件名/记录路径；取消 `--target albums`。
- semantic 查询当前文件中的有效向量，不跨文件，不自动生成缺失索引。
- `photo` 默认查看保存元数据/缩略图，不访问原图。
- `original` 执行双路径定位，可能写入绝对路径修复；返回路径和修复结果，由宿主按请求展示原图。
- `relink` 供两条路径都失效或跨盘迁移后使用：用户明确提供新位置。
  校验内容哈希与已入库版本一致后更新两条路径；不一致时报错，不提供强制覆盖绑定。
  无路径线索且内容也变化的文件只能经用户选择作为新照片导入，首版不猜测这种身份合并。
- `backup` 使用 SQLite 一致性备份，不复制正在写入的单个裸 `.sqlite` 文件；目标不得覆盖已有文件。
- 返回值统一带 `album: {id, name, database_path}`，让用户和 agent 能确认本次操作的对象。

删除旧顶层兼容命令/别名：`ingest`、`libraries`、`albums`、`album-*`、
`photos/photo/thumbnail`、`scan/scan-events`、`search-add`。
仍有用的缩略图导出和扫描诊断放入 management 子命令，不能借清理丢失诊断能力。

## 4. 创建、打开与新格式边界

### 数据库配置

`Config` 分离为数据库路径及机器本地缓存配置：

- `database_path`：用户本次明确选择的完整路径。
- `model_cache_root`：本机可配置路径，默认位于本机应用缓存目录；不写成相册内容身份。
- 不再存在把数据库、模型和报告一并绑定的 `state_dir`。

### Storage 接口

- `SQLiteStorage.create(database_path)`：排他创建新文件并事务初始化。
- `SQLiteStorage.open(database_path, writable=False)`：只打开已存在的受支持文件，
  分别使用 SQLite URI `mode=ro` / `mode=rw`，不得使用自动创建模式。
- 打开不执行 `CREATE TABLE IF NOT EXISTS`、自动修复或旧库迁移。
- 只在 create 时写入应用格式标识 `PRAGMA application_id=0x53414C42`、
  schema version 8 和相册 UUID；open 验证标识、版本、单行相册元数据及必需表/列。
- 文件不存在、非本应用 SQLite、旧格式、损坏文件、只读写入失败分别返回明确错误。
- 不把旧 v1-v7 文件清空、重新初始化或当作新格式打开；保留文件原样，提示需创建新相册。

创建必须排他，避免“检查不存在后别人创建”的竞态。实现采用私有临时文件完成初始化，
再以不覆盖已有目标的方式发布；备份采用同样的边界。Windows 使用不替换目标的 rename，
POSIX 使用排他的链接发布，不能退回会覆盖目标的普通 replace。失败只清理本次私有文件，
不移除预先存在或竞态出现的目标文件。open 仍明确拒绝不完整文件，不能默默覆盖。

删除旧版本迁移器、废弃表清理器和旧 schema fixture 兼容要求。
这不等于可以删除现有用户数据：当前测试库及其备份默认不动。

## 5. 新数据库结构：11 张在用表

| 表 | 内容 |
| --- | --- |
| `album_metadata` | 严格单行的相册 UUID、创建时间等；文件路径/显示名从实际打开位置取得 |
| `photos` | 照片身份、双路径、已入库内容版本、文件属性、元数据、输入状态及路径检查结果 |
| `thumbnails` | 每张照片的当前预览 BLOB、尺寸、配置、内容哈希 |
| `scans` | 每次导入的源目录及范围快照、状态和结果 |
| `scan_events` | 逐文件导入结果和错误 |
| `image_embedding_profiles` | 不可变的图片语义向量配置，包括配套文本编码规则 |
| `image_embedding_results` | 各照片输入版本/模型的图片语义向量历史 |
| `image_embedding_runs` | 图片向量生成的确认方案及执行记录 |
| `image_embedding_items` | 逐照片 embedding 状态、结果和尝试 |
| `image_embedding_claims` | 图片向量任务占用，避免重复计算 |
| `image_embedding_settings` | 当前相册显式选择的默认图片语义检索配置 |

删除 `libraries`、`albums`、`album_photos`，不增加另一张多相册容器表。
允许一个相册多次从不同目录导入；源目录是 scan 的信息，不是用户要另选的资源。

### 5.1 命名映射与组件边界

| 当前旧名称 | 新格式名称 |
| --- | --- |
| `image_index_profiles` | `image_embedding_profiles` |
| `image_index_results` | `image_embedding_results` |
| `image_index_runs` | `image_embedding_runs` |
| `image_index_items` | `image_embedding_items` |
| `image_index_claims` | `image_embedding_claims` |
| `image_index_settings` | `image_embedding_settings` |

这是新格式的建表合同，不是对现有 v7 库执行 ALTER/复制的授权。
新库不再创建 `image_index_*`、泛化的 `embedding_*`、旧文本 `photo_embeddings`
或 technical 占位表。约束、索引、触发器和内部存储方法一并采用图片 embedding 语义。

`index` 是可扩展的处理流程；`image_embedding_*` 只服务于其中的图片向量子流程。
不要把未来技术参数结果或失败记录塞进这些表，或把一个多态 result ID 用字符串约定
指向不同结果表而丢掉外键约束。

### 5.2 明确“什么的 embedding”

`image_embedding_profiles` 的版本化 JSON 至少说明：

| 信息 | 首版固定值/含义 |
| --- | --- |
| `profile_schema` | `image-embedding-profile-v1` |
| `embedding_kind` | `image_text_semantic`，图文对齐的语义空间 |
| `stored_modality` | `image`，持久化结果属于照片侧，不是查询文字 |
| `input_scope` | `stored_thumbnail`，输入来自 SQLite 保存的 JPEG 预览 |
| `granularity` | `whole_image`，整张图片，不是人脸框、物体框或区域 |
| 模型身份 | SigLIP 2 Base 224、固定权重 revision/文件摘要 |
| 图片处理 | 官方 224×224 预处理、特征提取、池化和归一化规则 |
| 配套查询处理 | 同一检查点的文本编码器、tokenizer、模板、长度和 padding 规则 |
| 输出与运行 | 768 维、float32 小端、L2 归一化、CPU 运行时及依赖身份 |

表名不包含 SigLIP、模型版本或向量维数；换模型只增加配置和结果，不创建模型专属表。
上述语义字段和编码参数纳入配置指纹，不能只靠模型名称或维度判断可复用。
新 profile 合同扩展可能产生不同于 v7 的 ID；不为兼容旧 ID 省略真实配置。
缓存路径本身仍不参与指纹，权重文件不因字段命名变化而自动重新下载。

`image_embedding_results` 至少保存：

```text
result_id
photo_id
profile_id
content_version
thumbnail_profile
input_image_hash
dimensions / dtype / normalized
vector / vector_hash
created_at
```

结果关联严格的配置及输入身份即可，不重复维护另一份可能分歧的用途说明。
配套文本编码器将用户查询映射到相同空间；查询向量只在本次搜索中使用，
不写入 `image_embedding_results`，也不新增持久化查询向量表。
不得恢复“先生成文字描述再编码”的流程。

### 5.3 照片字段合同

照片路径必须是可查询的明确列，不只藏在 JSON 中：

```text
photo_id
original_absolute_path
original_relative_path        nullable，相对于实际 SQLite 文件所在目录
content_version              成功入库内容的 SHA-256
thumbnail_profile            当前预览的处理配置
size_bytes / mtime_ns
metadata_json                尺寸、格式、EXIF 等，不重复存两条路径
ingest_state / last_ingest_error
original_status / last_original_check / last_path_error
created_at / updated_at / path_updated_at
```

来源可用性与预览/索引有效性分离。原图 missing/unavailable 不删除照片和有效保存预览；
图片内容成功重新导入后才更新内容版本及缩略图，旧输入向量失效但仍留作历史。

路径不是照片 ID，也不属于 embedding profile 或缓存内容键。
图片语义向量仍按照片 ID、内容版本、实际预览哈希、预览配置和模型 profile 区分，
由 `image_embedding_results` 的唯一键和外键保护。
将相册 UUID 纳入新方案/快照身份，防止在另一文件里执行错误上下文；
不把可移动的数据库绝对路径放入向量身份或固定方案摘要。

## 6. 双路径定位合同

新增唯一的 `source_paths.py`，由 ingestion 和显式原图操作复用；
不在各个命令中分别拼路径或各自实现 fallback。

| 情况 | 行为 |
| --- | --- |
| 绝对路径找到原图文件 | 使用它，不改为相对路径指向的另一个文件 |
| 绝对路径缺失，相对路径存在且找到文件 | 使用相对定位结果，更新绝对路径和路径更新时间 |
| 相对路径为空，绝对路径缺失 | 原图丢失；提示原先跨盘或缺少备用位置 |
| 两条路径都不存在 | 原图丢失，不删除记录、缩略图或索引 |
| 访问被拒、I/O 故障等 | 返回访问错误，不冒充“找不到”或盲目切换到另一文件 |
| 备用定位成功但数据库不可写 | 明确报告路径可用但修复未保存；不标记原图丢失、不假装已更新 |

实现约定：

- 相对路径统一保存为可跨平台解释的分段路径，解析时以当前数据库父目录为基准，
  不是 CWD、扫描根目录、模型目录或旧数据库位置。
- 允许 `..`。不同盘符/UNC share 的相对路径计算失败只产生 `NULL` 和可见警告，
  不阻止合法的绝对路径导入。
- 外来平台格式的绝对路径不能被误当作当前 CWD 下的相对路径。
  当前平台无法解释的绝对地址记录原因，并尝试有效的备用相对地址。
- 普通绝对路径命中不重写已有相对路径，避免数据库复制后把备用路径悄悄改回旧位置。
  相对路径在新照片录入、明确的重新定位或已确认的导入更新中重新计算。
- 定位和内容校验是两回事：定位返回 `content_verified: false`；
  文件存在不证明内容仍与已保存版本相同，内容版本更新由 ingestion 完成。
- 绝对路径优先也适用于两个位置都有文件的情况；不能凭相似文件名或不同内容哈希
  擅自选择相对路径。发现冲突需报告，不能偷偷合并两张记录。
- 修复在短事务内比较原路径/内容版本快照，防止覆盖刚发生的导入更新；
  只改路径相关字段，不改向量、预览或照片 ID。

`management original`/`relink` 是明确的元数据维护例外，需要可写访问；
普通 `open`、照片列表、缩略图和 search 仍为真正的只读操作。
不在 search 里遍历原图或写入路径修复。

## 7. Ingestion：移动后不重复入库

移除 `library_id + 根目录绝对路径` 身份规则。导入必须在当前相册内匹配现有记录：

1. 从一致读取快照建立现有照片的路径候选映射，释放快照后才做原图 I/O；
   写入时在短事务内复核路径匹配、版本及冲突，防止两个导入同时创建重复记录。
2. 优先匹配已记录、可解释的规范化绝对路径。
3. 绝对地址不可用时，用数据库相对路径定位及匹配扫描到的文件，沿用原 `photo_id`。
4. 仅路径变化且内容/预览相同：修复位置，复用缩略图和索引。
5. 内容变化：沿用照片 ID，更新哈希、元数据和预览，旧索引变为 stale。
6. 没有明确路径匹配才创建新照片记录。不要只靠相同内容哈希合并不同文件。
7. 多条记录命中同一路径，或绝对优先规则与扫描候选冲突时，报告路径冲突；
   不猜测应覆盖/合并哪一条。

范围规则：

- 只更新本次明确扫描范围内的照片；从另一个目录导入，不把相册其他照片标为 missing。
- 完整扫描后，对本范围中缺席的已知照片检查两条位置；备用路径仍能找到时不得标丢失。
- 不完整扫描不推断缺失。
- 保留现有格式支持、EXIF 方向、sRGB、等比例预览和逐文件错误隔离。
- SQLite 可与原图处于同一目录、其父目录、子目录或不同盘符。
  取消整个目录重叠禁令，但明确排除所选数据库、事务/锁 sidecar、缓存和输出产物；
  不能写入/覆盖原图，也不能把生成的预览再次当原图导入。

导入后保留 `index_prompt`：提供确切尚需索引的成功照片 ID、
无需索引能做什么、有图片语义向量后能做什么及确认邀请。
新默认范围是当前相册文件，不再提示内部相册 ID；不自动下载、建计划或执行模型。
说明中不得把建立图片向量描述成已经检测对焦、模糊或其他技术参数。

## 8. 本机模型缓存与 image_embedding 子流程

- 把 `default_model_dir(state_dir)` 改为由独立本机缓存配置解析。
- 默认使用操作系统应用缓存目录，例如 Windows LocalAppData 下的 Smart Albums/models；
  支持统一 `--model-cache-dir` 覆盖。
- `index setup` 在本机缓存安装/校验权重，并登记到当前相册；不自动选择默认 profile。
- 多个相册使用同一套模型文件，不重复下载；profile 和默认选择仍按相册保存。
- setup/configure/plan/execute/status/job/resume 当前全部针对 `image_embedding`；
  默认配置存 `image_embedding_settings`，不是未来所有 index 组件共用的“默认模型”。
- 不改变固定 SigLIP 224 权重、参数、维度或归一化规则，不借这次重构改搜索算法。
- 沿用短 generation 目录、原子发布和校验，避免重新引入 Windows MAX_PATH 问题。
- index 只读取当前 SQLite 预览。原图路径修复和相册移动不会产生额外图片编码。
- 旧模型文件可在显式指定缓存根目录时复用；不自动移动/删除当前安装或重新下载。

## 9. 技术参数扩展边界：以后实现，不预建表

未来的对焦/模糊、亮度或其他技术指标属于 `index` 下的另一个组件，
不是图片语义 embedding 的额外列，也不能从向量维度直接解释出来。

计划中的独立存储方向：

| 未来表 | 职责 |
| --- | --- |
| `technical_profiles` | 技术分析模型/算法、版本、预处理、字段定义、单位及阈值配置 |
| `technical_results` | 关联照片和技术配置的版本化结果、输入范围/哈希、指标及生成时间 |

这些不是首版的第 12、13 张表：本轮只保留 TODO，不创建它们，
也不把空 JSON、空任务或虚构技术结果写进现有表。

将来实施时必须保持：

1. **独立输入**：记录 `preview`、`original` 或 `crop` 及实际输入哈希，
   不能默认沿用图片向量的缩略图范围。读原图的技术任务必须另有明确范围和授权。
2. **独立版本**：更新技术算法不重算 embedding；更换 embedding 模型不重算技术结果。
3. **独立状态与恢复**：技术失败/缺失不把有效图片向量标为失败；
   需要持久化技术任务时再增加对应存储，不借用 `image_embedding_runs/items/claims`。
4. **可复用代码，不混结果**：通用确认、事务、锁和计时逻辑可按实际共同点提取，
   但外键必须指向正确的配置/结果表，不用无约束的通用 JSON 桶替代类型合同。
5. **可组合查询**：语义排序与技术条件可组合，但各自检查输入版本和配置。
   缺少技术结果是未知/未完成，不是“没有模糊”，也不能自动补跑。
6. **判断边界**：缩略图清晰度不等于原图对焦质量；失焦、运动模糊及普通模糊
   不能未经验证就用一个分数混为一谈。指标需要算法定义和适用范围，不默认是校准概率。
7. **明确授权**：未来增加技术步骤时，计划和摘要必须列出具体组件及其照片范围，
   不能把已有的 embedding 执行批准自动扩大为原图技术分析。

首版状态只报告 `component: image_embedding`；它的 ready 表示图片语义向量可用，
不代表整套未来 index 组件都已完成。技术参数的实际字段、算法、单位和查询语法另行确定。

## 10. 文件可携带性、锁和备份

- 所有命令对实际选择的完整数据库路径操作；规范化后回显，不推测隐藏状态目录。
- 图片向量执行锁使用完整文件名及组件派生，例如 `travel.sqlite.image-embedding.lock`，
  避免 `travel.sqlite` 与 `travel.db` 因相同 stem 错用同一锁。
- 新建数据库初版采用 rollback journal，不默认启用 WAL。
  正常命令结束时关闭连接；写入期间的 journal/lock 文件不是可随意删除的垃圾。
- 创建、路径修复、ingestion 和 index 的写操作均遵守 SQLite 事务；
  不跨推理或长时间原图读取持有写事务。
- 本机锁只能防止本机/同一文件上的并发。跨设备继续未完成任务前，须确认原设备已停止；
  不因本地看不到旧 PID 就认定远端工作已结束，不实现分布式锁或自动抢占。
- 云端按“下载到本地完整副本 → 操作 → 关闭 → 同步回去”使用。
  首版不把 HTTP/S3 URL 当数据库路径，也不承诺网络文件系统上的并发安全。
- `management backup` 创建完整一致性备份，包含预览和索引，不包含外部原图或模型权重。
  备份保持原数据内容；恢复时仍按双路径规则定位，不宣称仅搬一个 SQLite 就能找到任意位置的原图。
- 只有绝对位置仍有效或相对布局仍可解析时，原图才能自动找到；否则使用明确的 relink。
- 修改导出保护：不能再禁止写整个数据库父目录，但必须保护数据库、sidecar、缓存和已登记原图；
  新生成 JPEG 不能污染本次源目录扫描范围。

## 11. 代码与文档交付

| 部分 | 修改 |
| --- | --- |
| `config.py` | 显式数据库路径与独立缓存配置，无隐藏数据库默认值 |
| `sqlite_storage.py`、`storage.py` | create/open 分离，新单相册 schema，只读连接，删除旧迁移及多相册方法 |
| 新 `source_paths.py` | 双路径计算、定位、错误区分、修复与冲突保护 |
| `ingest.py`、`images.py` | 适配新照片字段，去掉根库/成员关系，正确匹配和扫描；保留图像处理算法及 index 邀请 |
| `index_storage.py` → `image_embedding_storage.py` | 六张表、约束/触发器、存取方法及常量统一图片向量命名 |
| `indexing.py` → `image_embedding.py` | 图片向量专用的计划、确认、状态、恢复；相册 UUID 和输入身份保护 |
| `index_profiles.py` → `image_embedding_profiles.py` | 显式用途/模态/粒度/输入范围、新 profile 合同和独立缓存配置 |
| `siglip_embedding.py`、`image_vectors.py` | 保留配套图文编码、固定权重及向量校验；不生成技术参数 |
| `index_lock.py`、`exports.py` | 完整文件名/组件锁、便携目录和写入目标保护 |
| `cli.py`、`index_cli.py`、`management_cli.py` | 新参数和生命周期/定位入口；删旧别名/内部相册筛选 |
| `management.py`、`management_report.py` | 当前相册信息、照片/搜索；双路径展示和未检查标记；新快照格式 |
| `search.py`、`search_report.py` | 删除旧跨内部相册选片兼容实现；已有静态报告文件不受影响 |
| `SKILL.md`、references、README | 一步选/建相册文件，清除“尚未实现”说明及过时命令 |
| tests | 新格式/路径/隔离/创建/只读/移动验收，删除不再要求的旧格式迁移与兼容测试 |

用户入口 `index_cli.py` 只负责调度当前图片向量组件；不保留旧模块导入兼容层，
不为尚未实现的 technical 组件创建空编排框架。
`INDEX_SCHEMA/INDEX_TABLES`、存储类和内部方法采用 `IMAGE_EMBEDDING_*` /
`ImageEmbeddingStorage` 等具体命名；管理查询和报告引用新的表和服务。
缓存路径不是模型身份，但新增 profile 语义字段是身份的一部分，测试不可混淆二者。

Skill 必须明确：

1. 先选/建相册文件，已明确选择则不重复询问。
2. 每次说明正在使用哪个文件；换文件后清空旧照片、profile、run 和确认上下文。
3. 创建与打开分开，不把找不到文件当成新建授权。
4. 创建文件、路径修复、模型安装和索引执行是不同操作，不互相隐式授权。
5. 导入后解释无 index 可浏览/按名称查找，有 index 才能按中英文画面语义搜候选。
6. 相对路径缺失的跨盘警告、只读修复失败、原图丢失与保存预览仍可用都要如实说明。
7. 当前 `index` 只生成缩略图的整图语义 embedding，用于中英文文字找图；
   不暗示它已计算对焦、模糊或其他技术参数，也不承诺精确目标检测。

文档同步更新 `references/library.md`、ingest/index/management 文档和 index 设计。
历史 v7 实施计划标记为已被新方案取代，不把旧迁移/多相册设计当成当前操作说明。
更新所有 schema 图、命令输出和测试中的目标名称；旧名称只在明确的历史说明中出现。
TODO 保留 NaFlex、technical 技术参数、ONNX/量化，技术参数不混进 `image_embedding_*`。
本机性能/私有照片报告不进入公开文档。

## 12. 测试与验收

使用已有 unittest、临时目录和合成照片；绝大部分验证不需要模型下载/真实推理。

### 文件生命周期

- 未传 database、文件不存在、错误类型、损坏文件、旧 v7 或其他 SQLite 格式：
  明确失败，无新文件、无 DDL、无自动迁移。
- 创建已有文件拒绝；并发创建至多一个成功；初始化失败不损坏预先存在文件。
- 新库严格为 11 张在用表和一个相册元数据记录；有六张 `image_embedding_*`，
  没有 libraries/albums/album_photos、旧 analysis/image_index 表或 technical 占位表。
- 两个同目录、不同文件名的相册彼此隔离，输出回显正确路径和 UUID。
- 只读挂载可打开、浏览、metadata/semantic 搜索；需要写入时给出明确错误。

### 原图双路径

- 绝对有效/相对无效：使用绝对位置。
- 两条都有效且指向不同文件：绝对优先，不偷偷切换。
- 绝对缺失/相对有效：定位成功，绝对路径修复持久化，照片 ID/内容版本/向量不变。
- 两条都不存在：missing；仅权限/I/O 失败不能记为 missing。
- Windows 跨盘与跨 share：相对路径 NULL、警告明确，绝对路径仍可用。
- 相对路径以 SQLite 父目录为基准，换 CWD 不改变解析结果；支持 `..`、空格及 Unicode。
- 外来系统绝对路径不能变成当前 CWD 下的意外路径。
- 只读数据库备用路径可定位但修复不可写：明确报告未保存，不误报丢失。
- relink 内容哈希一致才修复；内容不一致不得悄悄绑定另一张图片。

### 移动、重扫及索引

- 在新格式临时相册上移动 SQLite 与照片、移除旧路径后：
  相对定位修复绝对地址，重扫不新增重复 photo ID、不重新生成相同向量。
- 仅移动数据库但原绝对路径仍有效：仍使用绝对路径；不改旧向量或猜测新的照片位置。
- 两条地址都失败后重新提供位置，可恢复原 ID；普通重命名/改布局不冒充已实现自动追踪。
- 同相册从多个目录/重叠目录导入，不重复同一可明确定位的文件，也不把其他目录照片标丢失。
- 文件版本变化仍使旧输入向量过期；模型 A/B 结果保留，换相册不复用另一文件的行或审批。
- 原图离线时，保存预览/索引的浏览与检索仍可用，零原图 stat 和零路径写入。
- 缓存目录改变不改变 profile ID；创建新相册不加载模型或下载权重。
- 方案确认、缓存复用、失败恢复、路径冲突和保存前版本检查保持正确。
- 备份在有写入时仍一致；模拟失败和重开不丢已提交记录。
- README/Skill 的所有示例参数、链接和三个能力路由与实际 CLI 一致。

### 图片语义向量合同与技术边界

- profile 明确存储模态 image、用途 image_text_semantic、stored_thumbnail 输入、
  whole_image 粒度及配套文本编码配置；必要身份字段缺失/不匹配时不默默采用默认值。
- 图片索引只调用图片编码入口，搜索只调用配套文本编码入口；搜索不会往图片结果表写查询向量。
- 图片和文本使用相同 profile/空间校验，维度相同但身份不同的向量不能混用。
- 配置语义/模型/预处理变更形成不同身份；只移动缓存路径不改变身份。
- `image_embedding_results` 的照片、配置和输入关联有正确外键及唯一约束；
  runs/items/claims 不接受 technical 结果或任务冒充。
- 状态/报告明确是 image_embedding 覆盖率；没有技术参数时不声称技术检查完成。
- 所有命令都不会隐式执行技术分析；本轮新库没有技术算法、结果或任务占位记录。

## 13. 建议实施顺序

1. **冻结新合同与测试骨架**：命令、11 表 schema、image_embedding 语义配置、路径状态和错误码。
2. **实现文件生命周期与新存储**：排他 create、只读/rw open、格式校验、单行相册元数据和六张图片向量表。
3. **实现路径解析与 ingestion**：两条地址、修复、扫描匹配、范围保护和 index 邀请。
4. **适配图片语义 embedding 与共享缓存**：重命名表/服务并贯通查询；加入语义身份、组件状态和正确锁路径。
5. **适配 management、备份及报告**：当前文件视图/搜索、original/relink、只读和维护操作边界。
6. **更新 Skill 与全部文档，删除旧兼容实现和测试**。
7. **合并针对性回归和端到端便携测试**；真实照片验证另行指定新文件与范围。

完成条件：用户能创建/选择任意受支持名称的 SQLite 相册文件，导入、图片语义索引和查询
始终指向它；移动后在路径规则允许的情况下修复位置并复用索引，不会创建错误数据库、
混淆内部相册或悄悄重建用户数据。图片向量的用途、输入和粒度明确，
技术参数保持独立的未来扩展，不污染本轮表结构、任务状态或授权范围。

## 14. 对现有运行数据的处理

- 不自动迁移、不清空、不覆盖当前 v7 测试库及其备份。
- 开发/回归使用全新的临时新格式数据库。
- 若需要重建真实测试相册，用户另行确认一个新目标文件；可复用本机已下载模型，
  但真实导入/索引的范围和执行仍需明确授权。
- 本轮实现和验证只使用临时新格式数据库、合成图片及测试编码器；
  不修改现有 v7 数据库、不搬运用户原图、不下载或执行真实模型。
