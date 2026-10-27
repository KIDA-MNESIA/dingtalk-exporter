# 钉钉聊天记录导出工具 (DingTalk Chat Exporter)

从钉钉桌面客户端的本地加密数据库中导出聊天记录，提供 Web 界面浏览、搜索和导出。

> **声明**：本工具仅供个人数据备份使用，请遵守公司数据政策和相关法规。

## 功能特性

- **解密**钉钉桌面端 V2、V3 加密 SQLite 数据库
- **WAL 增量支持** — 自动复制并解密 `dingtalk.db-wal`，补齐尚未 checkpoint 回主库的最新消息
- **同步冗余保护** — 增量同步自动回退一个时间窗口，避免手动同步多次时出现消息断层
- **快照回退保护** — 新快照异常或回退时继续沿用当前解密库，不用坏快照覆盖已有消息
- **固定快照查询** — 只读版本库、原子发布，翻页期间可继续读取同一版本
- **Web 界面**浏览全部会话和消息，支持搜索、筛选、分页
- **20+ 种消息类型**：文本、图片、文件、语音、富文本、引用、审批、互动卡片等
- **图片预览** — 包括引用消息和富文本中内嵌的图片
- **附件导出** — 图片、文档（docx/pdf/xlsx）一并打包到自包含目录
- **时间范围筛选** — 支持按近3个月/6个月/1年/2年/全部/自定义时间导出
- **ZIP 打包下载** — 一键下载包含所有附件的压缩包
- **AI 友好格式** — 导出 JSON 包含统一的 `content` 字段，内联附件路径，方便 AI 工具处理
- **合并转发解码** — 读取本地 Base64/MessagePack 子消息，保留嵌套位置与原始顺序
- **可读正文搜索** — 搜索解码正文、引用、转发子消息和附件名称，保留原始 JSON 搜索模式
- **自动同步** — 每4小时自动增量同步新消息
- **完全离线** — 无需云端 API、无需钉钉开放平台 Token、无需网络请求

## 快速开始

### 环境要求

- Python 3.10+
- 钉钉桌面客户端已安装并登录过（用于生成本地数据）
- 可选：`dingwave` 语音解码工具（放入 `tools/` 目录，用于语音消息导出）

### 安装

**方式一：一键安装（推荐）**

Windows 用户双击 `setup.bat`，Linux/Mac 用户运行 `./setup.sh`。

**方式二：手动安装**

```bash
# 克隆仓库
git clone https://github.com/KIDA-MNESIA/dingtalk-exporter.git
cd dingtalk-exporter

# 安装 Python 依赖
pip install -r requirements.txt

# 下载 dingwave 语音解码工具（可选）
# 从 https://github.com/p1g3/dingwave/releases 下载对应平台的二进制文件
# 放入 tools/ 目录（Windows: dingwave.exe，Linux/Mac: dingwave）
```

### 运行

```bash
python main.py
```

浏览器访问 http://localhost:8090

> **无需配置** — 工具自动扫描 `%APPDATA%\DingTalk\`、`%LOCALAPPDATA%\DingTalk\` 等多个目录查找 `*_v2`、`*_v3` 用户数据。如果同一台电脑有多个钉钉账号，会自动选择最近使用的那个。

### 手动配置（仅在自动检测失败时需要）

如果工具无法自动找到钉钉数据，可以设置环境变量：

```bash
set DINGTALK_UID=123456789
set DINGTALK_DATA_DIR=C:\Users\用户名\AppData\Roaming\DingTalk\123456789_v2
python main.py
```

或直接修改 `config.py` 中的默认值。

## 工作原理

1. 复制加密数据库 `dingtalk.db` 及其 `-wal` 到临时目录，核对复制前后的文件状态；不复制客户端的活动 `-shm` 索引
2. 依据当前账号 UID 自动解密主库，并按 SQLite WAL 结构解密 `dingtalk.db-wal`
3. 校验候选库，合并已提交 WAL，建立正文索引，再发布只读版本（128 个消息分表：`tbmsg_000`–`tbmsg_127`）
4. 通过 `im_image_info` 表和 `resource_cache/` 目录解析本地图片路径
5. 启动 FastAPI Web 应用，提供浏览、搜索和导出功能

## 使用说明

| 功能 | 操作 |
|------|------|
| 浏览会话 | 左侧会话列表向下滚动自动加载更多 |
| 查看消息 | 点击会话名，右侧显示消息流，最新消息在底部 |
| 搜索消息 | 顶部搜索栏输入关键词回车 |
| 搜索会话 | 左侧搜索栏按名称搜索 |
| 筛选会话 | 左侧"全部/群聊/单聊"Tab切换 |
| 查看图片 | 消息中的图片可直接预览，点击放大 |
| 手动同步 | 点击右上角"手动同步"按钮 |
| 勾选导出 | 点击"导出" → 勾选会话 → 选择时间范围 → "导出选中会话" |
| 全量导出 | 点击"导出" → "已导出文件"Tab → "全量导出所有会话" |
| 下载导出 | "已导出文件"Tab中点击"下载 ZIP" |

> **同步提示**：若钉钉桌面端仍在使用 WAL 模式，最新消息可能先写入 `dingtalk.db-wal`。现在“手动同步”会一并处理这部分数据，并保留一个增量冗余窗口，避免第二次及后续同步时出现消息断层；同步完成后如仍感觉缺消息，先在钉钉桌面端打开对应会话，再触发一次手动同步。

## 导出格式

每次导出生成一个自包含目录：

```
export_20260417_191234/
├── export.json            # 消息数据，附件使用相对路径引用
├── images/                # 所有导出的图片
│   ├── 12345_67890.jpg
│   └── 12345_67891_0.webp
└── files/                 # 所有导出的文档
    ├── report_v1.0.docx
    └── datasheet.pdf
```

`export.json` 中的 `content` 字段整合了文本和附件引用：

```json
{
  "sender_name": "用户A",
  "content": "[图片: images/12345_67890.jpg]\n一些描述文字",
  "image_export_paths": ["images/12345_67890.jpg"]
}
```

此格式专为 AI 工具设计 — `content` 字段提供统一可读字符串，附件路径为相对路径。

## MCP Server（AI 工具接入）

本工具内置 MCP（Model Context Protocol）服务，AI 客户端（Claude Desktop / Cursor / WorkBuddy 等）可直接查询本机聊天记录，无需写代码。

### 快速配置（stdio，推荐）

在 Claude Desktop 的 `claude_desktop_config.json`（或 Cursor 的 MCP 配置）中添加：

```json
{
  "mcpServers": {
    "dingtalk-exporter": {
      "command": "python",
      "args": ["D:\\路径\\dingtalk-exporter\\mcp_server.py"]
    }
  }
}
```

> 建议使用虚拟环境中的 Python：`D:\\路径\\dingtalk-exporter\\.venv\\Scripts\\python.exe`

### 远程访问（streamable HTTP）

```bash
python mcp_server.py --http --host 127.0.0.1 --port 8091
```

客户端连接 `http://127.0.0.1:8091/mcp/`。注意：聊天记录为个人私密数据，`--host` 默认仅绑定本机，请勿暴露到外网。

### 可用工具

| 工具 | 说明 |
|------|------|
| `list_conversations` | 会话列表，支持标题、单聊昵称或姓名过滤；使用 `offset` 分页，`limit` 为 1–500 |
| `get_messages` | 读取指定会话消息，支持时间范围、固定快照和分页；`limit` 为 1–500 |
| `get_forwarded_messages` | 读取合并转发子消息及嵌套位置，支持固定快照和分页；`limit` 为 1–500 |
| `search_messages` | 默认搜索可读正文；`mode=raw` 搜索原始 content JSON；`limit` 为 1–200 |
| `get_stats` | 会话/消息统计 |
| `get_database_status` | 解密数据库就绪状态 |
| `get_sync_status` | 同步进度、上次导出路径 |
| `refresh_database` | 只刷新查询快照和索引，不导出附件；返回 `job_id` |
| `get_job_status` | 根据 `job_id` 查询本次任务的状态、结果和错误 |
| `trigger_sync` | 刷新并执行全量或增量导出；返回 `job_id` |
| `export_conversations` | 导出会话为 JSON + 附件 |
| `read_attachment` | 读取本地附件/导出文件内容给模型 |

> **首次使用**：调用 `refresh_database`，然后用返回的 `job_id` 调用 `get_job_status`，等待任务完成再查询。需要同时导出聊天记录及附件时使用 `trigger_sync`。

### 按需刷新与任务状态

刷新、同步和导出共用一个跨进程任务锁。Web 与 MCP 同时运行时也只接受一个写任务。
接受请求返回 `status=started` 和 `job_id`；已有任务时返回 `already_running` 及该任务 ID，表示本次请求未启动。
Web 的冲突请求返回 HTTP 409。线程启动失败返回 `failed`，不会遗留忙状态。

使用 `get_job_status(job_id)` 等待 `status` 从 `running` 变为 `succeeded` 或 `failed`。
导出文件目录位于本次任务的 `result.export_path`，失败原因位于 `error`。
刷新和同步还会在 `result.outcome` 返回以下结果：

| outcome | 含义 |
|---------|------|
| `updated` | 新快照和索引已发布 |
| `unchanged` | 数据未变化，继续使用同一快照；所需索引已准备好 |
| `fallback` | 刷新失败，旧快照仍可查询；原因在 `result.error` |
| `failed` | 没有可用快照，任务失败 |

任务结束不一定表示拿到了新数据，必须检查 `outcome`。
`get_sync_status` 保留旧字段，但 `last_export_path` 和 `last_error` 对应最近任务；
历史成功导出目录单独保存在 `last_successful_export_path`。
`last_refresh_time` 是最近成功刷新时间，`last_sync_time` 是导出增量游标，刷新不会推进导出游标。

### 完整读取与分页

会话、消息、合并转发和搜索查询都返回以下字段：

| 字段 | 含义 |
|------|------|
| `total` | 当前筛选条件下的全部匹配数，不是本页条数 |
| `returned` | 本页实际返回的条数 |
| `limit` / `offset` | 本次请求的分页参数 |
| `has_more` | 是否还有下一页 |
| `next_offset` | 下一页的 `offset`；读完后为 `null` |
| `snapshot_id` | 本次读取的快照版本；后续请求可传回此值 |
| `snapshot_time` | 快照生成时间，使用带时区的 ISO 8601 格式 |
| `latest_message_at` | 快照内最新消息的毫秒时间戳 |

完整读取一个会话时，先调用 `get_messages(conversation_id=..., limit=500, offset=0)`。
之后保持相同的会话、时间范围和 `limit`，传回 `snapshot_id`，并将 `next_offset` 用作下一次的 `offset`，直到 `has_more=false`。
末页只返回剩余消息；`offset >= total` 时返回空列表。会话列表和跨会话搜索使用同一分页方法。

`get_messages` 从最新一页向更早的页读取，每页内部按时间从早到晚排列。
`search_messages` 对全部分表统一排序、统一分页，每页按时间从新到旧排列。
默认 `mode=text` 按 Unicode 忽略大小写的字面子串匹配，不搜索 JSON 元数据或本地路径。
`mode=raw` 保留原始 `content LIKE` 行为；两种模式的命中数可以不同。
命中转发正文时返回所属根消息，`matched_paths` 指明子消息位置；同一根消息只计一次。
时间相同的消息使用固定的附加排序字段。`get_messages` 的时间条件为 `since_time < 消息时间 <= until_time`，单位为毫秒。
工具不会在查询后再次截断消息；超出允许范围的 `limit` 或负数 `offset` 会返回参数错误。

MCP 单独运行时不会启动自动同步任务。需要新数据时调用 `refresh_database`。
传入 `snapshot_id` 的查询固定使用该版本，即使另一个任务发布了新快照。
不传时使用当前版本。至少保留当前版、上一版和最近 24 小时的版本；已清理的版本明确返回 `snapshot_expired`，不会自动换成新版。
旧式 `data/decrypted/dingtalk.db` 仍可读取；旧库发生变化后，旧 ID 会失效。首次成功刷新后自动使用不可变版本库。
数据仍仅来自本机缓存，未缓存的云端历史不会由此补齐。
`read_attachment` 只读取文本附件，不提取 PDF、Word、图片或语音的内容。

### 合并转发与解析状态

消息查询和导出都包含统一的 `content`，保留原有 `text`、引用和附件字段。
`parse_status` 的含义是：`complete` 表示支持的消息结构和正文已解析，`partial` 表示部分内容缺失或达到解析限制，`unsupported` 表示正文结构尚未支持。
状态不代表图片、视频或语音已被识别，也不代表云端历史完整。

合并转发父消息提供 `forwarded_info`。调用
`get_forwarded_messages(conversation_id=..., message_id=..., snapshot_id=..., limit=100, offset=0)`
读取其子消息，再按 `next_offset` 翻页。
返回结果按原附件顺序先序展开；`path` 和 `parent_path` 是从根消息起的附件索引数组，包含嵌套关系。
`total` 只统计可返回的子节点。必须同时检查 `parse_status` 和 `errors`；只有标题摘要的记录不会被当成完整空记录。

解码限制为最多 8 层嵌套、1,000 个子消息、单个解码载荷 2 MiB、累计载荷 8 MiB。
损坏载荷、未知二进制内容或超过限制会明确标记，已成功解析的同级消息仍然保留。
全量和指定会话导出按完整时间顺序排列，`forwarded_messages` 包含子消息及解析状态。

版本数据库位于 `data/decrypted/snapshots/`，派生索引位于 `data/decrypted/indexes/`。
索引按快照 ID 和解析器版本重用；过期版本及对应索引一起清理。任务记录位于 `data/jobs/`。
这些目录均在现有 Git 忽略规则内。

### 回归测试

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
# 可选：使用 Node.js 运行 Web 任务轮询测试
node --test tests/test_web_jobs.cjs
```

测试使用合成 SQLite 数据，不读取本机聊天记录，也不启动同步任务。
覆盖分页边界、单聊名称查询、正文检索、转发解码、导出排序、快照故障回退、跨进程任务、Web API 和真实 stdio MCP 调用。

## 项目结构

```
dingtalk-exporter/
├── config.py          # 配置（自动检测 + 路径、常量）
├── main.py            # 入口文件（uvicorn 服务器）
├── mcp_server.py      # MCP server（stdio / HTTP，AI 客户端接入）
├── decrypt.py         # 数据库解密（复制 DB/WAL + 快照校验/回退）
├── parser.py          # 消息解析（SQLite → 结构化数据）
├── forwarded.py       # 合并转发 MessagePack 解码与边界检查
├── message_content.py # MCP、搜索和导出共用的正文格式
├── search_index.py    # 按快照和解析器版本生成正文索引
├── snapshots.py       # 不可变快照、校验、原子发布与保留策略
├── jobs.py            # 跨进程任务准入与持久任务状态
├── exporter.py        # 导出为 JSON + 附件打包
├── attachment.py      # 附件文件管理
├── scheduler.py       # 自动同步定时任务（含增量游标冗余保护）
├── setup.bat          # Windows 一键安装脚本
├── setup.sh           # Linux/Mac 一键安装脚本
├── requirements.txt   # Python 依赖
├── tools/
│   └── dingwave.exe   # 语音解码工具（可选）
└── web/
    ├── api.py         # FastAPI 路由
    └── static/        # 前端页面（HTML/CSS/JS）
```

## 技术细节

- **数据库加密**：按本地账号 UID 派生密钥，逐页解密 SQLite 主库
- **WAL 处理**：保留 WAL 文件头和 frame 头，仅解密页数据并重算 checksum
- **同步游标**：仅按已导出的最大消息时间推进，并自动回退一个重叠窗口
- **快照保护**：候选解密快照必须通过校验且不早于已知最新消息，失败时继续沿用当前快照
- **消息分片**：128 个表（`tbmsg_000`–`tbmsg_127`），按哈希路由
- **图片解析**：`im_image_info` 表将消息 ID 映射到 `resource_cache/` 中的本地文件路径
- **引用/富文本图片**：通过 `im_image_info` 多行查找，解析每条消息的多张图片
- **文件附件**：从消息 JSON 的 `content.attachments[].filepath` 中提取本地路径
- **并发访问**：解密前将数据库及其 WAL 侧文件复制到临时目录，避免客户端锁冲突

## 已知限制

- 仅包含钉钉桌面客户端缓存的数据 — 从未在该设备上查看过的消息可能缺失
- 未缓存到本地的图片会显示占位符
- 需要在使用过钉钉桌面客户端的电脑上运行
- 若钉钉客户端本地库本身没有落下最新消息，仍需先在桌面端打开对应会话触发本地缓存

## 致谢

- [dingwave](https://github.com/p1g3/dingwave) — 钉钉数据库解密工具
- [FastAPI](https://fastapi.tiangolo.com/) — Web 框架
- [APScheduler](https://apscheduler.readreadocs.io/) — 定时任务

## 许可证

[MIT](LICENSE)
