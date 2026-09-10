# video-search

本机视频素材镜头搜索：扫描一个或多个指定文件夹、按镜头分段、保存结构化视觉分析，并用自然语言定位到源视频的准确时间范围。

当前已完成的核心闭环：

- 递归发现常见视频格式，忽略 `._*` 和隐藏目录，并提供不写数据库的预检。
- 使用 FFmpeg 场景变化检测生成连续的 `start_ms / end_ms` 镜头范围；任何镜头最长 30 秒。
- Mage‑VL 直接从源视频的时间范围均匀抽帧，并复用中间帧作缩略图，不生成完整临时镜头。
- 持久化索引任务、实时进度和耗时估算；支持安全停止及从成功镜头继续。
- 保存 Mage‑VL 原始 JSON，以及角色、地点、画面时段、事件、物体等规范化数据。
- `when_period / lighting / environment / venue` 都是自由文本，不是枚举。
- 保存文本向量、代表帧、视觉向量及各自版本。
- 从目录和文件名提取项目、日期、素材类型、剪辑版本及相对路径并参与搜索。
- 混合文本语义、视觉语义、字面匹配、结构化事件重排和自由值硬筛选。
- 提供 JSON CLI 和本地网页；结果包含源文件、镜头开始/结束毫秒与秒数。

## 运行

项目没有必需的 Python 运行时依赖，系统需要 Python 3.12、FFmpeg 和 FFprobe。

```bash
uv sync
uv run video-search --db ./video-search.sqlite3 init
uv run video-search --db ./video-search.sqlite3 scan /path/to/videos
uv run video-search --db ./video-search.sqlite3 status
uv run video-search --db ./video-search.sqlite3 search "when:蓝调时刻 新娘挥手"
uv run video-search --db ./video-search.sqlite3 inspect 1
uv run video-search --db ./video-search.sqlite3 serve --port 8765
```

`scan` 只读源目录，也不会创建或迁移数据库。报告包含真实视频数、失败文件、总容量/时长、分辨率、按 3–30 秒每镜头估算的镜头范围，以及按每镜头 512KiB 估算的缓存空间范围。

打开 `http://127.0.0.1:8765` 即可搜索和按镜头时间播放。需要临时公网访问时，可另开终端运行：

```bash
cloudflared tunnel --url http://127.0.0.1:8765
```

## Mage‑VL 分析

### 使用 OpenAI 兼容服务

这是推荐的索引方式：video-search 负责直接从每个源镜头时间范围均匀抽帧，Mage‑VL 服务负责根据多帧理解动态过程并返回结构化 JSON。源视频只读；抽帧位于临时目录，单镜头分析结束即删除。

```bash
uv run video-search --db ./video-search.sqlite3 index /path/to/videos \
  --mage-base-url http://127.0.0.1:30000/v1 \
  --mage-prompt prompts/shot-analysis-v1.txt \
  --analysis-version "mage-vl+prompt-v1+schema-v1"
```

服务地址也可以指向另一台配有 NVIDIA GPU 的电脑。设置 `MAGE_API_KEY` 即可连接需要 Bearer Token 的服务。

### 在 Apple Silicon 本机运行（推荐：OptiQ / MLX）

安装独立的 MLX 推理依赖并启动服务：

```bash
uv sync --extra mage-mlx
uv run optiq serve \
  --model mlx-community/Mage-VL-OptiQ-4bit \
  --host 127.0.0.1 \
  --port 30000 \
  --max-concurrent 1 \
  --max-context 8192 \
  --no-anthropic \
  --no-responses
```

首次启动会下载约 3.7GB 权重。另开终端建立索引：

```bash
uv run video-search --db ./video-search.sqlite3 index /path/to/videos \
  --mage-base-url http://127.0.0.1:30000/v1 \
  --mage-model mlx-community/Mage-VL-OptiQ-4bit:no-think \
  --mage-prompt prompts/shot-analysis-v1.txt \
  --mage-max-long-edge 896 \
  --analysis-version "mage-vl-optiq-4bit+prompt-v1+schema-v1"
```

Mage 抽帧默认把画面最长边限制为 896 像素，并保持原始宽高比、不放大小图。可以用 `--mage-max-long-edge 640` 切换快速档，或用 `--mage-max-long-edge 1280` 保留更多细节。分辨率会自动追加到实际分析版本，例如 `mage-vl-optiq-4bit+prompt-v1+schema-v1+max-edge-896`；因此切换档位会正确触发重分析，恢复暂停任务时也必须使用原任务相同的分辨率。

默认最多生成 2400 tokens；实测 1200 tokens 会使较详细的镜头 JSON 被截断。如果常规抽帧返回无效 JSON 或不符合分析结构，客户端会自动降为 4 张均匀帧并追加紧凑输出约束，再重试一次。网络超时、连接断开、HTTP 429 和 5xx 会按 10、30、90 秒退避重试；四次请求仍不可用时，索引任务会在当前镜头安全暂停，不再继续让后续视频失败，可在服务恢复后使用 `--resume-job` 继续。非临时 HTTP 错误和 FFmpeg 错误不会被重试或降级掩盖。M1 Pro 32GB 上应保持单并发。OptiQ 本地开发服务不需要 API key；若换成有鉴权的服务，再设置 `MAGE_API_KEY`。

每次 `index` 都会返回 `job_id`，并在标准错误输出持续打印当前文件、镜头、阶段、已用时间、吞吐、估算总镜头数和估算剩余时间。估算值会随着已完成素材增加而收敛。

```bash
uv run video-search --db ./video-search.sqlite3 jobs
uv run video-search --db ./video-search.sqlite3 stop <job_id>
# 使用与原任务完全相同的文件夹、分析版本和分段参数：
uv run video-search --db ./video-search.sqlite3 index /path/to/videos \
  --mage-base-url http://127.0.0.1:30000/v1 \
  --mage-prompt prompts/shot-analysis-v1.txt \
  --mage-max-long-edge 896 \
  --analysis-version "mage-vl-optiq-4bit+prompt-v1+schema-v1" \
  --resume-job <job_id>
```

`stop` 会设置安全停止标记，当前镜头完成后暂停。只有源文件指纹、镜头分段版本和分析版本都相同时才会续跑；边界和版本一致的成功镜头会被跳过。源文件或版本变化时会在 staging 中完整重建，成功后才替换旧索引。同一数据库同一时刻只允许一个索引任务。FFprobe、FFmpeg、Mage 请求和自定义分析命令均有超时保护。

项目仍保留官方 PyTorch MPS 实验路径作为回退。`uv run video-search mage-preflight` 可先做只读检查；之后用 `uv sync --extra mage-local` 和 `uv run video-search mage-serve --device mps --port 30000` 启动。该路径尚未用官方 10.8GB BF16 权重在本机完成实测。

### 自定义命令适配器

`index` 接收一个常驻服务客户端或本地运行器命令。video-search 会向该命令的标准输入写入：

```json
{
  "clip_path": "/private/tmp/video-search-shot-.../shot.mp4",
  "source_path": "/path/to/source.mov",
  "start_ms": 1250,
  "end_ms": 8750,
  "sample_frames": 8
}
```

命令必须在标准输出返回符合 [shot-analysis-v1.txt](prompts/shot-analysis-v1.txt) 的单个 JSON 对象。临时 `clip_path` 只在命令执行期间存在。调用示例：

```bash
uv run video-search --db ./video-search.sqlite3 index /path/to/videos \
  --analyzer-command "python /path/to/mage_client.py" \
  --analysis-version "mage-vl+prompt-v1+schema-v1"
```

本项目把推理运行器做成独立适配器，不把数据库和 UI 绑定到某个运行库。无论使用本机 MPS、远程 CUDA 服务还是自定义命令，数据库结构和搜索界面都不需要改变。

## 本地中文文本 Embedding

Mage-VL 分析完成后，可以为已有镜头补生成文本向量，不会重新切镜头或运行 Mage-VL：

```bash
uv sync --extra text-embedding
uv run video-search --db ./video-search.sqlite3 embed-text
```

默认模型是 `BAAI/bge-small-zh-v1.5`，使用 FastEmbed/ONNX 在本机运行。模型缓存约 91MB，每个镜头保存一个 512 维向量；重复执行 `embed-text` 会跳过已有相同版本的向量。

命令行搜索和本地网页需要显式加载同一个模型：

```bash
uv run video-search --db ./video-search.sqlite3 search \
  "直升机里的新郎新娘" \
  --text-embedding-model BAAI/bge-small-zh-v1.5

uv run video-search --db ./video-search.sqlite3 serve \
  --text-embedding-model BAAI/bge-small-zh-v1.5
```

索引镜头文本使用 passage embedding，用户查询使用 query embedding。向量版本自动保存为 `fastembed-v1:BAAI/bge-small-zh-v1.5`。

## 固定查询评测

使用真实查询集重复评测 Top 1、Top 3 和 MRR：

```bash
uv run video-search --db ./.video-search-cache/optiq-test/index.sqlite3 \
  eval eval/wedding-queries-v1.json
```

`eval` 默认加载与文本向量相同的 `BAAI/bge-small-zh-v1.5`。评测条目以视频文件名、`start_ms` 和 `end_ms` 标识正确镜头；同一查询可以列出多个同样正确的镜头。报告包含总体指标、分类计数、每条查询的排名，以及 Top 1 / Top 3 失败案例的前三名结果。

当前两个真实视频的 `wedding-real-v1` 包含 30 条查询。基础文本混合排序为 Top 1 `26/30`、Top 3 `30/30`、MRR `0.933333`；加入结构化事件重排后为 Top 1 `30/30`、Top 3 `30/30`、MRR `1.0`。

事件重排直接使用已有 `shot_events`，不增加模型或数据库表。它对查询中精确出现的动作、事件描述的中文双字片段覆盖和动作顺序分别评分，再以 15% 权重与原分数混合；未加载文本 embedding 时，结构化事件也能独立召回动作镜头。只命中一个且在索引中高频出现的通用动作不会触发重排，避免“微笑”等词压过更准确的人物、地点或物体语义结果。10%、15%、20%、25% 四档真实评测中，15% 是达到 30/30 且不损失非动作查询 Top 1 的最低权重。

## Embedding 扩展契约

文本 embedding 命令接收 `{"kind":"text","text":"..."}`；SigLIP2 命令同时支持文本请求和 `{"kind":"image","image_path":"..."}`。两者都返回：

```json
{"vector": [0.1, -0.2, 0.3]}
```

外部命令模式下，索引和搜索必须使用相同的版本名称。未配置 embedding 时仍可使用字面搜索和结构化筛选。

## Agent Skill 与搜索会话

本地 Skill 已位于 `skills/video-footage-search/`，并链接到个人 Codex Skill 目录。它只调用 CLI，不打包模型权重、源视频或 SQLite。

需要让 Agent 和网页共享同一份有序结果时，显式保存搜索会话：

```bash
.venv/bin/video-search --db <index.sqlite3> search "<自然语言>" \
  --limit 20 \
  --text-embedding-model BAAI/bge-small-zh-v1.5 \
  --save-session \
  --web-base-url http://127.0.0.1:8765
```

JSON 会额外返回 `session_id` 和 `result_url`。启动 `serve` 后，`result_url` 会恢复当时保存的排序；在 URL 后附加 `?shot=<shot_id>` 可以直接打开并播放指定镜头范围。“第二个镜头”等后续指令必须根据保存结果中的排名解析，不能使用全库顺序或重新搜索。

镜头弹窗会从 `GET /api/shots/<shot_id>` 读取完整分析，并以内容概述、人物、场景与时间、动作过程、可见物体、镜头语言和素材来源分区展示。页面不会直接显示原始 JSON；缺失分区会自动隐藏，详情加载失败也不影响视频播放。

由于视频位于本机，这个 Skill 使用本地 shell 和本地浏览器能力。托管环境不会因为拿到文件路径就自动获得本机文件访问权。将来若普通 ChatGPT 也需要调用同一索引，可以在现有 CLI 上增加 MCP 服务，不需要更改搜索数据层。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
