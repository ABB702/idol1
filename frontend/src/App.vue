<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, nextTick } from "vue";
import {
  MessageSquare,
  Library,
  RefreshCw,
  FileText,
  SlidersHorizontal,
  ShieldCheck,
  Plus,
  ArrowUp,
  Search,
  ArrowUpRight,
  Sparkles,
  ChevronRight,
  CircleCheck,
  AlertCircle,
  LoaderCircle,
  Square,
  Database,
  Cpu,
  Download,
  BookOpen,
  PanelLeftClose,
  PanelLeftOpen,
} from "lucide-vue-next";
import { marked } from "marked";
import DOMPurify from "dompurify";

type Row = Record<string, any>;
type Page =
  "chat" | "knowledge" | "updates" | "reports" | "evaluations" | "settings";
const nav = [
  { id: "chat", label: "对话", icon: MessageSquare },
  { id: "knowledge", label: "知识库", icon: Library },
  { id: "updates", label: "知识更新", icon: RefreshCw },
  { id: "reports", label: "每日简报", icon: FileText },
  { id: "evaluations", label: "回答评测", icon: ShieldCheck },
  { id: "settings", label: "设置", icon: SlidersHorizontal },
] as const;
const page = ref<Page>("chat"),
  collapsed = ref(false),
  loading = ref(true),
  busy = ref(""),
  error = ref(""),
  notice = ref("");
const overview = ref<Row>({
  documents: 0,
  chunks: 0,
  conversations: 0,
  model: { ready: false, loaded: false },
});
const settings = ref<Row>({
  base_url: "https://api.deepseek.com",
  model: "deepseek-flash",
  evaluation_model: "",
  auto_update: true,
});
const apiKey = ref(""),
  models = ref<string[]>([]),
  conversations = ref<Row[]>([]),
  messages = ref<Row[]>([]),
  conversationId = ref<string | null>(null);
const prompt = ref(""),
  mode = ref<"auto" | "rag">("auto"),
  generating = ref(false),
  streamId = ref(""),
  stage = ref(""),
  messageList = ref<HTMLElement | null>(null);
const documents = ref<Row[]>([]),
  results = ref<Row[]>([]),
  search = ref(""),
  searched = ref(false),
  updates = ref<Row[]>([]),
  reports = ref<Row[]>([]),
  evaluations = ref<Row[]>([]),
  selectedReport = ref<Row | null>(null);
const bridge = window.idol;
const confirmingReset = ref(false);
const resetDisabled = computed(
  () =>
    !!busy.value ||
    !!activeUpdate.value ||
    operationRunning.value ||
    generating.value ||
    !overview.value.model?.ready,
);
let interval: ReturnType<typeof setInterval> | undefined,
  offChat: (() => void) | undefined,
  offBackend: (() => void) | undefined,
  toastTimer: ReturnType<typeof setTimeout> | undefined;
const title = computed(
  () => nav.find((item) => item.id === page.value)?.label ?? "对话",
);
const activeUpdate = computed(
  () =>
    overview.value.active_update ||
    updates.value.find((item) =>
      ["running", "pending", "cancelling"].includes(item.status),
    ),
);
const greeting = computed(() => {
  const hour = Number(
    new Intl.DateTimeFormat("zh-CN", {
      hour: "numeric",
      hour12: false,
      timeZone: "Asia/Shanghai",
    }).format(new Date()),
  );
  return hour < 12 ? "上午好" : hour < 18 ? "下午好" : "晚上好";
});
const operationRunning = computed(
  () => overview.value.operation?.status === "running",
);
const modelState = computed(() =>
  overview.value.operation?.name === "prepare_model" && operationRunning.value
    ? "准备中…"
    : overview.value.model?.ready
      ? overview.value.model?.loaded
        ? "正在使用 · CPU"
        : "已就绪 · 按需加载"
      : "尚未准备",
);
const labelMap: Record<string, string> = {
  success: "已完成",
  completed: "已完成",
  succeeded: "已完成",
  running: "进行中",
  pending: "等待中",
  failed: "失败",
  partial: "部分完成",
  cancelled: "已取消",
  interrupted: "已中断",
  skipped: "已跳过",
  initial: "首次构建",
  daily: "每日更新",
  rag: "知识库检索",
  direct: "直接回答",
  passed: "通过",
  error: "错误",
};
const scoreLabels: Record<string, string> = {
  correctness: "正确性",
  groundedness: "证据扎根度",
  relevance: "相关性",
  completeness: "完整性",
  unsupported_claim: "无依据陈述 ↓",
  conflict_disclosure: "冲突披露",
};
function label(value: unknown) {
  return labelMap[String(value)] ?? String(value ?? "—");
}
function date(value: unknown) {
  if (!value) return "—";
  const parsed = new Date(String(value));
  return Number.isNaN(parsed.valueOf())
    ? String(value)
    : parsed.toLocaleString("zh-CN", {
        timeZone: "Asia/Shanghai",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      });
}
function markdown(value: unknown) {
  return DOMPurify.sanitize(
    marked.parse(String(value ?? ""), { async: false }) as string,
    {
      FORBID_TAGS: ["img", "iframe", "style", "form"],
      FORBID_ATTR: ["style", "target"],
    },
  );
}
function tell(text: string) {
  notice.value = text;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (notice.value = ""), 5000);
}
function fail(value: unknown) {
  error.value = value instanceof Error ? value.message : String(value);
}
async function api(method: string, path: string, body?: unknown) {
  if (!bridge)
    throw new Error(
      "请通过 Electron 启动应用（npm run dev），浏览器预览无法连接本地工作台。",
    );
  return bridge.request(method, path, body);
}
async function safe(name: string, fn: () => Promise<void>) {
  if (busy.value) return;
  busy.value = name;
  error.value = "";
  try {
    await fn();
  } catch (e) {
    fail(e);
  } finally {
    busy.value = "";
  }
}
async function loadOverview() {
  overview.value = await api("GET", "/overview");
}
async function loadConversations() {
  conversations.value = (await api("GET", "/conversations")).items ?? [];
}
async function refreshPage() {
  if (page.value === "knowledge")
    documents.value = (await api("GET", "/knowledge/documents")).items ?? [];
  if (page.value === "updates")
    updates.value = (await api("GET", "/updates")).items ?? [];
  if (page.value === "reports")
    reports.value = (await api("GET", "/reports")).items ?? [];
  if (page.value === "evaluations")
    evaluations.value = ((await api("GET", "/evaluations")).items ?? []).map(
      (item: Row) => ({ ...item, ...parseJSON(item.result, {}) }),
    );
  if (page.value === "settings") settings.value = await api("GET", "/settings");
}
async function go(target: Page) {
  page.value = target;
  error.value = "";
  try {
    await refreshPage();
  } catch (e) {
    fail(e);
  }
}
async function initialize() {
  loading.value = true;
  error.value = "";
  try {
    await Promise.all([
      loadOverview(),
      loadConversations(),
      api("GET", "/settings").then((value) => (settings.value = value)),
    ]);
    await refreshPage();
  } catch (e) {
    fail(e);
  } finally {
    loading.value = false;
  }
}
async function newChat() {
  if (generating.value) return;
  conversationId.value = null;
  messages.value = [];
  page.value = "chat";
  prompt.value = "";
  error.value = "";
}
async function selectConversation(item: Row) {
  if (generating.value) return;
  await safe("history", async () => {
    const data = await api(
      "GET",
      `/conversations/${encodeURIComponent(item.id)}/messages`,
    );
    messages.value = (data.items ?? []).map((message: Row) => {
      const metadata = parseJSON(message.metadata, {});
      return {
        ...message,
        ...metadata,
        citations: parseJSON(metadata.citations ?? message.citations, []),
        evaluation: parseJSON(metadata.evaluation ?? message.evaluation, null),
      };
    });
    conversationId.value = item.id;
    page.value = "chat";
    await scrollBottom();
  });
}
function parseJSON(value: any, fallback: any) {
  if (typeof value !== "string") return value ?? fallback;
  try {
    return JSON.parse(value);
  } catch {
    return fallback;
  }
}
async function scrollBottom() {
  await nextTick();
  if (messageList.value)
    messageList.value.scrollTop = messageList.value.scrollHeight;
}
async function send() {
  if (!prompt.value.trim() || generating.value || loading.value) return;
  if (!settings.value.api_key_configured) {
    error.value = "请先到设置中保存 API Key，再开始对话。";
    return;
  }
  const text = prompt.value.trim();
  prompt.value = "";
  error.value = "";
  generating.value = true;
  stage.value = "正在理解问题";
  streamId.value = crypto.randomUUID();
  messages.value.push(
    { role: "user", content: text },
    { role: "assistant", content: "", citations: [], pending: true },
  );
  await scrollBottom();
  try {
    await bridge.startChat(streamId.value, {
      conversation_id: conversationId.value,
      message: text,
      mode: mode.value,
    });
  } catch (e) {
    fail(e);
    generating.value = false;
    messages.value[messages.value.length - 1].pending = false;
  }
}
function handleChat(event: { id: string; type: string; data: Row }) {
  if (event.id !== streamId.value) return;
  const message = messages.value[messages.value.length - 1];
  if (event.type === "trace") message.trace = event.data;
  if (event.type === "status") {
    stage.value = event.data.message ?? label(event.data.stage);
    if (event.data.conversation_id)
      conversationId.value = event.data.conversation_id;
  }
  if (event.type === "token") {
    message.content = (message.content ?? "") + (event.data.text ?? "");
    void scrollBottom();
  }
  if (event.type === "result") {
    conversationId.value = event.data.conversation_id;
    Object.assign(message, {
      content: event.data.answer,
      route: event.data.route,
      citations: event.data.citations ?? [],
      evaluation: event.data.evaluation,
      warning: event.data.warning,
      trace: event.data.trace,
      pending: false,
    });
    void scrollBottom();
  }
  if (event.type === "error") {
    message.error = event.data.message;
    message.pending = false;
    fail(event.data.message);
  }
  if (event.type === "done") {
    generating.value = false;
    message.pending = false;
    stage.value = "";
    void Promise.all([loadConversations(), loadOverview()]).catch(fail);
  }
}
async function stop() {
  try {
    await bridge.cancelChat(streamId.value);
  } catch (e) {
    fail(e);
  }
}
function keydown(event: KeyboardEvent) {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    void send();
  }
}
async function open(url: unknown) {
  if (!url) return;
  try {
    await bridge.openExternal(String(url));
  } catch (e) {
    fail(e);
  }
}
function markdownClick(event: MouseEvent) {
  const link = (event.target as HTMLElement).closest("a");
  if (link) {
    event.preventDefault();
    void open(link.getAttribute("href"));
  }
}
async function doSearch() {
  if (!search.value.trim()) return;
  await safe("search", async () => {
    const data = await api("POST", "/knowledge/search", {
      query: search.value.trim(),
    });
    results.value = data.items ?? [];
    searched.value = true;
  });
}
async function run(kind: "initial" | "daily") {
  await safe(kind, async () => {
    const result = await api("POST", "/updates/run", { kind });
    tell(result.message ?? "更新任务已提交，可在下方查看进度。");
    await loadOverview();
    updates.value = (await api("GET", "/updates")).items ?? [];
  });
}
async function cancelUpdate() {
  await safe("cancel", async () => {
    await api("POST", "/updates/cancel");
    tell("已请求取消，正在完成当前步骤。");
    await loadOverview();
    await refreshPage();
  });
}
async function prepare() {
  await safe("prepare", async () => {
    const data = await api("POST", "/knowledge/model/prepare");
    tell(data.message ?? "本地模型准备已开始。首次使用需要下载模型文件。");
    await loadOverview();
  });
}
async function reindex() {
  await safe("reindex", async () => {
    const data = await api("POST", "/knowledge/reindex");
    tell(data.message ?? "索引重建已提交。原始文档将保留。");
    await loadOverview();
  });
}
async function resetKnowledge() {
  if (resetDisabled.value) return;
  await safe("reset", async () => {
    await api("POST", "/knowledge/reset");
    confirmingReset.value = false;
    documents.value = [];
    results.value = [];
    searched.value = false;
    tell("正在清空知识库，完成后自动开始首次构建。请在下方查看进度。");
    await loadOverview();
  });
}
async function readReport(item: Row) {
  await safe(
    "report",
    async () =>
      (selectedReport.value = await api(
        "GET",
        `/reports/${encodeURIComponent(item.id)}`,
      )),
  );
}
async function saveSettings() {
  await safe("save", async () => {
    await api("PUT", "/settings", {
      base_url: settings.value.base_url,
      model: settings.value.model,
      evaluation_model: settings.value.evaluation_model || settings.value.model,
      auto_update: settings.value.auto_update,
    });
    if (apiKey.value.trim()) {
      await bridge.saveKey(apiKey.value);
      apiKey.value = "";
    }
    settings.value = await api("GET", "/settings");
    tell("设置已保存。");
  });
}
async function clearKey() {
  await safe("clear-key", async () => {
    await bridge.saveKey("");
    apiKey.value = "";
    settings.value = await api("GET", "/settings");
    tell("已清除保存的 API Key。");
  });
}
async function testConnection() {
  await safe("test", async () => {
    const result = await api("POST", "/models/test");
    models.value = (result.models ?? []).map((item: any) =>
      typeof item === "string" ? item : item.id,
    );
    if (!result.ok) throw new Error(result.message ?? "连接失败");
    tell(result.message ?? "连接正常，模型列表已刷新。");
  });
}
function evaluationStatus(value: any) {
  if (!value) return "未进行 RAG 评测";
  if (value.status === "failed" || value.error) return "评测失败";
  if (value.passed === true || value.status === "passed") return "评测通过";
  if (value.passed === false) return "存在待核实内容";
  return label(value.status ?? "已评测");
}
function scores(value: Row) {
  const data = parseJSON(
    value.scores ?? value.metrics ?? value.result ?? value,
    {},
  );
  return Object.entries(scoreLabels)
    .filter(([key]) => data[key] !== undefined)
    .map(([key, name]) => {
      const metric = data[key];
      return {
        key,
        name,
        score: typeof metric === "number" ? metric : Number(metric?.score ?? 0),
        reason:
          data[key + "_reason"] ?? metric?.reason ?? metric?.explanation ?? "",
      };
    });
}
onMounted(() => {
  if (bridge) {
    offChat = bridge.onChat(handleChat);
    offBackend = bridge.onBackendError((message) => (error.value = message));
  }
  void initialize();
  interval = setInterval(() => {
    if (document.hidden || loading.value || busy.value) return;
    void loadOverview()
      .then(async () => {
        if (page.value === "updates" || page.value === "reports")
          await refreshPage();
      })
      .catch(() => {});
  }, 10000);
});
onUnmounted(() => {
  if (interval) clearInterval(interval);
  if (toastTimer) clearTimeout(toastTimer);
  offChat?.();
  offBackend?.();
});
</script>

<template>
  <div class="workbench" :class="{ collapsed }">
    <aside class="sidebar">
      <button class="brand" aria-label="Idol 首页" @click="go('chat')">
        <span class="brand-symbol">i<span>·</span></span
        ><span class="brand-name">idol<span>你的知识，持续生长</span></span>
      </button>
      <button class="new-chat" :disabled="generating" @click="newChat">
        <Plus :size="17" /><span>新建对话</span><span class="key-hint">N</span>
      </button>
      <nav aria-label="主导航">
        <button
          v-for="item in nav"
          :key="item.id"
          :class="{ active: page === item.id }"
          :title="item.label"
          @click="go(item.id)"
        >
          <component :is="item.icon" :size="18" /><span>{{ item.label }}</span
          ><span
            v-if="item.id === 'updates' && activeUpdate"
            class="live-dot"
          />
        </button>
      </nav>
      <div class="history" v-if="!collapsed">
        <div class="section-caption">
          最近对话 <span>{{ conversations.length }}</span>
        </div>
        <button
          v-for="item in conversations.slice(0, 12)"
          :key="item.id"
          :class="{ selected: conversationId === item.id && page === 'chat' }"
          :disabled="generating"
          @click="selectConversation(item)"
        >
          <MessageSquare :size="13" /><span>{{
            item.title || "未命名对话"
          }}</span>
        </button>
        <p v-if="!conversations.length" class="history-empty">
          从一个问题开始。
        </p>
      </div>
      <div class="sidebar-bottom">
        <div class="local-status">
          <span class="status-orb" />
          <div>
            本地优先<span
              >{{ overview.documents ?? 0 }} 篇文档 ·
              {{ overview.chunks ?? 0 }} 个片段</span
            >
          </div>
        </div>
        <button
          class="collapse-button"
          @click="collapsed = !collapsed"
          :aria-label="collapsed ? '展开侧栏' : '收起侧栏'"
        >
          <PanelLeftOpen v-if="collapsed" :size="18" /><PanelLeftClose
            v-else
            :size="18"
          />
        </button>
      </div>
    </aside>
    <main class="main">
      <header class="topbar">
        <div class="breadcrumb">
          工作空间 <ChevronRight :size="14" /><strong>{{ title }}</strong>
        </div>
        <div class="top-status">
          <span v-if="activeUpdate" class="updating"
            ><LoaderCircle class="spin" :size="13" /> 知识更新中</span
          ><span class="privacy-pill"
            ><span class="tiny-dot" /> 本地数据 · 私有空间</span
          >
        </div>
      </header>
      <div v-if="error" class="error-banner" role="alert">
        <AlertCircle :size="18" /><span>{{ error }}</span
        ><button @click="error = ''">关闭</button>
      </div>
      <div
        v-if="overview.operation?.status === 'failed'"
        class="error-banner"
        role="alert"
      >
        <AlertCircle :size="18" /><span>{{ overview.operation.message }}</span>
      </div>
      <div v-if="overview.model?.index_error" class="error-banner">
        <AlertCircle :size="18" /><span>{{ overview.model.index_error }}</span>
      </div>
      <div v-if="notice" class="toast" role="status">
        <CircleCheck :size="17" />{{ notice }}
      </div>
      <div v-if="loading" class="loading-screen">
        <LoaderCircle class="spin" :size="26" />
        <p>正在连接本地工作空间…</p>
      </div>

      <section v-else-if="page === 'chat'" class="chat-page">
        <div ref="messageList" class="conversation-scroll">
          <div v-if="!messages.length" class="welcome">
            <div class="eyebrow">
              <span class="tiny-dot" /> PERSONAL KNOWLEDGE WORKSPACE
            </div>
            <h1>{{ greeting }}，<br />让知识成为你的下一步。</h1>
            <p class="welcome-copy">
              从一个简单的问题开始，或深入你的知识库。<br />Idol
              会找到线索，连接知识，并给出有出处的回答。
            </p>
            <div class="overview-cards">
              <div>
                <BookOpen :size="18" /><strong
                  >{{ overview.documents ?? 0 }}<small>篇</small></strong
                ><span>已收录文档</span>
              </div>
              <div>
                <Database :size="18" /><strong
                  >{{ overview.chunks ?? 0 }}<small>个</small></strong
                ><span>可检索知识片段</span>
              </div>
              <div>
                <RefreshCw :size="18" /><strong class="text-stat">{{
                  activeUpdate
                    ? "更新中"
                    : overview.today_update
                      ? label(
                          typeof overview.today_update === "object"
                            ? overview.today_update.status
                            : overview.today_update,
                        )
                      : "等待检查"
                }}</strong
                ><span>今日知识更新</span>
              </div>
            </div>
            <div
              v-if="!settings.api_key_configured || !overview.model?.ready"
              class="setup-card"
            >
              <div class="setup-icon"><Sparkles :size="19" /></div>
              <div>
                <strong>准备好你的知识工作空间</strong>
                <p>
                  {{
                    !settings.api_key_configured
                      ? "配置 DeepSeek API Key，开启对话。"
                      : "下载轻量本地模型，再首次构建知识库。"
                  }}
                  本地检索默认使用 CPU。
                </p>
              </div>
              <button class="text-button" @click="go('settings')">
                开始配置 <ArrowUpRight :size="16" />
              </button>
            </div>
            <div class="suggestion-label">试着问问</div>
            <div class="suggestions">
              <button @click="prompt = '你好，介绍一下你可以怎样帮助我。'">
                <MessageSquare :size="17" /><span>了解我的知识助手</span
                ><ArrowUpRight :size="15" /></button
              ><button
                @click="
                  prompt =
                    '结合知识库，解释 RAG 检索质量如何优化，并列出资料出处。';
                  mode = 'rag';
                "
              >
                <Library :size="17" /><span>探索 RAG 检索优化</span
                ><ArrowUpRight :size="15" />
              </button>
            </div>
          </div>
          <div v-else class="messages">
            <article
              v-for="(message, index) in messages"
              :key="message.id ?? index"
              class="message"
              :class="message.role"
            >
              <div class="message-avatar">
                {{ message.role === "user" ? "你" : "i·" }}
              </div>
              <div class="message-body">
                <div class="message-heading">
                  <strong>{{ message.role === "user" ? "你" : "Idol" }}</strong
                  ><span v-if="message.route" class="tag">{{
                    label(message.route)
                  }}</span>
                </div>
                <div v-if="message.role === 'user'" class="user-text">
                  {{ message.content }}
                </div>
                <div
                  v-else-if="message.content"
                  class="prose"
                  v-html="markdown(message.content)"
                  @click="markdownClick"
                />
                <div v-if="message.pending && generating" class="thinking">
                  <LoaderCircle class="spin" :size="15" />{{
                    stage || "正在生成回答"
                  }}
                </div>
                <p v-if="message.error" class="message-error">
                  {{ message.error }}
                </p>
                <p v-if="message.warning" class="direct-note">
                  {{ message.warning }}
                </p>
                <details v-if="message.citations?.length" class="citations">
                  <summary>
                    <BookOpen :size="14" />
                    {{ Math.min(message.citations.length, 3) }} 个参考来源
                  </summary>
                  <button
                    v-for="(citation, n) in message.citations.slice(0, 3)"
                    :key="citation.id ?? n"
                    @click="open(citation.url)"
                  >
                    <span class="citation-number">{{
                      citation.number ?? Number(n) + 1
                    }}</span
                    ><span
                      ><strong>{{ citation.title || citation.url }}</strong
                      ><small>{{ citation.text?.slice(0, 180) }}</small></span
                    ><ArrowUpRight :size="14" />
                  </button>
                </details>
                <details
                  v-if="
                    message.evaluation &&
                    message.evaluation.status !== 'not_applicable'
                  "
                  class="evaluation-detail"
                >
                  <summary>
                    <ShieldCheck :size="14" />{{
                      evaluationStatus(message.evaluation)
                    }}
                  </summary>
                  <div class="score-grid">
                    <div
                      v-for="metric in scores(message.evaluation)"
                      :key="metric.key"
                    >
                      <span>{{ metric.name }}</span
                      ><strong>{{ metric.score.toFixed(2) }}</strong
                      ><small>{{ metric.reason }}</small>
                    </div>
                  </div>
                  <p v-if="message.evaluation.error">
                    {{ message.evaluation.error }}
                  </p>
                </details>
                <span v-else-if="message.route === 'direct'" class="direct-note"
                  >直接回答 · 未进行 RAG 评测</span
                >
                <span
                  v-else-if="message.evaluation?.scope === 'model_knowledge'"
                  class="direct-note"
                  >模型知识回答 · 未经知识库验证</span
                >
                <details v-if="message.trace" class="retrieval-detail">
                  <summary>
                    检索与耗时详情 · {{ message.trace.node_total_ms ?? 0 }} ms
                  </summary>
                  <p>
                    路线：{{
                      message.trace.route === "rag"
                        ? "知识库检索"
                        : message.trace.route === "direct"
                          ? "直接回答"
                          : "分析中"
                    }}
                    · 决策方式：{{
                      (
                        {
                          manual: "手动指定",
                          rule: "本地规则",
                          llm: "模型判断",
                        } as Row
                      )[message.trace.route_source] ?? "—"
                    }}
                  </p>
                  <p>
                    耗时为各节点墙钟时间之和，不含节点间调度与界面传输。排名和融合分不是相关性概率。
                  </p>
                  <div class="trace-table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>节点</th>
                          <th>状态</th>
                          <th>耗时（ms）</th>
                        </tr>
                      </thead>
                      <tbody>
                        <tr v-for="(step, i) in message.trace.stages" :key="i">
                          <td>
                            {{
                              (
                                {
                                  initialize: "初始化",
                                  classify: "路由",
                                  direct: "直接生成",
                                  rewrite: "双语改写",
                                  retrieve: "检索",
                                  generate: "生成",
                                  evaluate: "评测",
                                  revise: "修订",
                                } as Row
                              )[step.name] ?? step.name
                            }}
                          </td>
                          <td>{{ label(step.status) }}</td>
                          <td>{{ step.duration_ms }}</td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                  <p>原问题：{{ message.trace.query }}</p>
                  <p
                    v-for="rewrite in message.trace.rewrites"
                    :key="rewrite.language"
                  >
                    {{ rewrite.language }}改写：{{ rewrite.query ?? "未成功" }}
                    <span>{{ rewrite.warning }}</span>
                  </p>
                  <details
                    v-for="(searchItem, i) in message.trace.searches"
                    :key="i"
                  >
                    <summary>
                      查询 {{ Number(i) + 1 }}：{{ searchItem.query }} ·
                      {{ searchItem.duration_ms }} ms ·
                      {{ searchItem.candidates.length }} 个候选
                    </summary>
                    <p v-if="searchItem.warning">{{ searchItem.warning }}</p>
                    <p>
                      展示每条查询返回的前 12
                      个候选；关键词/向量名次来自各自召回列表。
                    </p>
                    <div class="trace-table-wrap">
                      <table>
                        <thead>
                          <tr>
                            <th>名次 / 块</th>
                            <th>标题</th>
                            <th>关键词名次</th>
                            <th>向量名次</th>
                            <th>RRF 分</th>
                          </tr>
                        </thead>
                        <tbody>
                          <tr
                            v-for="candidate in searchItem.candidates"
                            :key="candidate.id"
                          >
                            <td>{{ candidate.rank }} / {{ candidate.id }}</td>
                            <td>
                              {{ candidate.title }} · {{ candidate.heading }}
                            </td>
                            <td>
                              {{ candidate.retrieval?.lexical_rank ?? "—" }}
                            </td>
                            <td>
                              {{ candidate.retrieval?.vector_rank ?? "—" }}
                            </td>
                            <td>
                              {{
                                candidate.retrieval?.rrf_score?.toFixed(6) ??
                                "—"
                              }}
                            </td>
                          </tr>
                        </tbody>
                      </table>
                    </div>
                  </details>
                  <details v-if="message.trace.fusion?.length">
                    <summary>
                      多查询 RRF 融合 ·
                      {{ message.trace.fusion.length }} 个去重候选
                    </summary>
                    <p
                      v-for="candidate in message.trace.fusion"
                      :key="candidate.id"
                    >
                      #{{ candidate.rank }} · 块 {{ candidate.id }} ·
                      {{ candidate.title }} · {{ candidate.score.toFixed(6) }} ·
                      {{ candidate.selected ? "已选入上下文" : "未选入" }}
                    </p>
                  </details>
                  <p
                    v-if="
                      message.trace.searches && !message.trace.context?.length
                    "
                  >
                    没有检索到可用上下文；如有降级原因，见上方查询详情。
                  </p>
                  <details
                    v-for="(context, i) in message.trace.context"
                    :key="context.id"
                  >
                    <summary>
                      最终上下文 {{ Number(i) + 1 }} · {{ context.title }} · 块
                      {{ context.id }}
                    </summary>
                    <button class="text-button" @click="open(context.url)">
                      查看原文
                    </button>
                    <pre>{{ context.text }}</pre>
                  </details>
                </details>
              </div>
            </article>
          </div>
        </div>
        <div class="composer-wrap">
          <div class="composer">
            <textarea
              v-model="prompt"
              placeholder="提出问题，让知识连接起来…"
              aria-label="消息内容"
              rows="2"
              :disabled="loading"
              @keydown="keydown"
            />
            <div class="composer-footer">
              <div class="mode-switch" aria-label="回答模式">
                <button
                  :class="{ selected: mode === 'auto' }"
                  @click="mode = 'auto'"
                >
                  <Sparkles :size="14" />智能路由</button
                ><button
                  :class="{ selected: mode === 'rag' }"
                  @click="mode = 'rag'"
                >
                  <Library :size="14" />知识库
                </button>
              </div>
              <span class="composer-hint">Shift + Enter 换行</span
              ><button
                v-if="generating"
                class="send-button stop"
                aria-label="停止生成"
                @click="stop"
              >
                <Square :size="16" /></button
              ><button
                v-else
                class="send-button"
                :disabled="!prompt.trim()"
                aria-label="发送消息"
                @click="send"
              >
                <ArrowUp :size="20" />
              </button>
            </div>
          </div>
          <p class="composer-note">
            回答由 AI 生成，请结合引用来源核实重要信息。
          </p>
        </div>
      </section>

      <section v-else class="content-page">
        <div class="page-heading">
          <div>
            <div class="eyebrow">YOUR KNOWLEDGE, CONNECTED</div>
            <h1>{{ title }}</h1>
            <p>
              {{
                page === "knowledge"
                  ? "让每一次检索，都有迹可循。"
                  : page === "updates"
                    ? "从可信来源持续学习，所有变化透明可见。"
                    : page === "reports"
                      ? "今天学到了什么，从这里一目了然。"
                      : page === "evaluations"
                        ? "检查回答的证据与质量，让迭代有据可依。"
                        : "按你的方式，连接模型与本地知识。"
              }}
            </p>
          </div>
          <button
            class="secondary-button"
            :disabled="!!busy"
            @click="
              safe('refresh', async () => {
                await loadOverview();
                await refreshPage();
              })
            "
          >
            <RefreshCw :size="15" />刷新
          </button>
        </div>

        <template v-if="page === 'knowledge'">
          <div class="metric-row">
            <div>
              <span>收录文档</span
              ><strong>{{ overview.documents ?? 0 }}</strong>
            </div>
            <div>
              <span>知识片段</span><strong>{{ overview.chunks ?? 0 }}</strong>
            </div>
            <div>
              <span>本地向量模型</span
              ><strong class="small-stat">{{ modelState }}</strong>
            </div>
          </div>
          <form class="searchbar" @submit.prevent="doSearch">
            <Search :size="19" /><input
              v-model="search"
              placeholder="用自然语言搜索，或输入 API、关键词…"
              aria-label="搜索知识库"
            /><button
              class="primary-button"
              :disabled="!!busy || !search.trim()"
            >
              {{ busy === "search" ? "检索中…" : "混合检索" }}
            </button>
          </form>
          <div class="list-heading">
            <h2>{{ searched ? "检索结果" : "最近收录" }}</h2>
            <button
              v-if="searched"
              class="text-button"
              @click="
                searched = false;
                search = '';
              "
            >
              返回文档列表</button
            ><span v-else>关键词 + 语义 · RRF 融合</span>
          </div>
          <div
            class="document-list"
            v-if="(searched ? results : documents).length"
          >
            <article
              v-for="(doc, index) in searched ? results : documents"
              :key="doc.id ?? index"
              class="document-card"
            >
              <div class="document-icon"><FileText :size="21" /></div>
              <div>
                <div class="document-meta">
                  <span class="tag">{{
                    doc.topic || doc.source_type || "技术文档"
                  }}</span
                  ><span>{{
                    date(doc.updated_at || doc.fetched_at || doc.created_at)
                  }}</span
                  ><span v-if="doc.score !== undefined"
                    >相关分 {{ Number(doc.score).toFixed(3) }}</span
                  >
                </div>
                <button class="document-title" @click="open(doc.url)">
                  {{ doc.title || "未命名文档" }}<ArrowUpRight :size="15" />
                </button>
                <p v-if="doc.text || doc.content || doc.summary">
                  {{ (doc.text || doc.content || doc.summary).slice(0, 360) }}
                </p>
                <button class="source-url" @click="open(doc.url)">
                  {{ doc.url }}
                </button>
              </div>
            </article>
          </div>
          <div v-else class="empty-state">
            <Library :size="35" />
            <h3>
              {{ searched ? "没有找到匹配的知识" : "知识库等待第一次生长" }}
            </h3>
            <p>
              {{
                searched
                  ? "试试更具体的关键词，或检查知识库是否已建立。"
                  : "先在设置中准备本地模型，然后运行首次构建。仅收录 Vue 文档，不设页面数量上限。"
              }}
            </p>
            <button
              v-if="!searched"
              class="secondary-button"
              @click="go('updates')"
            >
              前往知识更新 <ArrowUpRight :size="15" />
            </button>
          </div>
        </template>

        <template v-if="page === 'updates'">
          <div class="schedule-banner">
            <div class="schedule-icon"><RefreshCw :size="22" /></div>
            <div>
              <strong>启动时检查 · 每日 09:00 检查</strong>
              <p>
                北京时间，仅更新今天。今日已完成则跳过，错过的历史日期不补跑。
              </p>
            </div>
            <span class="tag" :class="{ green: settings.auto_update }">{{
              settings.auto_update ? "自动更新已开启" : "自动更新已关闭"
            }}</span>
          </div>
          <div class="action-cards">
            <div class="panel">
              <span class="card-index">01 / FOUNDATION</span>
              <h2>首次构建</h2>
              <p>仅从 Vue 中文官方文档出发，收录指南与 API 相关章节。</p>
              <div class="action-card-footer">
                <span>仅 Vue · 页数不限</span
                ><button
                  class="primary-button"
                  :disabled="
                    !!busy ||
                    !!activeUpdate ||
                    operationRunning ||
                    !overview.model?.ready
                  "
                  @click="run('initial')"
                >
                  <Plus :size="15" />开始构建 / 继续
                </button>
              </div>
              <div class="action-card-footer">
                <button
                  class="text-button danger"
                  :disabled="resetDisabled"
                  @click="confirmingReset = !confirmingReset"
                >
                  清空知识库并重新构建
                </button>
              </div>
              <div v-if="confirmingReset" role="alert">
                <p>
                  将永久删除全部知识库文档（含每日收录）、历史版本、索引和采集队列，随后重新采集
                  Vue 文档。聊天记录、设置、本地模型、日报及每日已用额度保留。
                </p>
                <div class="action-card-footer">
                  <button
                    class="secondary-button"
                    :disabled="!!busy"
                    @click="confirmingReset = false"
                  >
                    取消
                  </button>
                  <button
                    class="secondary-button danger"
                    :disabled="resetDisabled"
                    @click="resetKnowledge"
                  >
                    确认清空并重建
                  </button>
                </div>
              </div>
            </div>
            <div class="panel">
              <span class="card-index">02 / DAILY DISCOVERY</span>
              <h2>今日更新</h2>
              <p>
                关注 AI、音视频、Java 后端与前端。从白名单来源筛选实用技术内容。
              </p>
              <div class="action-card-footer">
                <span>每日上限 35 页 / 350 片段</span
                ><button
                  class="secondary-button"
                  :disabled="
                    !!busy ||
                    !!activeUpdate ||
                    operationRunning ||
                    !overview.model?.ready
                  "
                  @click="run('daily')"
                >
                  <RefreshCw :size="15" />检查今日
                </button>
              </div>
            </div>
          </div>
          <p v-if="!overview.model?.ready" class="inline-help">
            <AlertCircle :size="15" /> 请先到设置中准备本地向量模型。<button
              class="text-button"
              @click="go('settings')"
            >
              前往设置
            </button>
          </p>
          <div v-if="activeUpdate" class="active-job">
            <LoaderCircle class="spin" :size="19" />
            <div>
              <strong>{{ label(activeUpdate.kind) }}正在进行</strong
              ><span>{{
                activeUpdate.message ||
                activeUpdate.stage ||
                "任务在后台运行，你可以继续使用工作台。"
              }}</span>
            </div>
            <button
              class="secondary-button"
              :disabled="!!busy"
              @click="cancelUpdate"
            >
              取消任务
            </button>
          </div>
          <div class="list-heading">
            <h2>执行记录</h2>
            <span>成功、跳过和失败均保留记录</span>
          </div>
          <div v-if="updates.length" class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>任务</th>
                  <th>状态</th>
                  <th>开始时间</th>
                  <th>新增 / 更新</th>
                  <th>详情</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="run in updates" :key="run.id">
                  <td>
                    <strong>{{ label(run.kind) }}</strong
                    ><small>{{ run.date || run.run_date || run.day }}</small>
                  </td>
                  <td>
                    <span
                      class="tag"
                      :class="{
                        green: ['success', 'completed', 'succeeded'].includes(
                          run.status,
                        ),
                        amber: run.status === 'running',
                        red: run.status === 'failed',
                      }"
                      >{{ label(run.status) }}</span
                    >
                  </td>
                  <td>{{ date(run.started_at || run.created_at) }}</td>
                  <td>
                    {{
                      run.added ?? run.new_documents ?? run.stats?.added ?? "—"
                    }}
                    /
                    {{
                      run.updated ??
                      run.updated_documents ??
                      run.stats?.updated ??
                      "—"
                    }}
                  </td>
                  <td class="task-detail">
                    {{ run.message || run.error || run.summary || "—" }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-else class="empty-state compact">
            <RefreshCw :size="30" />
            <h3>还没有执行记录</h3>
            <p>开始构建后，进度与采集结果会显示在这里。</p>
          </div>
        </template>

        <template v-if="page === 'reports'">
          <div v-if="reports.length" class="reports-layout">
            <div class="report-list">
              <button
                v-for="report in reports"
                :key="report.id"
                :class="{ selected: selectedReport?.id === report.id }"
                @click="readReport(report)"
              >
                <div class="report-date">
                  <FileText :size="18" /><strong>{{
                    report.date ||
                    report.report_date ||
                    report.day ||
                    date(report.created_at)
                  }}</strong>
                </div>
                <span>{{ report.title || "知识更新日报" }}</span
                ><small
                  >{{ label(report.status) }} <ChevronRight :size="13"
                /></small>
              </button>
            </div>
            <article class="report-reader">
              <div
                v-if="selectedReport"
                class="prose"
                v-html="
                  markdown(selectedReport.markdown || selectedReport.content)
                "
                @click="markdownClick"
              />
              <div v-else class="empty-state">
                <BookOpen :size="32" />
                <h3>选择一份日报</h3>
                <p>查看新增知识、更新摘要与每条内容的出处。</p>
              </div>
            </article>
          </div>
          <div v-else class="empty-state">
            <FileText :size="35" />
            <h3>你的第一份日报即将诞生</h3>
            <p>每日更新结束后自动生成，零新增和部分失败也会如实记录。</p>
            <button class="secondary-button" @click="go('updates')">
              查看更新任务
            </button>
          </div>
        </template>

        <template v-if="page === 'evaluations'">
          <div class="info-banner">
            <ShieldCheck :size="20" />
            <div>
              <strong>每个有证据的回答，都值得被检验</strong>
              <p>
                复杂问答经独立角色评审，最多修订一次。无依据陈述越低越好，其余指标越高越好。模型自评仍需人工核实。
              </p>
            </div>
          </div>
          <div v-if="evaluations.length" class="evaluation-list">
            <details
              v-for="evaluation in evaluations"
              :key="evaluation.id"
              class="evaluation-card"
            >
              <summary>
                <div>
                  <strong>{{
                    evaluation.question || evaluation.query || "回答质量评测"
                  }}</strong
                  ><span
                    >{{ evaluation.model || "评审模型" }} ·
                    {{ date(evaluation.created_at) }} ·
                    {{ evaluation.duration_ms ?? evaluation.latency_ms ?? "—" }}
                    ms</span
                  >
                </div>
                <span
                  class="tag"
                  :class="{
                    green: evaluation.passed === true,
                    amber: evaluation.passed === false,
                  }"
                  >{{ evaluationStatus(evaluation) }}</span
                >
              </summary>
              <div class="score-grid">
                <div v-for="metric in scores(evaluation)" :key="metric.key">
                  <span>{{ metric.name }}</span
                  ><strong>{{ metric.score.toFixed(2) }}</strong>
                  <div class="score-track">
                    <i
                      :style="{
                        width: `${Math.max(0, Math.min(100, metric.score * 100))}%`,
                      }"
                    />
                  </div>
                  <small>{{ metric.reason }}</small>
                </div>
              </div>
              <p v-if="evaluation.error" class="message-error">
                {{ evaluation.error }}
              </p>
              <div
                v-if="evaluation.answer"
                class="prose"
                v-html="markdown(evaluation.answer)"
                @click="markdownClick"
              />
            </details>
          </div>
          <div v-else class="empty-state">
            <ShieldCheck :size="35" />
            <h3>还没有评测记录</h3>
            <p>
              使用知识库回答复杂问题后，评审结果将在这里留存。简单直接回答不做
              RAG 评分。
            </p>
          </div>
        </template>

        <template v-if="page === 'settings'">
          <div class="settings-grid">
            <div class="settings-main">
              <form class="panel settings-panel" @submit.prevent="saveSettings">
                <div class="panel-title">
                  <div class="icon-tile"><Sparkles :size="19" /></div>
                  <div>
                    <h2>大模型连接</h2>
                    <p>支持 DeepSeek 的 OpenAI 兼容接口</p>
                  </div>
                  <span v-if="settings.api_key_configured" class="tag green"
                    >密钥已配置</span
                  >
                </div>
                <label
                  >API Key <span>只保存加密密文，不回显已有密钥</span
                  ><input
                    v-model="apiKey"
                    type="password"
                    autocomplete="new-password"
                    :placeholder="
                      settings.api_key_configured
                        ? '已配置，输入新密钥以替换'
                        : '输入你的 API Key'
                    "
                /></label>
                <label
                  >Base URL<input
                    v-model="settings.base_url"
                    type="url"
                    required
                    placeholder="https://api.deepseek.com"
                /></label>
                <div class="field-row">
                  <label
                    >生成模型<input
                      v-model="settings.model"
                      list="model-list"
                      required
                      placeholder="deepseek-flash" /></label
                  ><label
                    >评审模型<input
                      v-model="settings.evaluation_model"
                      list="model-list"
                      placeholder="留空则使用生成模型"
                  /></label>
                </div>
                <datalist id="model-list">
                  <option v-for="model in models" :key="model" :value="model" />
                </datalist>
                <div class="toggle-row">
                  <div>
                    <strong>自动检查每日更新</strong>
                    <p>应用启动和每天 09:00 检查当天；不补历史任务。</p>
                  </div>
                  <input
                    v-model="settings.auto_update"
                    type="checkbox"
                    role="switch"
                    aria-label="自动检查每日更新"
                  />
                </div>
                <div class="settings-actions">
                  <button
                    type="submit"
                    class="primary-button"
                    :disabled="!!busy"
                  >
                    {{ busy === "save" ? "保存中…" : "保存设置" }}</button
                  ><button
                    type="button"
                    class="secondary-button"
                    :disabled="!!busy || !settings.api_key_configured"
                    @click="testConnection"
                  >
                    <LoaderCircle
                      v-if="busy === 'test'"
                      class="spin"
                      :size="15"
                    />{{
                      busy === "test" ? "测试中…" : "测试已保存的连接"
                    }}</button
                  ><button
                    v-if="settings.api_key_configured"
                    type="button"
                    class="text-button danger"
                    :disabled="!!busy"
                    @click="clearKey"
                  >
                    清除密钥
                  </button>
                </div>
              </form>
              <div class="panel settings-panel">
                <div class="panel-title">
                  <div class="icon-tile"><Cpu :size="19" /></div>
                  <div>
                    <h2>本地向量模型</h2>
                    <p>多语言 MiniLM · ONNX Runtime · CPU</p>
                  </div>
                  <span class="tag" :class="{ green: overview.model?.ready }">{{
                    modelState
                  }}</span>
                </div>
                <div class="model-name">
                  {{
                    overview.model?.name ||
                    settings.embedding_model ||
                    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
                  }}
                </div>
                <p class="muted-copy">
                  首次准备需要联网下载模型。之后向量化在本机完成，使用 2 个 CPU
                  线程、小批处理，空闲后释放模型会话。
                </p>
                <p v-if="overview.model?.error" class="message-error">
                  {{ overview.model.error }}
                </p>
                <div class="settings-actions">
                  <button
                    class="secondary-button"
                    :disabled="!!busy || operationRunning || !!activeUpdate"
                    @click="prepare"
                  >
                    <Download :size="15" />{{
                      operationRunning
                        ? "任务进行中…"
                        : overview.model?.ready
                          ? "检查本地模型"
                          : "准备本地模型"
                    }}</button
                  ><button
                    class="text-button"
                    :disabled="
                      !!busy ||
                      !overview.model?.ready ||
                      !!activeUpdate ||
                      operationRunning
                    "
                    @click="reindex"
                  >
                    <RefreshCw :size="15" />重建知识索引
                  </button>
                </div>
              </div>
            </div>
            <aside class="settings-aside">
              <ShieldCheck :size="27" />
              <h3>你的知识，保存在本地</h3>
              <p>
                对话、文档、索引与日报持久化在应用数据目录。API Key
                由系统加密保存。
              </p>
              <div class="aside-divider" />
              <h4>轻量运行</h4>
              <p>
                GPU
                默认不参与计算。本地向量模型按需加载，采集串行进行；大模型回答通过你配置的
                API 完成。
              </p>
              <h4>有迹可循</h4>
              <p>
                知识更新受来源白名单与每日配额约束，日报保留真实更新与失败记录。
              </p>
              <span class="version-label">IDOL / v0.1.0</span>
            </aside>
          </div>
        </template>
      </section>
    </main>
  </div>
</template>
