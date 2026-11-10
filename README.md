# Idol · 本地知识工作台

Windows 桌面知识助手：Electron + Vue 3 + FastAPI + LangGraph。简单问题直接回答，复杂问题走混合检索、证据回答和独立评审；知识库按指定来源自动更新并生成有出处的日报。

## 使用

已构建的 Windows 安装程序位于 `release/Idol Setup 0.1.0.exe`（本地构建产物，不提交 Git）。也可以运行 `release/win-unpacked/Idol.exe`。

1. 打开「设置」，填写 DeepSeek API Key、Base URL 和生成模型，保存后测试连接。模型名以服务返回的列表为准；评审模型可单独填写。
2. 点击「准备本地模型」。首次从 Hugging Face 下载约 220 MB 的多语言 MiniLM ONNX 模型；下载成功后只在本机运行。
3. 在「知识更新」点击「开始构建 / 继续」。仅收录 Vue 中文官方文档的指南与 API 章节，不设页面数量、队列数量或发现深度上限。
4. 开始聊天；可以切换「智能路由」或强制「知识库」。展开回答下方的引用和评测，检查依据。
5. 在「每日简报」查看每次更新的内容、出处、失败原因和预算使用。

应用每次启动、以及运行期间北京时间 09:00 检查**今天**的每日任务。今天成功完成后不重复执行；失败可手动重试，继续使用当天剩余额度。退出后不在后台运行，错过的日期不补跑。电脑休眠跨过 09:00 时，不补发已经错过的定时触发；下次启动仍检查当天。

## 本地运行源码

已验证环境：Windows 11 x64、Node.js 24、Python 3.14。使用 PowerShell 时直接调用 `npm.cmd`，无需改变脚本执行策略。

```powershell
python -m venv backend/.venv
.\backend\.venv\Scripts\python.exe -m pip install -r backend/requirements-lock.txt
.\backend\.venv\Scripts\python.exe -m pip install -e backend --no-deps
npm.cmd ci
npm.cmd run dev
```

`requirements-lock.txt` 和 `package-lock.json` 记录本次验证的依赖版本。开发依赖更新时可使用 `pip install -e "./backend[dev]"`，验证后重新生成锁文件。

网络较慢时可以为 pip/npm 临时指定镜像和仓库内缓存；模型下载失败会在设置页明确显示，可在网络恢复后重新准备。支持 Hugging Face 标准 `HF_ENDPOINT` / 代理环境变量，需在启动应用前设置。已经下载的模型不会因普通启动而重新下载。

## 构建 Windows 应用

```powershell
.\backend\.venv\Scripts\python.exe backend/build.py
npm.cmd run dist:win -- --config.electronDist=node_modules/electron/dist
```

后端打包为独立 sidecar，安装后的应用不依赖用户另装 Python。`electronDist` 复用已安装的 Electron，避免重复下载。构建没有配置签名证书，生成的是未签名安装包。

## 静态检查

可视化编排调试：`npm.cmd run studio`；VS Code 断点调试：`npm.cmd run studio:debug`。首次使用请参考 [Studio 调试指南](docs/studio-debugging.md)，配置独立 API Key 和知识库副本。此入口不启动或构建 Electron。

新回答下方的「检索与耗时详情」默认折叠，可查看路由方式、中英文改写、每条查询返回的前 12 个候选及其关键词/向量名次、多查询 RRF 融合排名，以及最终送入模型的上下文。节点耗时为墙钟时间，不含节点间调度与界面传输，融合分不是相关性概率。详情随聊天记录保存在本地；失败或中断也会保留已完成节点记录，旧消息不补生成详情。此功能不增加模型调用，Studio 中也可通过图状态的 `trace` 字段检查结果。

```powershell
backend/.venv/Scripts/python.exe -m ruff check backend/idol
npm.cmd run typecheck
```

测试代码和测试专用依赖已移除；历史验证结果保留在 `docs/verification.md`，不代表当前可重新执行的测试入口。

## 资源与数据

- 本地 embedding 固定使用 CPU，2 线程、batch size 8，空闲 5 分钟释放模型会话。默认不使用 GPU。
- 采集单任务串行执行，同域限速；聊天期间后台在页间暂停。不会启动无限 Agent 循环，最多一次回答修订。
- SQLite 保存对话、原文版本、FTS5、任务、配额、评测和日报；LanceDB 保存可重建向量。旧版原文保留，但不进入当前检索。
- API Key 由 Electron `safeStorage` 系统加密。后端仅绑定 `127.0.0.1`，每次启动生成访问 token；前端不持有该 token，也不读取已保存的密钥明文。
- 正常应用数据位于 Electron 的 `userData` 目录（Windows 通常在 `%APPDATA%/idol-knowledge-workbench`）；其中 `data/` 保存数据库和模型，`credentials.bin` 保存加密密钥。备份请在退出应用后复制整个目录；加密密钥通常只能由原 Windows 用户解密。
- 对话问题和检索片段会发送给你配置的模型 API；本地持久化不意味着生成模型离线。

## 采集规则与当前边界

每日 AI 20 页 / 200 chunks、音视频 5 / 50、Java 5 / 50、前端 5 / 50。发现页、失败请求消耗页面额度；同日重试共享预算。首次构建仅限 Vue 文档，不设累计访问上限；保留去重、限速和手动取消。完整文章超过剩余 chunk 额度时跳过，不截断替换已存在的完整版本。

Vue 首次构建从官方中文文档仓库读取指南和 API 的 Markdown，同一任务固定一个仓库版本，保留代码块并处理源码片段引用。引用出处仍显示 Vue 网站链接；不再抓取 Vue 渲染页面中的导航、复制按钮等界面内容。GitHub API 或源码不可访问时会记录失败，不退回整页 HTML。

社区只访问来源白名单及允许路径，重定向也检查范围，遵守 robots。采用静态 HTML 发现，正文先匹配站点专用容器，再使用本地 Trafilatura 提取；无正文则跳过，不使用整页内容兜底。保留代码语言和缩进，过滤导航、评论、推荐和广告等已知区域。不绕过登录、验证码或动态页面限制；账号属性按公开作者元数据过滤，质量规则是启发式筛选。日报会如实报告无法抓取的来源，不能保证每天收满额度。

升级后重启后端，点击「首次构建」可触发一次已有 Vue 采集队列的重新处理；相同 URL 的新内容按现有版本机制更新。仅「重建知识索引」不会重新抓取或清洗旧正文。社区旧文档需要重新采集后才会使用新的提取结果。

需要从空库开始时，在「知识更新 → 首次构建」点击「清空知识库并重新构建」，确认后清除全部知识文档（包括每日收录）、历史版本、全文/向量索引及采集队列，并自动启动 Vue 首次构建。保留聊天、设置、本地模型、更新历史与日报，以及每日已用额度。须先准备模型，并等待聊天、采集和索引任务结束；重复点击会被拦截。此操作不删除整个 SQLite 文件。

重排采用 BM25 + 向量 RRF、标题匹配与文档多样性；首版不常驻大型神经 reranker。向量索引异常时保留原文，明确提示退化为关键词检索，并可手动重建。

直接回答从服务流式显示；RAG 回答先生成、评审及必要时修订，再展示最终文本，期间展示节点进度。独立评审是角色分工，默认仍可使用同一模型；评分不替代人工判断。

## 代码与设计

- `docs/design-overview.md`：讨论后先生成的设计基线。
- `docs/verification.md`：实际验证结果与限制。
- `desktop/`：Electron 主进程、受限 IPC、密钥与 Python 生命周期。
- `frontend/`：Vue 中文工作台。
- `backend/idol/graph.py`、`evaluation.py`：LangGraph 与评审。
- `backend/idol/knowledge.py`、`embeddings.py`：版本化知识库、混合检索和本地模型。
- `backend/idol/ingestion.py`、`sources.py`、`scheduler.py`：采集、配额、日报和调度。
- `backend/resources/*-original.txt`：用户原始启动规则和评测提示词的归档。
- `C:\Users\Administrator\AppData\Roaming\idol-knowledge-workbench\data`：数据库位置
