# 本地 RAG 编排调试

## 启动

在项目根目录执行（不启动或构建 Electron）：

```powershell
npm.cmd run studio
```

首次启动会从 `backend/.env.example` 创建 `backend/.env`，不会覆盖已有文件。在 `.env` 中填写 `IDOL_API_KEY`，检查服务地址和模型名称，然后重启调试服务器。这个 Key 用于 DeepSeek；不需要解密或复制 Electron 的 credentials.bin。不要把 Key 填进 Studio 的图输入或 configurable 中。`.env` 已被 Git 忽略。

安装调试依赖（当前工作区已安装）：

```powershell
backend/.venv/Scripts/python.exe -m pip install -r backend/requirements-lock.txt -r backend/requirements-studio.txt
```

浏览器打开：

https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024

选择 `idol`，使用 Graph 模式。Studio 是托管网页，图在本地 Agent Server 执行。若网页要求登录，使用自己的 LangSmith 账号；这与模型 API Key 不同。默认关闭 LangSmith tracing 和 CLI analytics，但实际回答仍会调用你配置的模型服务。开发服务器只监听 127.0.0.1，采用本地开发认证模式，不应用于公网部署。

输入示例：

```json
{"query": "Vue 的 ref 和 reactive 有什么区别？", "mode": "rag", "history": []}
```

`query` 必填；`mode` 可为 `auto` 或 `rag`。首次运行可只输入 query。复用同一 thread 时建议明确传入 mode、history；不自动从上次 answer 拼接聊天历史。initialize 会清空上次检索结果、引用、评测与修订计数，生成本次评测 ID。暂停后直接 Resume 则继续现有状态，不会重新初始化。

## 导入实际知识库的副本

调试默认使用 `backend/.data/studio`，不会自动采集或下载模型。空库可以检查路由、改写和模型补充；要调试实际检索，需要复制原文、向量和已下载模型。

先关闭 Idol（包括 npm run dev 的实例），停止 Studio，再运行：

```powershell
backend/.venv/Scripts/python.exe backend/studio_data.py --source "$env:APPDATA\idol-knowledge-workbench\data" --destination "G:\projects\idol1\idol1\backend\.data\studio-knowledge"
```

然后在 `backend/.env` 增加：

```dotenv
IDOL_STUDIO_DATA_DIR=G:/projects/idol1/idol1/backend/.data/studio-knowledge
```

再次 `npm.cmd run studio`。导入使用 SQLite backup 和文件复制，不修改原库，不复制 API Key 或应用图检查点。源 SQLite 中的聊天、设置、评测等其他业务记录也会随快照复制，仅保存在本地副本。目的目录必须不存在；再次导入请换一个新目录名并更新环境变量。务必关闭源应用，保证向量文件与数据库一致。仅复制已有模型，不触发下载；缺少模型时检索会退回关键词。

## 看什么、改哪里

| 节点 | 要观察的状态 | 修改位置 |
|---|---|---|
| initialize | query、history、mode、revision | graph.py `_initialize` |
| classify | route | graph.py `_route` / `rule_route` |
| rewrite | search_queries、warning | graph.py `_rewrite` |
| retrieve | context 中的命中片段 | graph.py `_retrieve`；knowledge.py `search` |
| generate | answer、citations | graph.py `_answer_messages` / `_generate` |
| evaluate | evaluation | evaluation.py 与 graph.py `_evaluate` |
| revise | revision、修订后的 answer | graph.py `_revise` / `_after_evaluate` |

所有节点和连线均复用 `backend/idol/graph.py`，不存在第二份 Studio 专用问答逻辑。调整 add_node / add_edge / add_conditional_edges 后，普通 studio 命令会自动重载。Studio 用于可视化与状态调试，不是拖拽后自动改写源码的编辑器。

在节点前设置 Interrupt，运行后检查 State；编辑 search_queries 可观察查询改写的影响，编辑 context 可观察检索片段对生成的影响。从历史 checkpoint 分支或 Resume 可重复调试后续节点。回放 generate / evaluate / revise 会再次产生模型调用及评测记录。不要同时重复提交许多运行。

当前 LLM 客户端使用 HTTPX，自定义 SSE 回调属于 Electron 通道；Studio 主要查看节点输出和 State，不承诺展示逐 token 消息流或自动拆分每个 HTTP 请求的 token 用量。

## Python 断点

停止普通 Studio 后执行：

```powershell
npm.cmd run studio:debug
```

在 VS Code 中打开项目根目录（含 package.json 的 idol1），安装 Python Debugger 扩展。在运行与调试中选择 `Idol: attach Studio (5678)`，连接后在 `_rewrite`、`_retrieve` 等方法中设置断点，再从 Studio 提交问题。

断点模式关闭自动重载，修改代码后需要重启命令并重新连接。普通模式会自动重载。启动脚本强制 Python UTF-8，避免中文 Windows 上的 GBK 解码错误。

## 数据和资源

- 业务副本：`backend/.data/studio` 或指定目录；不会启动每日采集调度。
- Agent Server 的 threads / checkpoints：`backend/.langgraph_api`，由 Server 自己管理；不会装载应用的 SQLite checkpointer。
- 图工厂按运行创建依赖，运行结束释放本地模型和 HTTP 客户端；连续调试可能重复加载模型，但不会一直占用模型内存。
- 为兼容现有短时 SQLite 同步查询，开发启动启用 `--allow-blocking`；这是单人本地调试配置。
- Ctrl+C 停止服务器。无密钥可查看图，但执行到 initialize 会明确提示配置 Key。

官方参考：[Studio 启动](https://docs.langchain.com/langsmith/quick-start-studio)、[图工厂](https://docs.langchain.com/langsmith/graph-rebuild)、[数据存储](https://docs.langchain.com/langsmith/data-storage-and-privacy)。
