# video-search 项目交接

更新时间：2026-09-09（Asia/Shanghai）

## 1. 项目目标

为本机一个或多个指定文件夹中的视频素材建立可搜索索引。用户输入自然语言，例如“白天在草坪上挥手的新娘”，系统返回所有符合条件的镜头，并明确给出：

- 源视频绝对路径；
- 镜头编号；
- `start_ms / end_ms`；
- 便于人阅读的开始/结束秒数；
- 缩略图、内容摘要和结构化属性；
- 能从准确时间开始播放的源视频。

素材只在本机读取，不复制或改写源视频。搜索网页默认只在本机运行；如需临时公网访问，可用 Cloudflare Quick Tunnel 暴露本地端口，但 Quick Tunnel URL 是临时的，不能当成稳定部署地址。

## 2. 已确认的产品决策

### 2.1 镜头是基本检索单元

先用 FFmpeg 对每个视频做镜头分段，再逐镜头分析。视频级描述太粗，无法返回准确的命中时间范围；逐帧分析又会产生大量重复结果，因此数据库的主要实体是 `shot`。

### 2.2 两套时间语义必须分开

- `start_ms / end_ms`：镜头在源视频中的物理时间范围，用于定位和播放。
- `when.period`：画面内容发生的时段，例如清晨、白天、傍晚、蓝调时刻、夜晚；它不是“视频第几秒”。

### 2.3 `who / where / when` 及相关属性使用自由文本

目前不维护枚举表。以下字段都允许模型返回任意合理文本：

- `who.role`
- `where.environment`
- `where.venue`
- `where.background`
- `when.period`
- `when.lighting`
- `camera.*`
- 事件动作和物体名称

数据库中常用筛选字段单独成列，完整分析仍保存在原始 JSON 中。这样既能筛选，也不会因枚举设计过早而丢失细节。

### 2.4 动态内容优先于静态标签

Mage-VL 的提示词必须重点描述：

- 人物动作；
- 人物之间的互动；
- 动作先后顺序；
- 涉及的物体和目标；
- 摄影机运动。

仅输出“新人、草坪、白天”不足以支持“新娘走向新郎后拥抱”这类搜索。`events` 必须按 `sequence = 0, 1, 2...` 连续保存。

### 2.5 多帧负责理解，单帧负责展示和视觉检索

- 每个镜头均匀抽取多帧交给 Mage-VL，用于理解动作和变化。
- 另取一张清晰代表帧作为缩略图。
- 缩略图本身不一定需要 embedding；只有启用视觉语义搜索时，才对代表帧生成视觉 embedding。
- 文本 embedding 作用于结构化分析拼成的 `search_text`。
- 视觉 embedding 让文字查询可以直接匹配画面，即使 Mage-VL 描述漏掉某个颜色、构图或物体。

### 2.6 不长期保存镜头子片段

当前推荐的 Mage 服务流程直接按镜头的 `start_ms/end_ms` 从源视频抽取缩放 JPEG，不会完整转码镜头；分析完成后抽帧临时目录自动删除。数据库只保存源视频路径、时间范围、分析结果和代表帧。自定义命令适配器为了兼容外部视频运行器，仍会提供一个临时 H.264 镜头。

技术上 Mage-VL/OptiQ 不要求保存子片段；只要服务能收到该镜头的多张帧即可。直接抽帧可以：

- 明确限制分析范围；
- 统一处理 H.264、HEVC 等源格式；
- 避免完整镜头转码和完整计帧解码；
- 降低临时空间及 SSD 写入；
- 失败后不遗留大量片段文件。

## 3. 当前处理流程

```text
指定文件夹
  -> 递归发现视频
  -> FFprobe 获取时长/尺寸
  -> FFmpeg scene detection 生成镜头边界，并限制最长 30 秒
  -> 直接按源时间范围抽取 8/16/24 帧
  -> Mage-VL 返回结构化 JSON
  -> 保存 JSON + 规范化字段
  -> 复用中间样本作缩略图
  -> 可选：文本 embedding
  -> 可选：代表帧视觉 embedding
  -> 混合检索
  -> 返回源文件与准确时间范围
```

当前抽帧策略：

- 镜头不超过 8 秒：8 帧；
- 8–20 秒：16 帧；
- 超过 20 秒：24 帧。

Mage-VL 官方示例默认最多使用 32 帧。当前策略是首版成本和动态覆盖之间的折中，可以在真实数据评测后调整。

## 4. 镜头分段研究结论

FFmpeg 可以用场景变化分数识别硬切和明显转场，但它不是语义聚类器，也不能保证把“内容相近的连续帧”完美分成同一镜头。

当前实现：

- `scene` 阈值：`0.32`；
- 最短镜头：`500ms`；
- 分段版本：`ffmpeg-scene-v1`；
- 输出连续、不重叠的 `[start_ms, end_ms)` 范围。

在婚礼素材中，应重点关注：闪光灯、快速摇镜、遮挡、叠化、慢动作和曝光变化可能造成误切或漏切。首版先使用 FFmpeg；如果真实素材误切明显，再考虑 PySceneDetect 的 ContentDetector/AdaptiveDetector 或增加相邻短镜头合并规则。

Mage-VL 可以理解一个已经截好的镜头，但不建议让它承担整段视频的精确镜头边界检测。镜头检测和内容理解保持为两个独立阶段。

## 5. Mage-VL 输出结构

提示词位于 `prompts/shot-analysis-v1.txt`。核心结构：

```json
{
  "summary": "主要人物、场景和动态内容",
  "who": [
    {
      "role": "新娘",
      "count": 1,
      "appearance": "白色婚纱",
      "confidence": 0.95
    }
  ],
  "where": {
    "environment": "户外草坪",
    "venue": "庄园婚礼场地",
    "background": "花艺拱门和宾客座椅"
  },
  "when": {
    "period": "傍晚",
    "lighting": "自然逆光"
  },
  "events": [
    {
      "sequence": 0,
      "subject_role": "新娘",
      "action": "走向",
      "object": null,
      "target": "新郎",
      "description": "新娘沿草坪走向新郎",
      "confidence": 0.9
    }
  ],
  "objects": [
    {"name": "捧花", "confidence": 0.9}
  ],
  "camera": {
    "movement": "缓慢跟随",
    "shot_size": "中景",
    "viewpoint": "平视"
  }
}
```

约束：

- 只描述画面可支持的事实；
- 不通过人脸猜真实姓名，只使用新娘、新郎、宾客等视觉角色；
- 不重新判断镜头边界；
- 不让模型猜源视频秒数；
- `summary` 必填；
- `events.sequence` 必须从 0 连续递增；
- `event.action` 和 `event.description` 必填。

## 6. 数据库设计

数据库为本地 SQLite。

### `videos`

保存：

- 源文件绝对路径和文件指纹；
- 视频时长；
- `segmentation_version`；
- `pending / processing / ready / failed` 状态；
- 错误信息和时间戳。

### `shots`

保存：

- `video_id / shot_index`；
- `start_ms / end_ms`；
- `summary / search_text`；
- 可直接筛选的 `when_period / lighting / environment / venue`；
- 完整 `analysis_json`；
- `analysis_version`；
- 状态、错误和时间戳。

### 明细表

- `shot_roles`：角色、数量、置信度；
- `shot_events`：顺序、主体、动作、物体、目标、完整描述、置信度；
- `shot_objects`：物体及置信度；
- `shot_frames`：代表帧时间、路径、用途、质量分；
- `shot_text_vectors`：按 embedding 版本保存镜头文本向量；
- `frame_visual_vectors`：按 embedding 版本保存代表帧视觉向量。

向量首版直接以 little-endian float32 BLOB 存在 SQLite 中。数据量增大到需要 ANN 时，再迁移到专门的向量索引；当前不提前引入复杂基础设施。

### 版本字段的首版简化

研究过程中讨论过以下六个独立版本字段：

- `segmenter_version`
- `mage_model_version`
- `prompt_version`
- `analysis_schema_version`
- `text_embedding_version`
- `visual_embedding_version`

首版没有全部拆成顶层列，避免过度设计。目前实际保存方式是：

- `videos.segmentation_version`：镜头分段算法版本；
- `shots.analysis_version`：模型、提示词和 JSON schema 的组合版本，例如 `mage-vl-optiq-4bit+prompt-v1+schema-v1`；
- `shot_text_vectors.embedding_version`：文本 embedding 版本；
- `frame_visual_vectors.embedding_version`：视觉 embedding 版本；
- `shots.analysis_json`：完整模型原始输出。

当需要独立重跑提示词、比较多个模型或做更严格的数据血缘时，再把组合 `analysis_version` 拆表或拆列。

## 7. 搜索设计

支持自然语言与显式自由值筛选混合使用。

当前可解析的筛选前缀：

- `when:` 或 `period:`
- `lighting:`
- `environment:`
- `where:`
- `venue:`
- `who:`
- `action:`

例如：

```text
when:蓝调时刻 who:新娘 挥手
where:草坪 新人拥抱
lighting:室内暖光 宾客鼓掌
```

筛选值不是枚举，而是对数据库自由文本执行不区分大小写的包含匹配。

当前混合评分：

- 同时有文本和视觉 embedding：`0.5 * text_semantic + 0.3 * visual + 0.2 * lexical`；
- 只有文本 embedding：`0.75 * text_semantic + 0.25 * lexical`；
- 只有视觉 embedding：`0.75 * visual + 0.25 * lexical`；
- 没有 embedding：只使用 lexical。

文本和视觉相似度使用 cosine。视觉分数取该镜头已有代表帧中的最高值。

文本 embedding 已接入 `BAAI/bge-small-zh-v1.5`，通过 FastEmbed 0.8.0 / ONNX 在本机运行：

- 中文专用 24M 参数模型；
- 512 维归一化向量；
- 本机缓存约 91MB；
- 镜头 `search_text` 使用 passage embedding；
- 用户查询使用 query embedding；
- 版本保存为 `fastembed-v1:BAAI/bge-small-zh-v1.5`；
- `embed-text` 可以给已有 shots 补向量，不会重新运行 Mage-VL；
- 重复回填会跳过同版本已有向量。

视觉 embedding 仍采用外部命令适配器，尚未实际安装或生成真实向量。此前的推荐方向是使用 SigLIP2 一类共享图文空间模型。没有任何 embedding 时，字面搜索和结构化筛选仍可工作。

## 8. 已实现代码

主要文件：

- `src/video_search/media.py`：视频发现、FFprobe、FFmpeg 镜头分段、缩略图；
- `src/video_search/analysis.py`：结构化 JSON 校验、动态搜索文本、临时镜头；
- `src/video_search/mage_client.py`：均匀抽帧、OpenAI 兼容 Mage 服务客户端；
- `src/video_search/mage_local.py`：实验性 PyTorch MPS 本地服务和只读 preflight；
- `src/video_search/database.py`：SQLite schema 和数据访问；
- `src/video_search/indexer.py`：完整索引流程、版本跳过与失败状态；
- `src/video_search/embeddings.py`：文本/视觉 embedding 命令适配器；
- `src/video_search/search.py`：筛选、字面、文本和视觉混合检索；
- `src/video_search/server.py`：本地网页 API、受控媒体访问和 HTTP Range；
- `src/video_search/web/index.html`：非技术搜索界面；
- `src/video_search/cli.py`：`init/index/search/inspect/status/serve/mage-*` 命令；
- `tests/`：单元和集成测试。

索引器会基于文件大小和 mtime 生成指纹。只有文件指纹、分段版本和分析版本都匹配，且视频状态为 `ready` 时才跳过；首次导入中断会保留成功镜头，重建则在 staging 完整成功后原子替换旧分析。

本地网页只允许通过已入库的 shot 访问对应源视频或缩略图，不接受任意文件路径；视频接口支持 HTTP Range，因此浏览器可以从命中镜头的时间点播放。

## 9. CLI 状态

基础命令：

```bash
uv sync
uv run video-search --db ./video-search.sqlite3 init
uv run video-search --db ./video-search.sqlite3 scan /path/to/videos
uv run video-search --db ./video-search.sqlite3 status
uv run video-search --db ./video-search.sqlite3 jobs
uv run video-search --db ./video-search.sqlite3 search "when:蓝调时刻 新娘挥手"
uv run video-search --db ./video-search.sqlite3 inspect 1
uv run video-search --db ./video-search.sqlite3 serve --port 8765
```

使用 OpenAI 兼容 Mage 服务建立索引：

```bash
uv run video-search --db ./video-search.sqlite3 index /path/to/videos \
  --mage-base-url http://127.0.0.1:<PORT>/v1 \
  --mage-model <MODEL_ID> \
  --mage-prompt prompts/shot-analysis-v1.txt \
  --analysis-version "<MODEL>+prompt-v1+schema-v1"
```

若服务需要 Bearer Token，使用环境变量 `MAGE_API_KEY`。不要把 token 写入仓库。

本地搜索网页的临时公网访问：

```bash
cloudflared tunnel --url http://127.0.0.1:8765
```

以前建立过一个 `trycloudflare.com` 临时地址，但不能假设现在仍有效。需要时重新启动 tunnel，并只暴露搜索服务端口，不要暴露模型管理接口或文件系统。

## 10. 真实婚礼素材验证

用户允许只读使用：

```text
/Users/jerry/Downloads/video_assets/
```

其中两段测试素材：

| 文件 | 编码 | 分辨率/帧率 | 时长 | 镜头数 |
|---|---|---:|---:|---:|
| `8252a60cfe2fabeb6eb22290113f0178_raw.mp4` | HEVC | 1920×1080 / 30fps | 30.700s | 6 |
| `c96bc7cb696289d4e250a61ca24f5c0f_raw.mp4` | HEVC | 1920×1080 / 30fps | 63.831655s | 22 |

验证结果：

- 两个源文件完整解码成功；
- FFmpeg `threshold=0.32` 共得到 28 个镜头；
- 生成了 28 张代表帧和两张 contact sheet；
- 测试索引位于 `.video-search-cache/asset-test/index.sqlite3`；
- 当前状态为 2 个 `ready` 视频、28 个镜头；
- 这些 28 个镜头的 `analysis_version` 是 `manual-contact-sheet-test-v1`，不是 Mage-VL 的真实输出；
- 搜索 API、网页播放和 HTTP Range 已验证；
- 选取第一个视频 `27.400s–28.433s` 的 1.033 秒真实镜头，成功完成 HEVC 源文件 -> 临时 H.264 镜头 -> 8 张 JPEG -> OpenAI 兼容假 Mage 服务 -> 结构化 JSON 的端到端传输；
- 该次服务返回是假数据，只证明数据链路，不证明 Mage-VL 识别质量；
- 源视频没有被修改，临时镜头和抽帧在分析后删除。

随后已完成真实 OptiQ Mage-VL 验证，详见 12.6。真实索引数据库位于 `.video-search-cache/optiq-test/index.sqlite3`，其中第一个 30.700 秒视频为 `ready`，共 6 个真实模型分析镜头，6 份 `analysis_json` 均通过 SQLite `json_valid()`。

## 11. 自动化验证

最后一次完整运行：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

结果：25 项测试全部通过。覆盖：

- JSON 自由值、动态事件顺序和错误校验；
- 临时镜头创建与删除；
- 视频发现、探测、分段和缩略图；
- 数据库结构、明细和版本化向量；
- 增量索引；
- 文本/视觉混合检索与自由值筛选；
- OpenAI 兼容 Mage 客户端；
- 本地 Mage 兼容接口；
- CLI 端到端索引；
- 本地网页和媒体 Range 请求。

同时运行过 `compileall` 和 `git diff --check`，均无错误。

## 12. Mage-VL 运行方式研究

### 12.1 微软官方 BF16 / PyTorch 版本

事实：

- 模型：`microsoft/Mage-VL`；
- 约 5B 参数，Hugging Face 仓库总计约 10.8GB；
- Mage-ViT 视觉塔 + Qwen3-4B 语言塔；
- 支持图片、均匀抽帧视频、H.264/HEVC codec 路径和 streaming；
- 官方帧采样路径使用多张图片作为视频输入；
- 官方在线路径使用 OpenAI 兼容接口发送多张 `image_url`。

限制：

- 官方完整 `requirements.txt` 包含 `mamba-ssm` 和 `flash-attn`，它们面向 CUDA；不能在 Mac 上原样照装；
- 模型代码支持 eager/SDPA，且未发现硬编码 `.cuda()`，所以裁剪后的 PyTorch MPS 帧采样路径理论上可行；
- 本机尚未下载官方权重，也没有完成真实 MPS 推理，因此不能宣称已验证可用或性能可接受。

项目中已经实现 `mage-preflight` 和实验性的 `mage-serve --device mps`，但在发现 OptiQ Mac 版之后，这条路径应降级为备用研究方案。

### 12.2 本机硬件与官方版预检

已确认：

- MacBook Pro `MacBookPro18,3`；
- Apple M1 Pro，10 核；
- 32GB 统一内存；
- 检查时约 80GiB 可用磁盘；
- Python 3.12；
- FFmpeg/FFprobe 可用；
- PyTorch、Transformers 等本地 Mage 依赖当时尚未安装；
- 官方模型当时未缓存；
- `uv sync --extra mage-local --dry-run` 能为 macOS arm64 解析全部精简依赖，但没有实际安装，也没有下载模型。

### 12.3 Docker Model Runner

本机有 Docker CLI 和 Docker Model Runner，但检查时 Docker Desktop/daemon 未运行。此前文档研究显示 macOS Model Runner 主要依赖 llama.cpp/Metal 和 GGUF；官方 Mage-VL 是 Safetensors/custom architecture。Hugging Face 页面虽然展示 `docker model run hf.co/microsoft/Mage-VL`，但没有在这台 Mac 上实测成功。

结论：不把 Docker Model Runner 作为当前首选路径，除非后续做单独验证。

### 12.4 社区 GGUF

研究过 `JohnTDI-cpu/mage-vl-gguf`：

- 有较小的量化权重和视觉 projector；
- 其说明主要验证 Vulkan/CUDA/CPU/Linux/Docker；
- 没有找到可信的 Apple Metal 视频路径验证。

结论：不选为当前 Mac 首选。

### 12.5 普通 `mlx-vlm`

`mlx-vlm` 的内置 `mage_vl` 当时支持图片，但对视频输入会抛出 `NotImplementedError`；相关视频支持 issue 仍开放。

结论：不能仅安装普通 `mlx-vlm` 就认为 Mage-VL 视频理解已可用。

### 12.6 OptiQ 的 Mac 专用 Mage-VL（当前推荐）

模型：

```text
mlx-community/Mage-VL-OptiQ-4bit
```

研究结论：

- 专门面向 Apple Silicon，使用 MLX，不依赖 PyTorch 或 CUDA；
- 语言塔使用混合 4/8-bit，约 3.0GB；
- Mage-ViT 视觉塔保持 BF16 sidecar，约 0.63GB；
- 总磁盘约 3.7–3.9GB；
- 支持图片和视频；视频采用均匀抽取多帧；
- 不需要 DCVC neural codec；
- 提供 OpenAI + Anthropic 兼容服务；
- 官方文档示例为：

```bash
pip install "mlx-optiq>=0.4.8"
optiq serve --model mlx-community/Mage-VL-OptiQ-4bit
```

- OptiQ 声明视觉塔 MLX 移植与 PyTorch 参考实现做过数值对齐，float32 最大绝对差约 `1.7e-3`；
- OptiQ `0.4.8` 首次加入 Mage-VL；`0.4.9` 修复了 `optiq serve` 的 Mage 图片/视频崩溃，因此实际安装应使用当前最新版，至少不要停在 0.4.8；
- 研究时 PyPI 最新版为 `0.4.33`（该版本号会变化，安装前重新查询）。

为什么它最适合本项目：

- 本机就是 M1 Pro 32GB；
- 模型体积远小于官方 BF16；
- video-search 已经负责抽取镜头多帧；
- video-search 已经实现 OpenAI 兼容客户端；
- `optiq serve` 接受多个 `image_url`，接口可直接对接；
- 不需要修改数据库、索引器、提示词或搜索 UI。

本机实测（2026-08-30）：

- 使用 `mlx-optiq 0.4.33` 和 `mlx-community/Mage-VL-OptiQ-4bit:no-think`；
- 模型缓存实际占用 3.7GB；
- 单张真实代表帧请求耗时 39.46 秒（包含首次模型载入），正确识别直升机驾驶舱、新娘、新郎和耳机；
- 同一真实镜头的 8 帧请求耗时 69.327 秒，返回合法结构化 JSON，并识别新人在直升机内微笑互动；
- 对无声素材，模型可能根据口型输出“说话”等视觉推断，不能把它当成音频证据；
- `max_tokens=1200` 时，第一个较复杂镜头的 JSON 在 `events` 中途被截断；提高到 2400 后解决，项目默认值已同步改成 2400；
- 第一个 30.700 秒 HEVC 视频完整索引成功：6 个镜头、0 失败、时间覆盖 0–30.700 秒，连续处理约 12 分钟；
- 处理过程中系统空闲内存在约 4%–31% 间波动，完成后回升到 76%；因此 M1 Pro 32GB 应使用 `--max-concurrent 1`，不要并行分析镜头；
- 单进程 RSS 约 1.3GB 不能代表真实统一内存峰值，因为 MLX/Metal 缓冲区未完整计入该数字；
- 字面查询“直升机”返回 3 个镜头；`where:直升机 新人` 精确命中 27.400–28.433 秒；
- 未启用文本 embedding 时，口语查询“直升机里的新郎新娘”没有命中；接入 BGE 后，该查询第一名为 27.400–28.433 秒的直升机内部新人镜头；
- “新人交换结婚誓言”第一名为 0–5.933 秒；“乘坐飞机欣赏高山湖泊”第一名为 28.433–30.700 秒；
- 真实索引库已有 6 条 `fastembed-v1:BAAI/bge-small-zh-v1.5` 向量，全部为 512 维；
- 第二个 `63.831655` 秒视频已完成真实 Mage 索引：共 22 个镜头，覆盖 `0–63.832` 秒且无间隙；22 份 `analysis_json` 均可解析，22 张缩略图齐全；补做文本 embedding 后整个测试库为 28 条 `fastembed-v1:BAAI/bge-small-zh-v1.5`、512 维向量。
- 第二个视频的首次连续运行约 28 分钟完成前 20 个镜头，在第 21 个镜头（`59.267–62.300` 秒）因超长且未闭合的 JSON 中断。将 `max_tokens` 从 2400 提到 4000 后仍在约 1.1 万字符处产生不合法 JSON；对这一约 3 秒镜头改用 4 张均匀帧，并限制最多 3 个事件、8 个物体和 1800 个中文字符后，最后两个镜头分别用时 51.5 秒和 43.6 秒并成功入库。
- `47.733–54.700` 秒的两个纯风景空镜初次被模型写成“无法判断”。用同一紧凑提示重分析后，正确得到雪山、云海、湖泊、云层流动等描述，分别耗时 44.1 秒和 46.0 秒。“云海覆盖的雪山与湖泊空镜”查询会把这两个镜头排在前两名。
- 第二个视频实测期间，系统空闲内存最低采到 6%–7%；交换空间相对运行前增加约 6GB，最高已用约 34.5GB，完成或失败后空闲内存可恢复到 65% 以上。由于运行前其他应用已经占用大量 swap，这些绝对值不能全部归因于 Mage-VL，但增量和压力峰值说明 32GB 机器也应保持单并发。
- 第二个视频的查询回归正常：“雪山湖边新人亲吻拥抱”第一名为 `62.300–63.832` 秒；“直升机飞过机场跑道”第一名为 `40.767–44.333` 秒。

本机与 M4 Mac mini 的容量判断：

- 当前实测机为 10 核 CPU、14 核 GPU、32GB 统一内存的 M1 Pro MacBook Pro；M1 Pro 内存带宽为 200GB/s。
- 基础款 M4 Mac mini 为 10 核 CPU、10 核 GPU、120GB/s 内存带宽，16GB 统一内存可选 24GB 或 32GB。虽然 M4 的单核和 CPU 侧处理更新，但对 MLX 视觉语言模型，统一内存容量、带宽和 GPU 规模同样关键。
- 因此不能预期 M4 16GB 在本项目端到端分析中快于当前 M1 Pro 32GB；结合本次 6% 空闲内存和约 6GB swap 增量，它更可能频繁换页并出现吞吐下降。这个结论是基于规格和本机压力数据的推断，仍需同一视频实机 A/B 才能给出精确倍率。
- 如果专门为本项目购机，最低建议 M4 24GB；希望明确超过当前机器，优先 M4 Pro 24GB 或更高。M4 Pro 基础配置为 12 核 CPU、16 核 GPU、273GB/s 带宽，并可选 48GB/64GB；长批量任务更推荐 48GB。
- 官方规格来源：[Mac mini (2024)](https://support.apple.com/en-euro/121555)、[MacBook Pro 14-inch (2021)](https://support.apple.com/en-us/111902)。

仍需注意：OptiQ 是较新的第三方量化与运行时，不是微软发布的官方 MLX 版本。升级运行时或模型后必须重新跑同一真实素材回归。

## 13. 当前推荐架构

```text
video-search
  ├─ FFmpeg 镜头分段
  ├─ 源时间范围直接均匀抽帧
  ├─ OpenAI-compatible MageServiceAnalyzer
  ├─ SQLite 索引与搜索 UI
  └─ http://127.0.0.1:<optiq-port>/v1
       └─ optiq serve
            └─ mlx-community/Mage-VL-OptiQ-4bit
```

保留 `mage_local.py` 的 PyTorch MPS 路径作为实验/回退，但 README 的主推荐应在 OptiQ 实测通过后改成 OptiQ。不要同时启动和下载两套权重，避免浪费磁盘和混淆结果。

## 14. 下一步执行清单

按以下顺序推进：

已完成：OptiQ 版本确认、独立 `mage-mlx` 依赖、本机安装、模型下载、单帧测试、8 帧测试、首个 6 镜头视频完整索引、JSON 与内存验证，以及真实中文文本 embedding 接入和口语查询验证。

第二个视频、断点续跑、无效 JSON 自动降级重试和首批固定查询评测均已完成。

真实评测集位于 `eval/wedding-queries-v1.json`，版本为 `wedding-real-v1`，包含 30 条人物、地点、时段、动作、物体和构图查询。BGE 文本语义混合搜索原始基线：Top 1 `26/30`（`86.6667%`）、Top 3 `30/30`（`100%`）、MRR `0.933333`。人物、地点、物体、构图和时段全部 Top 1 命中；四个 Top 1 未命中均为动作查询，正确镜头都在第二名：

- `宾客列队走进婚礼现场`；
- `新人拥抱后走向直升机并起飞`；
- `新人闭眼依偎并轻抚对方`；
- `雪山湖边新人牵手后拥抱`。

已完成第一阶段结构化事件重排：

- 不新增模型或数据库表，直接读取按 `sequence` 排序的 `shot_events`；
- 对查询中的精确动作词、事件描述中文双字片段覆盖率和动作出现顺序计算事件分；
- 最终分以 15% 事件权重和原混合搜索分数组合；
- 即使未加载文本 embedding，字面分为零但结构化事件命中的查询也能召回对应镜头；
- 单个动作若至少出现在 3 个镜头且覆盖 20% 以上索引，则视为高频通用动作，不单独触发事件重排；这条保护修复了“微笑”干扰人物查询的问题；
- 权重网格实测：10% 为 Top 1 `29/30`，15%、20%、25% 均为 `30/30`；选择最低的 15%；
- 当前结果：动作 Top 1 `14/14`，非动作 Top 1 `16/16`，总 Top 1 `30/30`，Top 3 `30/30`，MRR `1.0`。

后续按以下顺序推进：

1. 扩充更多视频和固定查询，验证事件权重能否跨素材保持稳定，避免只适配当前 28 个镜头。
2. 用颜色、构图和遗漏物体查询单独评估代表帧视觉 embedding 的增益，不把它当成动作排序的主要解法。
3. 根据更多素材实测调整抽帧数、最大宽度、提示词、token 上限和场景阈值。
4. 已完成第一版 Agent Skill；下一步只需随着便携素材库的数据库选择方式更新其本地配置，不要把模型、源视频或 SQLite 打包进 Skill。

建议的索引调用形式：

```bash
uv run video-search \
  --db ./.video-search-cache/optiq-test/index.sqlite3 \
  index /Users/jerry/Downloads/video_assets \
  --mage-base-url http://127.0.0.1:<PORT>/v1 \
  --mage-model mlx-community/Mage-VL-OptiQ-4bit:no-think \
  --mage-prompt prompts/shot-analysis-v1.txt \
  --mage-max-long-edge 896 \
  --analysis-version "mage-vl-optiq-4bit+prompt-v1+schema-v1"
```

本机实测端口使用 30000。若修改端口，以 `optiq serve` 启动日志为准。

## 15. 风险与观察点

- 场景检测阈值可能不适合所有婚礼机位和剪辑风格。
- 多帧抽样会漏掉很短的动作；抽帧数越多，视觉 token 和延迟越高。
- 量化语言塔可能降低细粒度描述或严格 JSON 能力，需要用真实镜头评测。
- VLM 可能把服装角色误判成具体身份；提示词和 UI 应始终展示置信度/可复核画面。
- 同一源文件指纹、分段版本和分析版本下，失败或中断的视频会保留边界一致的成功镜头，再次执行只补缺失、失败或边界不一致的镜头。源文件或版本变化仍会完整重建；版本切换时若需要零停机回滚，再考虑 staging/transactional replace。
- 当前 SQLite 搜索是逐 shot 扫描；几百到几千镜头可接受，规模更大时再加 FTS5/ANN。
- 当前缩略图固定取镜头中点，不做清晰度、人脸、闭眼或运动模糊评分。
- Cloudflare Quick Tunnel 是临时公网入口，没有应用级鉴权；只适合短期查看，不能作为长期公开服务。
- OptiQ 项目和 Mage Mac 支持较新，升级时必须重新跑真实素材回归测试。

## 16. 仓库状态与保护边界

- 项目根目录：`/Users/jerry/Documents/Projects/video-search`；
- 当前仓库尚无提交；`git status` 显示项目文件均为 untracked；
- 不要在用户未要求时创建 commit；
- `.venv/`、SQLite、WAL/SHM 和 `.video-search-cache/` 已加入 `.gitignore`；
- `/Users/jerry/Downloads/video_assets/` 是只读测试素材，不应移动、重命名、覆盖或加入仓库；
- `.video-search-cache/asset-test` 中的数据库、缩略图和 contact sheet 是可重建测试产物；
- 不要把 Cloudflare 临时 URL、API key 或本机隐私硬件标识写入代码或提交。

## 17. 研究来源

### Mage-VL 官方

- GitHub：https://github.com/microsoft/Mage
- Mage-VL README：https://github.com/microsoft/Mage/blob/main/mage_vl/README.md
- 官方推理示例：https://raw.githubusercontent.com/microsoft/Mage/main/mage_vl/inference_base.py
- 官方依赖：https://github.com/microsoft/Mage/blob/main/mage_vl/requirements.txt
- 官方模型：https://huggingface.co/microsoft/Mage-VL
- 官方模型文件：https://huggingface.co/microsoft/Mage-VL/tree/main
- 模型代码：https://huggingface.co/microsoft/Mage-VL/blob/main/modeling_mage_vl.py

### Apple Silicon / MLX

- OptiQ Mage-VL Mac 指南：https://mlx-optiq.com/docs/mage-vl
- OptiQ 发布说明：https://mlx-optiq.com/blog/mage-vl
- OptiQ changelog：https://mlx-optiq.com/changelog/archive
- OptiQ PyPI：https://pypi.org/project/mlx-optiq/
- OptiQ Mage 模型：https://huggingface.co/mlx-community/Mage-VL-OptiQ-4bit
- mlx-vlm Mage 视频支持 issue：https://github.com/Blaizzy/mlx-vlm/issues/1766
- PyTorch MPS：https://docs.pytorch.org/docs/stable/notes/mps.html

### 其他备选

- 社区 GGUF：https://github.com/JohnTDI-cpu/mage-vl-gguf
- Docker Model Runner inference engines：https://docs.docker.com/ai/model-runner/inference-engines/

### 文本 Embedding

- BGE 中文模型：https://huggingface.co/BAAI/bge-small-zh-v1.5
- FastEmbed：https://github.com/qdrant/fastembed
- FastEmbed 支持模型：https://qdrant.github.io/fastembed/examples/Supported_Models/
- Query / passage 检索用法：https://qdrant.github.io/fastembed/qdrant/Retrieval_with_FastEmbed/

## 18. 一句话交接结论

Mac 版已经在 M1 Pro 32GB 上完成两个真实婚礼视频验证：共 28 个镜头、28 份合法 JSON、28 张缩略图和 28 条 512 维中文文本向量。默认单并发；常规 Mage 输出结构无效时会自动降为 4 帧并使用紧凑提示重试。同一文件和版本的索引中断后会保留成功镜头，只补缺失部分。结构化事件重排使用 15% 权重后，30 条真实查询为 Top 1 `100%`、Top 3 `100%`、MRR `1.0`。本地 Agent Skill、搜索会话和镜头深链已经可用；下一步应继续产品化便携素材库，再扩充跨视频评测和视觉 embedding 评估。

## 19. 2026-08-30 Agent Skill 与搜索会话

- 新增 `search_sessions` 表，保存自然语言查询和当时的有序结果 JSON；普通搜索不写会话，只有 `search --save-session` 才创建记录。
- CLI 保存会话后返回 `session_id` 和 `result_url`。
- 本地网页支持 `/search/session/<session-id>` 恢复固定结果，也支持 `?shot=<shot-id>` 自动打开播放器并跳到精确镜头范围。
- 搜索结果 JSON 新增精简的 `who` 和 `actions`，便于 Agent 在当前对话中继续筛选；视频字节不会进入对话上下文。
- Skill 源文件：`skills/video-footage-search/SKILL.md`；个人安装位置 `/Users/jerry/.codex/skills/video-footage-search` 是指向仓库源文件的符号链接。
- Skill 数据库选择优先级：用户显式路径、`VIDEO_SEARCH_DB`、当前真实测试库、CLI 默认库。
- “第二个镜头”按已保存会话的 rank 2 解析，使用稳定 `shot_id`，不使用 `shot_index=1`，也不重新搜索。
- 真实验证会话页加载 5 条结果；深链 `?shot=12` 打开第 2 条，播放器范围为 `11.367–13.133s`。
- 浏览器关闭未读完的视频流会正常断开连接；服务端已处理 `BrokenPipeError/ConnectionResetError`，避免在可见终端打印无意义堆栈。
- 新增只读 `GET /api/shots/<shot-id>` 详情接口。镜头弹窗将完整分析转换为内容概述、人物、场景与时间、动作过程、可见物体、镜头语言和素材来源，不直接展示 JSON。
- 真实 `shot_id=12` 已验证：人物外观、场景背景、时段/光线、动作顺序、物体置信度和镜头语言均正确显示，视频范围仍为 `11.367–13.133s`。
- 当前自动化测试共 86 项，全部通过。

## 20. 2026-09-09 大素材库导入前的必要优化

目标目录为 `/Volumes/PEISEY/内容策划:剪辑项目/aMount TK剪辑/素材库`。只读盘点为 23 个真实视频、118.14GB、总时长约 3.14 小时；22 个为约 84Mbps 的 4K H.264，8 个 Ceremony 超过 10 分钟，最长 25.2 分钟；另有 23 个视频扩展名的 AppleDouble `._*` sidecar。

已完成以下导入阻塞项：

- 递归发现忽略 `._*`、隐藏文件和隐藏目录。新增只读 `scan`，逐文件 FFprobe，隔离坏文件并报告视频数、容量、时长、分辨率、镜头/缓存空间估算。
- FFmpeg 场景边界之后再把长区间均匀切为不超过 30 秒的连续镜头；最大时长进入 `segmentation_version`，参数变化会触发正确重建。
- Mage 服务路径不再创建或完整转码临时镜头，也不再完整计帧；直接按 `start_ms/end_ms` 从源视频抽取 8/16/24 张缩放 JPEG，并复用中间样本作缩略图。
- 新增持久化 `index_jobs`、非阻塞数据库独占锁、逐镜头安全停止、继续运行、当前文件/镜头/阶段、完成数、已用时间、吞吐和剩余时间估算。任务只有拿到锁后才创建，避免并发冲突留下假的 running 记录。
- FFprobe、场景扫描、抽帧、缩略图、自定义分析命令、Mage HTTP 请求都有超时。
- Mage 网络超时、连接断开、HTTP 429/5xx 按 10、30、90 秒退避重试。四次尝试仍失败时，任务进入 `paused/service-unavailable`，保留成功镜头并停止处理后续视频；服务恢复后用同一参数和 `--resume-job` 继续。JSON 结构失败仍使用独立的 4 帧紧凑提示重试，401/403 等非临时错误不会盲目重试。
- `videos` 增加 `source_root`、`relative_path`、`project_name`、`event_date`、`media_type`、`edit_version`、`metadata_search_text`。目录元数据与 Mage 画面描述共同进入字面检索和文本 embedding；项目日期与画面时段仍是不同字段。

外置盘首个竖屏素材试跑暴露并修复了一个资源阻塞问题：旧抽帧过滤器只限制宽度，导致 `1080x1920` 竖屏帧不缩放，4 帧和 8 帧请求都会在 Mage 视觉塔预填充阶段触发 Metal OOM。现在统一限制最长边为 `1280` 且不放大小图，竖屏样本会成为 `720x1280`；横屏既有行为保持不变，并增加了真实 FFmpeg 竖屏回归测试。

修复后使用单并发、`--max-context 4096 --kv-bits 4` 恢复原 `paused/service-unavailable` 任务，同一 `22.105s` teaser 的 14 个镜头全部以常规 8 帧一次成功完成，无网络重试或 4 帧 JSON 降级，总耗时 `1803s`。外置盘数据库位于素材库内的 `.video-search/pilot/index.sqlite3`：SQLite 完整性为 `ok`，14 个镜头均为 `ready`，时间范围连续覆盖 `0–22105ms`，有 14 份合法 JSON、14 张数据库引用的缩略图和 14 条 512 维文本向量。查询“户外新人手牵手行走，宾客抛洒花瓣”将 `16.516–22.105s` 镜头排在第一。ExFAT 会为每张 JPG 生成 `._*.jpg` AppleDouble 元数据伴随文件；它们不是额外缩略图，隐藏目录和隐藏文件过滤会阻止它们进入素材扫描。

镜头数预检使用每镜头 3–30 秒给出上下界；缓存按每镜头 512KiB 做容量规划。这两个数是规划估算，不是场景检测的实际结果。任务 ETA 根据当前已观察文件的镜头密度和已完成镜头吞吐动态计算，早期波动属于预期。

完成分辨率参数后，当前自动化测试共 90 项，全部通过，并以 `ResourceWarning` 作为错误运行。

### 20.1 Mage 输入分辨率 A/B

基于上述外置盘 teaser 的同一组 14 个镜头边界，固定每镜头均匀抽取 8 帧、相同模型/提示词/token 上限，对最长边 `896` 与 `640` 各完成 14 次分析。共 28 次请求全部成功，未触发网络重试、紧凑 JSON 重试、4 帧降级或 Metal OOM；原始结果保存在外置盘 `.video-search/pilot/resolution-ab-2026-09-10.jsonl`，详细报告为 `eval/resolution-ab-2026-09-10.md`。

- `896`：总推理时间 `986.0s`，平均 `70.4s/镜头`；14 个镜头中 11 个与 1280 等效或更好，3 个有局部元数据偏差，没有主事件错误。
- `640`：总推理时间 `641.8s`，平均 `45.8s/镜头`；比 896 快 `34.9%`，但只有 8 个等效或更好、5 个有细节损失，并在一个静止镜头中把“并肩站立”错判成“并肩行走”。
- 现有 `1280` 的 `1803s` 来自完整正式索引，包含冷启动、分段、缩略图和文本 embedding，不能与两档隔离推理时间做严格百分比对照。

`896` 已改为正式索引的默认平衡档，并继续保留 8 帧时间采样。CLI 可通过 `--mage-max-long-edge` 选择其它正整数值；`640` 适合作为快速模式，`1280` 适合作为细节档或关键镜头重分析。正常分析和紧凑重试共用该设置，实际分析版本会自动追加 `+max-edge-<像素>`，因此改变分辨率会触发正确重建，恢复任务时也会校验分辨率一致。

当前仍未完成的产品化、质量和规模优化统一维护在根目录 `BACKLOG.md`；后续以该文件为准，旧 review 和本 handoff 中的历史清单只作为决策与验证记录。
