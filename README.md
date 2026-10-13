# 知屿 RAG 知识库

一个可以直接在本地运行的轻量 RAG 应用：上传资料、自动切分、向量检索、基于原文回答，并展示可展开核对的引用片段。

## 深大官方资料验证环境

验证链路单独使用 Crawl4AI 0.9.3 和 RAGFlow 0.27.2，不替换原有 SQLite 知识库。
需要先安装并启动 Docker Desktop，启用 WSL 2 后端。部署脚本当前按 Windows D 盘安装布局编写；
如果 Docker 安装位置不同，请先调整 `szu_stack.py` 和 `start-docker.ps1` 中的程序路径。

```powershell
python szu_stack.py crawler
python szu_stack.py ragflow
python szu_stack.py status
```

部署命令自动生成随机内部凭据，后续从已有容器恢复。Crawl4AI 端口为
`http://127.0.0.1:11235`，RAGFlow 网页为 `http://127.0.0.1:8088`，API 为
`http://127.0.0.1:9380`。以上地址只有对应容器成功启动后才可访问。
首次下载较大；RAGFlow 官方建议至少 16 GB 内存，当前配置采用低并发
和容器内存限制，仅用于少量资料验证。

采集第一批 10 页，确认效果后最多可扩到 200 页：

```powershell
python szu_pilot.py crawl --max-pages 10
python szu_pilot.py export
```

采集限于 `jwb.szu.edu.cn`，优先处理通知正文，间隔至少 3 秒；获取不到 robots 规则时停止。
如果代理使用 Fake-IP，部署器通过加密 DNS 获取公网地址，仅对容器内的
教务部域名固定解析，不取消私网拦截。域名公网地址变化时需要重新验证并更新容器配置。

结果保存到一个 `data/szu-pilot.json` 报告，包含正文 Markdown、来源 URL、验证时间、哈希、
附件链接、失败与短文本页面统计。导出命令只生成一个可读的 `data/szu-pilot.md`。
附件只识别，不下载；发布日期从网页明确标注处提取，旧年份标记待复核。
适用学生、校区和实际有效性仍须审核，不能把抓取时间当作发布时间。长度过滤只是初筛，成功率不等于资料质量。

验证时已向“深圳大学知识库”上传首批 9 篇通知，尚未解析或生成向量。
仓库不包含账户、会话或采集数据；部署后需要在页面注册自己的账户，
配置聊天模型与嵌入模型，并在账户设置创建 API Key。
密钥只在运行环境设置，不提交到代码仓库：

```powershell
$env:RAGFLOW_API_KEY = '在本机页面生成的 API Key'
python szu_pilot.py import
# 确认嵌入模型已配置，再发起解析：
python szu_pilot.py import --parse
```

导入器创建“深圳大学知识库”，将每页正文作为独立 Markdown 文档上传，并加入原始 URL 和验证时间。
同一内容不会重复上传。解析与生成式回答需要模型服务，不会因 Docker 安装完成就自动可用。
模型连接需要正式 API Base URL、聊天模型名，以及单独的嵌入接口。
本项目不会把不确定用途的密钥发到猜测的接口。
第一批仅含教务部资料，校园网问题通常需要之后采集信息中心网站，不应据此臆造答案。

停止验证服务（保留卷内数据）：

```powershell
python szu_stack.py stop
```

## 已实现

- 支持 PDF、DOCX、Markdown、TXT、CSV、JSON
- 文件哈希去重，SQLite 持久化文档、分块和向量
- OpenAI Embeddings + Responses API 生成式问答
- 可切换任意 OpenAI 兼容的嵌入与聊天服务
- 未配置模型时自动使用中文/英文特征哈希进行本地检索
- 回答附来源文件、段落编号、相关度和原文
- 文档列表与单文档删除
- Brave Search 关键词发现、可信域名抓取与白名单外候选审核
- RSS/sitemap、条件请求、内容版本历史和 Windows 每日任务
- robots.txt、私网地址拦截、跨域重定向检查和每站限速
- 20 MB 上传限制、参数化 SQL、文件名净化
- 零前端构建步骤，响应式单页界面

## 快速开始

需要 Python 3.10 或更高版本。

```powershell
python -m pip install -r requirements.txt
python app.py
```

打开 <http://127.0.0.1:8000>。不安装依赖也能导入 DOCX、TXT、Markdown、CSV 和 JSON；`pypdf` 仅用于提取 PDF 文本。

首次启动会创建 `data/knowledge.db`。该目录包含知识库数据，不应提交到 Git。

## 启用生成式回答

当前 PowerShell 会话中设置模型参数：

```powershell
$env:RAG_API_KEY = "你的 API Key"
$env:RAG_CHAT_MODEL = "gpt-5-mini"
$env:RAG_EMBEDDING_MODEL = "text-embedding-3-small"
python app.py
```

`RAG_API_KEY` 也可以换成 `OPENAI_API_KEY`。密钥只从服务端环境变量读取，不会返回给浏览器。

接入使用 Chat Completions 的 OpenAI 兼容服务：

```powershell
$env:RAG_REMOTE = "1"
$env:RAG_BASE_URL = "http://127.0.0.1:11434/v1"
$env:RAG_CHAT_API = "chat_completions"
$env:RAG_CHAT_MODEL = "qwen3:8b"
$env:RAG_EMBEDDING_MODEL = "nomic-embed-text"
python app.py
```

已导入分块会记录所用嵌入模型。更换嵌入模型后，新旧文档可以共存；检索时应用会按各自模型生成查询向量。旧模型完全不可用时，对应分块会暂时跳过，重新使用当前模型导入即可。

## 配置项

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `RAG_API_KEY` | 空 | 模型服务密钥 |
| `RAG_BASE_URL` | `https://api.openai.com/v1` | OpenAI 兼容 API 根地址 |
| `RAG_EMBEDDING_MODEL` | `text-embedding-3-small` | 嵌入模型 |
| `RAG_CHAT_MODEL` | `gpt-5-mini` | 回答模型 |
| `RAG_CHAT_API` | `responses` | `responses` 或 `chat_completions` |
| `RAG_REMOTE` | 空 | 无密钥兼容服务设为 `1` |
| `RAG_HOST` | `127.0.0.1` | 监听地址 |
| `RAG_PORT` | `8000` | 监听端口 |
| `RAG_MAX_UPLOAD_MB` | `20` | 单文件大小限制 |
| `RAG_CHUNK_SIZE` | `900` | 分块字符数 |
| `RAG_CHUNK_OVERLAP` | `150` | 相邻分块重叠字符数 |
| `RAG_DATA_DIR` | 项目下的 `data` | SQLite 数据目录 |
| `BRAVE_SEARCH_API_KEY` | 空 | Brave Search API 密钥 |
| `CRAWLER_USER_AGENT` | `ZhiyuRAGBot/1.0 ...` | robots 与请求使用的爬虫标识 |
| `CRAWLER_MAX_PAGES` | `200` | 单次采集全局页面上限 |
| `CRAWLER_REQUEST_TIMEOUT` | `20` | 网络请求超时秒数 |
| `CRAWLER_HOST_DELAY` | `3` | 同一域名请求最小间隔秒数 |

环境变量在进程启动时读取，修改后需重启服务。`.env.example` 是配置样例，应用不会自动加载 `.env`，避免意外引入额外依赖。

## HTTP API

- `GET /api/health`：运行模式与文档统计
- `GET /api/documents`：文档列表
- `POST /api/documents`：导入一个 Base64 编码文件
- `DELETE /api/documents/{id}`：删除文档及其分块
- `POST /api/search`：只检索，不调用聊天模型
- `POST /api/chat`：检索并生成带引用回答

检索请求示例：

```json
{
  "query": "项目的交付日期是什么时候？",
  "top_k": 5
}
```

## 实现说明

导入链路为“文本提取 → 重叠切分 → 批量嵌入 → SQLite 入库”。提问时按嵌入模型分组生成查询向量，使用余弦相似度和词项覆盖率做混合排序，再把前几段原文交给回答模型。回答提示明确限制模型只依据检索片段作答，并要求用 `[1]`、`[2]` 引用。

当前版本定位为个人或小团队单机知识库，向量检索会扫描本地分块。数据达到数十万分块、需要多租户权限或公网部署时，建议换用专用向量数据库，并在 HTTP 层增加身份认证、限流、审计和 HTTPS。扫描版 PDF 需先 OCR。

## 互联网自动采集

先安装依赖并把 Brave Search Key 写入当前 Windows 用户环境：

```powershell
python -m pip install -r requirements.txt
[Environment]::SetEnvironmentVariable("BRAVE_SEARCH_API_KEY", "你的 Brave Search Key", "User")
```

重新打开终端并启动应用，在顶部切换到“自动采集”，配置：

- 研究主题和搜索关键词
- 允许自动抓取的可信域名
- 可选的 RSS 或 sitemap 地址
- 国家、搜索语言和每次页面上限

白名单内结果会自动抓取，其他域名只保存搜索标题、链接和摘要。你可以在候选列表中批准单个页面，或把整个域名加入可信列表。

手动执行一次：

```powershell
python crawler.py run
```

注册每天 18:00 运行、错过后尽快补跑的 Windows 计划任务：

```powershell
python crawler.py install-task --time 18:00
```

计划任务名为 `ZhiyuRAGDailyCrawler`。它使用当前 Python 解释器和项目路径，设置后移动项目或 Python 环境时需要重新注册。任务不会保存终端里的临时环境变量，因此 API Key 应写入 Windows 用户环境。

历史版本默认保留 180 天且每页最多 20 版；清理也会在每次成功采集后执行，可单独运行：

```powershell
python crawler.py prune
```

### 采集安全边界

- 仅访问 HTTP/HTTPS 的 80/443 端口，并拒绝回环、私网、链路本地和云元数据地址。
- 每次重定向重新校验目标域名和公网地址；只抓白名单或明确批准的 URL。
- 严格遵守 `robots.txt`；规则无法可靠获取时默认不抓。
- 同一域名串行、默认间隔 3 秒，429/5xx 最多重试 3 次。
- HTML 最大 5 MB，PDF 最大 20 MB；不登录、不绕过验证码或付费墙、不执行网页脚本。
- 只保存清洗后的正文和版本元数据，不保存原始 HTML/PDF。

新增采集接口：

- `GET/POST /api/crawl/sources`
- `PATCH/DELETE /api/crawl/sources/{id}`
- `POST /api/crawl/run`
- `GET /api/crawl/jobs`
- `GET /api/crawl/candidates`
- `POST /api/crawl/candidates/{id}/decision`
- `GET /api/crawl/pages/{id}/versions`
