# Idol · 设计概要

日期：2026-10-25。阶段：方案讨论完成，作为首版实现基线。

## 1. 已确认的需求与边界

- Windows 个人桌面应用，Electron + Vue 3 + FastAPI，Agent 框架采用 LangGraph。
- 生成模型使用用户配置的 DeepSeek API；设置页提供 API Key、Base URL、模型及连接测试。
- 用户设备为 32 GB RAM + RTX 5080；默认节省资源，不启用 GPU，不常驻大模型。
- 本地 embedding，业务数据、对话、知识原文、向量、执行记录、评测与日报持久化。
- 首次构建仅采集 cn.vuejs.org 的 /guide/ 与 /api/ 文档，不设页面数、队列数和发现深度上限。
- 每次应用启动及北京时间 09:00 检查今日每日任务；今日已成功完成则跳过，否则执行。仅处理今天，不补历史日期。退出应用后不运行后台服务；重新打开仅检查当天。
- 首次构建与每日任务分别记账。初始化需本地模型准备好；首次运行界面明确提示准备模型、初始化知识库。
- 保留用户原始 React 镜像地址；未经调整来源规则，不自动替换为其他域名。
- 自我更新是资料与索引更新，不训练模型，不把模型回答作为事实回灌。

## 2. 技术选型

| 层 | 选型 | 职责 |
| --- | --- | --- |
| 桌面宿主 | Electron / TypeScript | 生命周期、启动 Python 子进程、系统加密密钥、受限 IPC |
| 界面 | Vue 3 / TypeScript / Vite | 聊天、知识库、更新任务、日报、评测、设置 |
| 服务 | FastAPI / Uvicorn 单进程 | 本机鉴权 API、SSE、后台任务 |
| Agent | LangGraph StateGraph + SQLite checkpointer | 条件路由、研究、生成、评审、一次修订 |
| 数据 | SQLite WAL + FTS5 | 权威原文、版本、分块、中文分词全文索引和业务记录 |
| 向量索引 | LanceDB 嵌入式 | 本地持久化、余弦检索；索引可从 SQLite 重建 |
| 向量模型 | FastEmbed / ONNX CPU，多语言 MiniLM | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`，384 维 |
| 采集 | HTTPX + BeautifulSoup / Markdown 提取 | 白名单检查、robots、限速、正文与链接解析 |

选择多语言小模型是中英文资料覆盖与资源之间的折中；默认不加载 BGE-M3 或额外神经 reranker。首版通过 BM25 + 向量 RRF 融合、标题匹配及文档多样性重排优化检索。大模型和神经重排的收益应通过固定测试集再决定。

## 3. 进程与安全边界

Vue → Electron preload 的有限 IPC → Electron 主进程 → 带随机 Bearer token 的 localhost FastAPI。主进程转发 SSE 事件，前端不持有后端访问凭证。生产页面启用 sandbox、contextIsolation，关闭 nodeIntegration，禁止任意导航及任意 IPC；外链仅允许 HTTP(S)。

API Key 用 Electron safeStorage 系统加密后保存于用户数据目录，启动时只传入后端内存；不放 localStorage、SQLite、日志或代码仓库。后端 token 通过子进程环境传递。FastAPI 仅绑定 127.0.0.1。网页正文是不可信数据，不能覆盖系统提示词、抓取策略或执行本地命令。

开发模式可以通过环境变量提供 key；没有 key 时界面与本地知识库仍可使用，聊天明确提示配置，不返回伪造回答。

## 4. 资源预算

- CPU embedding 线程 2，batch size 8，单个模型实例；默认不启用 CUDA。
- 按需加载，空闲 5 分钟释放模型引用与会话；进程可能保留内存分配器缓存，不承诺立即归还全部 RSS。
- 采集单任务、有限并发（首版串行），同域请求间隔至少 1 秒，响应体最多 3 MB，单请求超时 20 秒。
- 后台向量化按小批进行，聊天活跃时暂停推进后台页面；首次构建不设数量上限，可取消并继续。
- LLM 评审仅用于 RAG 回答；最多一次修订。请求有超时，失败显示原因，不无限重试。
- 页面按需读取、限制历史消息上下文和引用总长度，避免将整个知识库送给 LLM。
- 不给出未经本机实测的 CPU/RAM 占用保证；验证记录中报告实际观察与未测项。

## 5. 问答图与评测

问答图：START → classify → [direct → END | rewrite → retrieve → generate → evaluate → (一次 revise → evaluate 或 END)]。

rewrite 节点分别调用一次 LLM，结合历史生成中文和英文检索问题，保留技术名词、版本和约束，不生成预备答案。原问题与两种语言改写分别混合检索，跨查询使用 RRF 融合并去重；单路改写失败时显示提示并使用剩余查询。直接回答不执行这两次调用。

路由采用保守规则与结构化 LLM 判断；问候、改写等简单请求直接回答，具体版本、最新信息、出处及技术分析进入 RAG。用户可强制知识库模式。对话历史参与问题理解；每次请求独立 checkpoint thread，业务会话另存数据库，避免上次引用串入下一轮。

检索研究、回答生成、独立评审为不同提示词角色。评审模型可单独配置；默认复用生成服务，不把同模型自评视为绝对正确性证明。默认以自然段落直接回答，不列证据清单；无召回或资料不完整时允许模型知识补充，并明确区分其与资料结论。无召回时不做依据评测，不因此判定不通过；有资料时评审引用准确性及补充内容的逻辑与不确定性表达，不能把评审视为外部核验。参考来源最多展示三个，保持原始证据编号，历史消息也限制展示数量。

用户评测提示词原样归档，运行版修正：correctness 在有标准答案时以标准答案判断；groundedness 永远对照给定上下文。unsupported_claim 越低越好，其他五项越高越好。每项 0~1，附原因，严格校验 JSON。默认通过线：correctness/groundedness/relevance/completeness >= 0.75，unsupported_claim <= 0.20，conflict_disclosure >= 0.75。阈值为初始工程策略，后续按基准数据调整。

没有上下文的直接回答记录“未进行 RAG 评测”，不伪造分数。保存每次评审的上下文、答案、模型、耗时与修订次数。无效评测输出记为失败，不按通过处理；最终回答可返回质量提示。

## 6. RAG 与知识版本

正文优先定位站点正文容器并转换为 Markdown，保留标题、列表、表格、链接、行内代码和代码块语言及缩进。按标题层级、段落和代码块分块；普通正文优先按句子边界切分，代码优先按行及空行边界切分，每个子块保留代码围栏及语言。使用约 300 token 的估算预算及字符上限，不下载额外 tokenizer；超长单句、代码行最终可硬切分，不保证语法完整。长表格按行拆分并重复表头，单行过长仍受下游上下文上限约束。代码携带相邻短说明和章节标题。保留现有字段，不新增章节锚点或提取器版本等来源字段。

中文分词 + 英文/API token 构建 FTS5 BM25；向量取候选后 RRF 融合，再按标题命中及同文档多样性重排。返回少量高质量片段与相邻上下文。只检索当前有效版本，不混入已替换版本；原始版本保留以便审计。

SQLite 是权威数据；向量索引失败不丢弃原文，索引状态标为待重建，提供重建动作。用稳定 chunk ID 避免重复，内容 hash 防止相同正文重复入库。切换 embedding 模型必须重建索引，不能混合不同向量空间。

## 7. 采集范围与调度

初次种子：

1. `https://cn.vuejs.org/guide/best-practices/production-deployment`，限定 `/guide/`、`/api/`。

从 Vue 种子发现相关章节，不设深度、页面和队列数量限制；持久化队列去重并可续跑。升级时重新发现旧 Vue 页面一次，已有文档去重，历史其他来源内容保留但不继续采集。重定向逐跳检查白名单；不跟随站外链接、不访问私网目的地址，不绕过登录或反爬。不可访问的来源明确记录失败及出处，不冒充完整覆盖。

每日白名单：`juejin.cn`、`zhihu.com`（含明确登记的 `www.zhihu.com`）、`blog.csdn.net`、`cloud.tencent.com/developer`、`tech.meituan.com`，以及启动材料中指定头条账号路径。头条文章仅可从指定账号发现，不能扩散账号范围。

| 主题 | 优先级 | 每日页面访问上限 | 每日新入库 chunk 上限 |
| --- | --- | --- | --- |
| AI：Agent/RAG/向量库/评测/国产模型 | P0 | 20 | 200 |
| 音视频：WebRTC/编码/流媒体/Electron | P1 | 5 | 50 |
| Java 后端：SpringBoot/中间件/架构 | P2 | 5 | 50 |
| 前端：Vue/React/Electron/Uniapp/RN | P3 | 5 | 50 |

社区只接收实践、踩坑、教程，过滤营销、课程导流和面试刷题；官方团队内容独立标记来源类别。发现请求也受总预算限制，访问失败消耗访问预算；配额是上限而非目标。单篇超出剩余 chunk 额度时明确跳过或标记截取，不静默截断替换已有完整版本。

调度时区固定 Asia/Shanghai。启动检查是明确触发点，09:00 定时器不回放昨天任务；系统休眠跨过 09:00 时不补发定时任务。任务以日期和状态防重，应用内只允许一个更新任务。当天失败可在下次启动或手动重试，仍共享当天配额；人工重试不绕过已用额度。昨日中断任务标记 interrupted，不作为今日待补任务。

## 8. 日报

从真实入库变更与任务记录生成 Markdown：主题、标题、内容摘要、来源链接、发布时间（未知则明示）、抓取时间、新增/更新/重复/过滤/失败数量、配额使用、受限来源、任务耗时。可附 LLM 摘要，但事实清单及计数由数据库生成；没有 key 也能产出基础日报。零新增或部分失败仍生成日报并标明状态。

## 9. 数据和接口

数据实体：settings、conversations、messages、documents、document_versions、chunks、FTS、index manifest、update_runs、crawl_queue/items、daily_reports、evaluations，LangGraph checkpoints 使用独立 SQLite 文件。

API（均在 `/api`）：

- `GET /health`：ready、版本；`GET /overview`：统计、今日状态、模型状态。
- `GET/PUT /settings`：非敏感设置和 key 是否已配置；`POST /credentials`：只接受主进程传入内存 key；`POST /models/test`：连接测试。
- `GET/POST /conversations`；`GET /conversations/{id}/messages`。
- `POST /chat`：`{conversation_id?, message, mode: auto|rag}`；SSE 事件 `status`、`token`、`result`、`error`、`done`。result 包含 conversation_id、answer、route、citations、evaluation。
- `GET /knowledge/documents?query=`；`POST /knowledge/search`：`{query}`；`POST /knowledge/reindex`；`POST /knowledge/model/prepare`。
- `GET /updates`；`POST /updates/run`：`{kind: initial|daily}`；`POST /updates/cancel`。
- `GET /reports`；`GET /reports/{id}`；`GET /evaluations`。

## 10. 实施及验收

先生成本文件，再分模块实现桌面壳和页面、持久化与 API、知识采集和调度、LangGraph/RAG/评测，最后集成。

验证重点：同日防重、09:00 边界/跨日不补跑、首次仅 Vue 且超过旧页面/队列/深度上限、每日各主题双配额、白名单与重定向、恶意页面不改变提示词、中文检索、重启持久化、旧版本隔离、评测方向与修订上限、SSE 与 API 鉴权、密钥不回显。使用本地 fixture 与模型替身做确定性测试；真实 DeepSeek 端到端需要用户 key，不将 mock 通过表述为真实模型通过。

交付源码、依赖锁定、启动说明、Windows 打包配置、测试与已知限制。需实际构建和启动验证后才宣称对应环节完成；在线社区可访问性和抓取覆盖不保证。

## 11. 正文采集调整（2026-10-27）

- Vue 指南及 API 改用 `vuejs-translations/docs-zh-cn` 的 Markdown；每次任务获取目录并固定提交读取正文和代码引用，出处保持官网 URL。清理展示组件、前言配置及交互链接，保留中英文、标题层级、表格和完整代码。暂不支持的 Markdown 包含指令明确报错，避免不完整入库。
- 社区采用站点正文选择器与本地 Trafilatura 两级提取，移除已知评论、推荐、导航及广告区域。提取失败明确跳过，不使用整页兜底；代码块以占位方式经过正文筛选，再恢复存活的代码块。
- 不引入常驻浏览器、云端抓取服务或额外 LLM 清洗调用。现有白名单、限速、每日预算、断点队列和日报继续生效；Vue 源码请求只访问固定官方仓库的 GitHub API/raw 地址。
- 下次手动首次构建一次性重新处理已有 Vue 队列。新提取策略不会仅因重建向量索引而应用于旧正文，也不直接清空生产知识库。
- 删除聊天系统提示中的 identity 方法及身份/模型自称约束，保留回答格式、来源使用和 RAG 规则。

## 12. 检索可观测性（2026-10-28）

ChatState 增加版本为 1 的 trace，按节点返回增量并合并到当前运行状态，不使用全局 trace。节点包装器记录每次执行（含再次评测）的耗时和完成/失败/取消状态，通过 SSE trace 事件传递给桌面端；最终消息 metadata 持久化完整记录，异常路径保存已完成的记录。

记录原问题、路由方式、两种语言改写及其失败提示、每查询最多 12 个返回候选的块身份和排名、最多 36 个去重融合候选及选中状态，以及最多 8 个最终上下文（沿用每块 1800 字符上限）。候选列表不重复存储全文；最终上下文展示实际使用的内容。记录不包含模型私有推理、API Key、请求头或原始异常。前端使用文本插值展示文档和查询，不将 trace 当作 HTML。

关键词/向量名次及单查询 RRF 来自原检索过程，不重复执行检索或调用模型。检索结果和降级提示在同一知识库锁范围内取得，避免其他查询覆盖提示。耗时之和是节点累计墙钟时间，不作为端到端响应延迟或模型计费依据。

## 参考

- https://docs.langchain.com/oss/python/langgraph/graph-api
- https://docs.langchain.com/oss/python/langgraph/persistence
- https://qdrant.github.io/fastembed/examples/Supported_Models/
- https://lancedb.github.io/lancedb/python/python/
- https://api-docs.deepseek.com/api/create-chat-completion/
- https://github.com/electron/electron/blob/main/docs/api/safe-storage.md
