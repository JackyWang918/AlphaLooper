<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'

type TabId = 'control' | 'account' | 'logs'
type BrowserState = {
  connected: boolean
  url: string
  title: string
  fill_supported: boolean
  symbol?: string
  quote?: string
  reason?: string
}
type Task = {
  id: string
  active: boolean
  phase: string
  message: string
  risk_policy_version?: number
  strategy_policy_version?: number
  task_start_equity?: string | null
  task_start_quote?: string | null
  round_start_quote?: string | null
  inventory: string
  cost: string
  proceeds: string
  buy_total: string
  realized_pnl: string
  session_loss: string
  rounds: number
  stop_buying: boolean
  request: {
    url: string
    expected_symbol: string
    expected_quote: string
    config: {
      target_points: string
      current_points?: string
      points_per_u?: string
      amount?: string
    }
  }
  balances?: { quote_available: string; base_available: string }
  pending?: { side: string; price: string; quantity: string; quote_amount?: string } | null
  pending_order?: { state: string; message: string; submission_error?: string }
  risk?: {
    equity: string | null
    session_loss?: string | null
    loss_pct?: string | null
    unit_cost?: string | null
    reason?: string
  } | null
  schedule?: { kind: string; at: number | null; reason: string }
  created_at: number
}
type AutomaticStatus = { running: boolean; current: Task | null; recent: Task[] }
type LogEntry = {
  id: number
  time: number
  kind: string
  reason: string
  state: Record<string, unknown>
  evidence: Record<string, unknown>
  details: Record<string, unknown> | null
}

const tabs: { id: TabId; label: string; icon: string }[] = [
  { id: 'control', label: '控制', icon: '⌁' },
  { id: 'account', label: '账户', icon: '◎' },
  { id: 'logs', label: '日志', icon: '≡' },
]
const logLabels: Record<string, string> = {
  control: '任务操作',
  buy_wait: '等待买入',
  buy: '决定买入',
  sell: '决定卖出',
  risk: '风险检查',
  cancel: '决定撤单',
  execution: '执行反馈',
  order_check: '订单巡检',
  fill: '成交回填',
  completed: '任务结束',
  error: '异常暂停',
}

const activeTab = ref<TabId>('control')
const browser = ref<BrowserState>({ connected: false, url: '', title: '', fill_supported: false })
const automatic = ref<AutomaticStatus>({ running: false, current: null, recent: [] })
const token = ref(localStorage.getItem('alphalooper.mobile-token') ?? '')
const targetPoints = ref(localStorage.getItem('alphalooper.mobile-target-points') ?? '32768')
const currentPoints = ref(localStorage.getItem('alphalooper.mobile-current-points') ?? '0')
const amount = ref(localStorage.getItem('alphalooper.mobile-amount') ?? '50')
const logs = ref<LogEntry[]>([])
const logKind = ref('')
const logPage = ref(1)
const logCursors = ref<(number | null)[]>([null])
const logNext = ref<number | null>(null)
const busy = ref('')
const message = ref('正在读取运行状态…')
const error = ref('')
let timer: ReturnType<typeof setInterval> | undefined
let requestId = localStorage.getItem('automatic-request-id') || crypto.randomUUID()
localStorage.setItem('automatic-request-id', requestId)

watch(token, value => localStorage.setItem('alphalooper.mobile-token', value.trim()))
watch(targetPoints, value => localStorage.setItem('alphalooper.mobile-target-points', value))
watch(currentPoints, value => localStorage.setItem('alphalooper.mobile-current-points', value))
watch(amount, value => localStorage.setItem('alphalooper.mobile-amount', value))

const current = computed(() => automatic.value.current)
const taskForLogs = computed(() => current.value ?? automatic.value.recent[0] ?? null)
watch(logKind, () => {
  resetLogPagination()
  void refreshLogs()
})
watch(() => taskForLogs.value?.id, () => resetLogPagination())
const tokenProblem = computed(() => {
  const value = token.value.trim()
  if (!value) return '请输入 Token 合约地址。'
  if (!/^0x[0-9a-fA-F]{40}$/.test(value)) return '请输入 0x 开头的 40 位 BSC 合约地址。'
  return ''
})
const configProblem = computed(() => {
  for (const [label, value, allowZero] of [
    ['目标积分', targetPoints.value, false],
    ['启动已有积分', currentPoints.value, true],
    ['每轮买入金额', amount.value, false],
  ] as const) {
    if (!/^\d+(?:\.\d+)?$/.test(value) || (!allowZero && Number(value) <= 0)) {
      return `${label}必须是${allowZero ? '非负' : '大于 0 的'}普通数字。`
    }
  }
  if (Number(currentPoints.value) > Number(targetPoints.value)) return '启动已有积分不能大于目标积分。'
  if (Number(amount.value) > 2000) return '每轮买入金额不能超过 2,000 U。'
  return ''
})
const canStart = computed(() =>
  !busy.value && !current.value && browser.value.connected && browser.value.fill_supported
  && !tokenProblem.value && !configProblem.value,
)
const earnedPoints = computed(() => {
  if (!current.value) return 0
  return Number(current.value.buy_total) * Number(current.value.request.config.points_per_u ?? 4)
})
const totalPoints = computed(() => Number(current.value?.request.config.current_points ?? 0) + earnedPoints.value)
const remainingPoints = computed(() => Math.max(0, Number(current.value?.request.config.target_points ?? 0) - totalPoints.value))
const overallPnl = computed(() => {
  if (!current.value) return '—'
  const loss = current.value.risk?.session_loss ?? current.value.session_loss
  return invertDecimal(loss)
})
const statusTone = computed(() => automatic.value.running ? 'running' : current.value ? 'paused' : 'idle')
const statusText = computed(() => automatic.value.running ? '运行中' : current.value ? '已暂停' : '未启动')

function tradeUrl() {
  return `https://www.binance.com/zh-CN/alpha/bsc/${token.value.trim().toLowerCase()}`
}

function invertDecimal(value: string | null | undefined) {
  if (!value || /^-?0(?:\.0+)?$/.test(value)) return '0'
  return value.startsWith('-') ? value.slice(1) : `-${value}`
}

function formatPoints(value: number) {
  return Number.isFinite(value) ? value.toLocaleString('zh-CN', { maximumFractionDigits: 2 }) : '—'
}

function formatTime(value: number) {
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

function formatMoney(value: string | null | undefined) {
  if (value === null || value === undefined || value === '—') return '—'
  const number = Number(value)
  return Number.isFinite(number)
    ? number.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 4 })
    : value
}

function apiError(body: unknown, fallback: string) {
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail: unknown }).detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) {
      return detail.map(item => {
        if (item && typeof item === 'object' && 'msg' in item) return String(item.msg).replace(/^Value error,\s*/, '')
        return '参数无效'
      }).join('；')
    }
  }
  return fallback
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      'X-AlphaLooper-Client': 'local-ui',
      ...init?.headers,
    },
    signal: AbortSignal.timeout(120000),
  })
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(apiError(body, `请求失败（HTTP ${response.status}）`))
  return body as T
}

function showError(value: unknown, fallback: string) {
  error.value = value instanceof Error ? value.message : fallback
  message.value = ''
}

async function refreshBrowser(silent = false) {
  try {
    browser.value = await request<BrowserState>('/api/browser/status')
    const match = browser.value.url.match(/\/alpha\/bsc\/(0x[0-9a-fA-F]{40})/i)
    if (match?.[1]) token.value = match[1]
    if (!silent) message.value = browser.value.connected ? '浏览器状态已更新。' : 'Chrome 尚未启动。'
  } catch (value) {
    if (!silent) showError(value, '读取浏览器状态失败。')
  }
}

async function refreshAutomatic(silent = false) {
  try {
    automatic.value = await request<AutomaticStatus>('/api/automatic')
    if (!automatic.value.current && automatic.value.recent.some(task => task.id === requestId && !task.active)) {
      requestId = crypto.randomUUID()
      localStorage.setItem('automatic-request-id', requestId)
    }
    if (!silent) message.value = '任务状态已更新。'
  } catch (value) {
    if (!silent) showError(value, '读取任务状态失败。')
  }
}

async function refreshAll(silent = false) {
  await Promise.all([refreshBrowser(silent), refreshAutomatic(silent)])
  if (activeTab.value === 'logs' && logPage.value === 1) await refreshLogs(true)
}

async function launchBrowser() {
  if (busy.value) return
  busy.value = 'launch'
  error.value = ''
  message.value = '正在启动 Chrome…'
  try {
    browser.value = await request<BrowserState>('/api/browser/launch', { method: 'POST', body: '{}' })
    message.value = 'Chrome 已启动，请在受控浏览器中完成登录或验证。'
  } catch (value) {
    showError(value, '启动 Chrome 失败。')
  } finally {
    busy.value = ''
  }
}

async function openToken() {
  if (busy.value || tokenProblem.value) {
    error.value = tokenProblem.value
    return
  }
  busy.value = 'open'
  error.value = ''
  message.value = '正在打开 Token 交易页面…'
  try {
    browser.value = await request<BrowserState>('/api/browser/open', {
      method: 'POST',
      body: JSON.stringify({ url: tradeUrl() }),
    })
    message.value = browser.value.fill_supported
      ? `已识别 ${browser.value.symbol} / ${browser.value.quote}。`
      : '页面已打开，请完成登录或平台提示后刷新识别。'
  } catch (value) {
    showError(value, '打开 Token 页面失败。')
  } finally {
    busy.value = ''
  }
}

async function startTask() {
  if (!canStart.value) {
    error.value = configProblem.value || tokenProblem.value || '请先启动浏览器并识别 Token 交易页面。'
    return
  }
  busy.value = 'start'
  error.value = ''
  message.value = '正在核对余额和当前委托…'
  try {
    await request<Task>('/api/automatic/start', {
      method: 'POST',
      body: JSON.stringify({
        request_id: requestId,
        book: '本机账户',
        url: tradeUrl(),
        expected_symbol: browser.value.symbol,
        expected_quote: browser.value.quote,
        config: {
          amount: amount.value,
          target_points: targetPoints.value,
          current_points: currentPoints.value,
          buy_check_seconds: 20,
        },
      }),
    })
    requestId = crypto.randomUUID()
    localStorage.setItem('automatic-request-id', requestId)
    await refreshAutomatic(true)
    message.value = '自动实盘任务已启动。'
    activeTab.value = 'account'
  } catch (value) {
    showError(value, '启动自动任务失败。')
  } finally {
    busy.value = ''
  }
}

async function controlTask(action: 'pause' | 'resume' | 'finish' | 'force_restart') {
  if (!current.value || busy.value) return
  if (action === 'force_restart' && !window.confirm(
    '确定强制停止吗？这只会结束本地任务，不会撤销平台挂单，也不会卖出持仓。',
  )) return
  busy.value = action
  error.value = ''
  try {
    await request<Task>('/api/automatic/control', {
      method: 'POST',
      body: JSON.stringify({ task_id: current.value.id, action }),
    })
    await refreshAutomatic(true)
    message.value = action === 'pause' ? '自动操作已暂停。'
      : action === 'resume' ? '任务正在核对后恢复。'
        : action === 'finish' ? '已停止新买入，将卖完后结束。'
          : '本地任务已强制停止；平台挂单和持仓未处理。'
  } catch (value) {
    showError(value, '任务操作失败。')
  } finally {
    busy.value = ''
  }
}

function resetLogPagination() {
  logPage.value = 1
  logCursors.value = [null]
  logNext.value = null
  logs.value = []
}

async function refreshLogs(silent = false) {
  if (!taskForLogs.value) {
    logs.value = []
    logNext.value = null
    return
  }
  try {
    const params = new URLSearchParams({ limit: '20' })
    if (logKind.value) params.set('kind', logKind.value)
    const cursor = logCursors.value[logPage.value - 1]
    if (cursor) params.set('before', String(cursor))
    const result = await request<{ items: LogEntry[]; next_before: number | null }>(
      `/api/automatic/${taskForLogs.value.id}/decisions?${params}`,
    )
    logs.value = result.items
    logNext.value = result.next_before
    if (!silent) message.value = '日志已更新。'
  } catch (value) {
    if (!silent) showError(value, '读取任务日志失败。')
  }
}

async function nextLogPage() {
  if (!logNext.value) return
  logCursors.value[logPage.value] = logNext.value
  logPage.value += 1
  await refreshLogs(true)
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

async function previousLogPage() {
  if (logPage.value <= 1) return
  logPage.value -= 1
  await refreshLogs(true)
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

function selectTab(tab: TabId) {
  activeTab.value = tab
  if (tab === 'logs') void refreshLogs(true)
}

onMounted(async () => {
  await refreshAll(true)
  message.value = '状态已同步。'
  timer = setInterval(() => void refreshAll(true), 5000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <main class="mobile-shell">
    <header class="mobile-header">
      <div>
        <p class="brand">AlphaLooper</p>
        <p class="subtitle">自动实盘控制台</p>
      </div>
      <span :class="['status-pill', statusTone]"><i />{{ statusText }}</span>
    </header>

    <div v-if="message || error" :class="['mobile-message', { danger: error }]" role="status">
      {{ error || message }}
      <button v-if="error" aria-label="关闭错误提示" @click="error = ''">×</button>
    </div>

    <section v-show="activeTab === 'control'" class="mobile-page" aria-label="控制">
      <div class="step-card">
        <div class="card-heading">
          <span class="step-number">1</span>
          <div><h2>浏览器</h2><p>{{ browser.connected ? '受控 Chrome 已连接' : '启动运行自动任务的 Chrome' }}</p></div>
        </div>
        <div class="button-row">
          <button class="primary" :disabled="!!busy" @click="launchBrowser">
            {{ busy === 'launch' ? '启动中…' : browser.connected ? '显示 Chrome' : '启动浏览器' }}
          </button>
          <button class="ghost" :disabled="!!busy" @click="refreshBrowser()">刷新状态</button>
        </div>
      </div>

      <div class="step-card">
        <div class="card-heading">
          <span class="step-number">2</span>
          <div><h2>打开币种</h2><p>输入 BSC Token 合约地址</p></div>
        </div>
        <label class="mobile-field">
          <span>Token</span>
          <input v-model="token" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="0x…" />
        </label>
        <p v-if="token && tokenProblem" class="field-error">{{ tokenProblem }}</p>
        <button class="primary full" :disabled="!!busy || !browser.connected || !!tokenProblem" @click="openToken">
          {{ busy === 'open' ? '正在打开…' : '打开交易页面' }}
        </button>
        <p v-if="browser.fill_supported" class="recognized">已识别 {{ browser.symbol }} / {{ browser.quote }}</p>
        <p v-else-if="browser.reason" class="card-note">{{ browser.reason }}</p>
      </div>

      <div class="step-card">
        <div class="card-heading">
          <span class="step-number">3</span>
          <div><h2>任务配置</h2><p>输入内容会保存在当前手机</p></div>
        </div>
        <div class="mobile-grid">
          <label class="mobile-field wide"><span>目标积分</span><input v-model="targetPoints" inputmode="decimal" /></label>
          <label class="mobile-field"><span>启动已有积分</span><input v-model="currentPoints" inputmode="decimal" /></label>
          <label class="mobile-field"><span>每轮买入金额（U）</span><input v-model="amount" inputmode="decimal" /></label>
        </div>
        <p v-if="configProblem" class="field-error">{{ configProblem }}</p>
        <button class="primary full start-button" :disabled="!canStart" @click="startTask">
          {{ busy === 'start' ? '正在启动任务…' : '启动自动实盘买卖' }}
        </button>
      </div>

      <div v-if="current" class="step-card task-control">
        <div class="card-heading compact">
          <div><h2>任务控制</h2><p>{{ current.request.expected_symbol }} · {{ current.message }}</p></div>
        </div>
        <div class="control-grid">
          <button class="pause" :disabled="!!busy || !automatic.running" @click="controlTask('pause')">暂停自动操作</button>
          <button class="primary" :disabled="!!busy || automatic.running || current.strategy_policy_version !== 2" @click="controlTask('resume')">核对后恢复</button>
          <button class="ghost" :disabled="!!busy || current.strategy_policy_version !== 2" @click="controlTask('finish')">停止买入，卖完结束</button>
          <button class="danger-button" :disabled="!!busy" @click="controlTask('force_restart')">强制停止</button>
        </div>
        <p class="warning-copy">强制停止不会撤销平台挂单，也不会卖出持仓。</p>
        <p v-if="current.strategy_policy_version !== 2" class="warning-copy">旧策略任务不能直接恢复。请在桌面控制台处理旧挂单和持仓后结束旧版记录。</p>
      </div>
    </section>

    <section v-show="activeTab === 'account'" class="mobile-page" aria-label="账户状态">
      <div v-if="current" class="account-stack">
        <div class="account-hero">
          <span>整体盈亏估值</span>
          <strong :class="{ positive: overallPnl.startsWith('-') === false && overallPnl !== '0', negative: overallPnl.startsWith('-') }">
            {{ overallPnl === '—' ? '—' : `${formatMoney(overallPnl)} U` }}
          </strong>
          <p>按任务启动总资产与当前净资产估值计算</p>
        </div>

        <div class="metric-grid">
          <article><span>开始金额</span><strong>{{ formatMoney(current.task_start_equity ?? current.task_start_quote) }} U</strong></article>
          <article><span>本轮起始 USDT</span><strong>{{ current.round_start_quote ? formatMoney(current.round_start_quote) : '尚未开始' }}</strong></article>
          <article><span>累计买入</span><strong>{{ formatMoney(current.buy_total) }} U</strong></article>
          <article><span>已完成轮次</span><strong>{{ current.rounds }}</strong></article>
        </div>

        <div class="progress-card">
          <div><span>积分进度</span><strong>还差 {{ formatPoints(remainingPoints) }} 分</strong></div>
          <div class="progress-track"><i :style="{ width: `${Math.min(100, Math.max(0, totalPoints / Number(current.request.config.target_points) * 100))}%` }" /></div>
          <p>{{ formatPoints(totalPoints) }} / {{ formatPoints(Number(current.request.config.target_points)) }}</p>
        </div>

        <div class="status-card">
          <span>当前状态</span>
          <strong>{{ current.message }}</strong>
          <p v-if="current.schedule">下一动作：{{ current.schedule.reason }}</p>
          <p v-if="current.balances">可用 USDT {{ formatMoney(current.balances.quote_available) }} · 代币 {{ current.inventory }}</p>
          <p v-if="current.pending">当前计划：{{ current.pending.side === 'buy' ? `买入 ${current.pending.quote_amount} U` : '卖出全部可用代币' }} @ {{ current.pending.price }}</p>
        </div>
        <button class="ghost full" :disabled="!!busy" @click="refreshAutomatic()">刷新账户状态</button>
      </div>
      <div v-else class="empty-state"><strong>暂无运行任务</strong><p>请先到“控制”页启动自动实盘任务。</p><button class="primary" @click="selectTab('control')">前往控制</button></div>
    </section>

    <section v-show="activeTab === 'logs'" class="mobile-page" aria-label="运行日志">
      <div class="log-toolbar">
        <label><span>日志类型</span><select v-model="logKind"><option value="">全部</option><option v-for="(label, key) in logLabels" :key="key" :value="key">{{ label }}</option></select></label>
        <button class="ghost" :disabled="!!busy || !taskForLogs" @click="refreshLogs()">刷新</button>
      </div>
      <p v-if="taskForLogs" class="log-task">{{ taskForLogs.request.expected_symbol }} · {{ taskForLogs.id.slice(0, 8) }}</p>
      <div v-if="logs.length" class="log-list">
        <article v-for="entry in logs" :key="entry.id">
          <div><span class="log-kind">{{ logLabels[entry.kind] ?? entry.kind }}</span><time>{{ formatTime(entry.time) }}</time></div>
          <p>{{ entry.reason }}</p>
          <details><summary>完整依据</summary><pre>{{ JSON.stringify(entry, null, 2) }}</pre></details>
        </article>
      </div>
      <div v-else class="empty-state"><strong>{{ taskForLogs ? '暂无日志' : '暂无任务' }}</strong><p>{{ taskForLogs ? '任务产生新判断后会显示在这里。' : '启动任务后可查看运行日志。' }}</p></div>
      <nav v-if="taskForLogs && (logs.length || logPage > 1)" class="log-pagination" aria-label="日志分页">
        <button class="ghost" :disabled="logPage === 1" @click="previousLogPage">上一页</button>
        <span>第 {{ logPage }} 页</span>
        <button class="ghost" :disabled="!logNext" @click="nextLogPage">下一页</button>
      </nav>
    </section>

    <nav class="mobile-tabs" aria-label="手机控制台导航">
      <button v-for="tab in tabs" :key="tab.id" :class="{ active: activeTab === tab.id }" @click="selectTab(tab.id)">
        <i>{{ tab.icon }}</i><span>{{ tab.label }}</span>
      </button>
    </nav>
  </main>
</template>

<style scoped>
.mobile-shell{width:100%;max-width:560px;min-height:100dvh;margin:0 auto;padding:0 14px 104px;background:#0b111b;color:#edf4fa}
.mobile-header{position:sticky;top:0;z-index:10;display:flex;align-items:center;justify-content:space-between;padding:20px 2px 14px;background:linear-gradient(#0b111b 82%,transparent)}
.brand{margin:0;color:#7ce0c5;font-size:20px;font-weight:800;letter-spacing:.2px}.subtitle{margin:3px 0 0;color:#8190a3;font-size:12px}
.status-pill{display:flex;align-items:center;gap:7px;padding:8px 11px;border:1px solid #2a384b;border-radius:999px;background:#141e2c;color:#a9b5c5;font-size:12px}.status-pill i{width:7px;height:7px;border-radius:50%;background:#758195}.status-pill.running{color:#82e4c8;border-color:#285f53}.status-pill.running i{background:#5ce1bc;box-shadow:0 0 10px #5ce1bc}.status-pill.paused{color:#ffd081;border-color:#6c532b}.status-pill.paused i{background:#f2b955}
.mobile-message{position:relative;margin:4px 0 12px;padding:12px 38px 12px 14px;border:1px solid #285f53;border-radius:12px;background:#112c29;color:#a8edda;font-size:13px;line-height:1.45}.mobile-message.danger{border-color:#704044;background:#351e25;color:#ffbfc4}.mobile-message button{position:absolute;right:6px;top:0;margin:0;padding:8px;background:transparent;color:inherit;font-size:22px}
.mobile-page{margin:0;padding:0;background:transparent;border:0;border-radius:0}.step-card,.account-hero,.progress-card,.status-card,.metric-grid article{margin:0 0 12px;padding:18px;background:#151f2d;border:1px solid #27364a;border-radius:16px;box-shadow:0 8px 28px rgba(0,0,0,.13)}
.card-heading{display:flex;align-items:flex-start;gap:12px;margin-bottom:17px}.card-heading.compact{margin-bottom:12px}.card-heading h2{margin:0 0 4px;font-size:17px}.card-heading p{margin:0;color:#8291a4;font-size:12px;line-height:1.5}.step-number{display:grid;place-items:center;width:29px;height:29px;flex:0 0 29px;border-radius:9px;background:#203e3a;color:#75ddc0;font-size:13px;font-weight:800}
.button-row,.control-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}button{min-height:46px;margin:0;padding:11px 14px;border-radius:12px;font-weight:700}.primary{background:#6de0bf;color:#071713}.ghost{background:#253247;color:#e7edf5}.pause{background:#5e4725;color:#ffe1a8}.danger-button{background:#602e37;color:#ffd6da}.full{width:100%}.start-button{margin-top:16px}
.mobile-field{margin-bottom:13px}.mobile-field span,.log-toolbar label span{display:block;margin-bottom:7px;color:#aebaca;font-size:12px}.mobile-field input,.log-toolbar select{min-height:48px;margin:0;padding:12px 13px;border-color:#36475e;border-radius:11px;background:#0d1521;font-size:16px}.mobile-grid{display:grid;grid-template-columns:1fr 1fr;gap:0 10px}.mobile-grid .wide{grid-column:1/-1}.field-error{margin:7px 0;color:#ff9fa8;font-size:12px;line-height:1.45}.recognized{margin:12px 0 0;color:#73dfc0;font-size:13px}.card-note,.warning-copy{margin:12px 0 0;color:#8998aa;font-size:12px;line-height:1.55}.warning-copy{color:#e6acb2}.task-control{border-color:#354861}
.account-stack{padding-top:2px}.account-hero{text-align:center;padding:26px 18px}.account-hero span,.metric-grid span,.progress-card span,.status-card span{color:#8796aa;font-size:12px}.account-hero strong{display:block;margin:10px 0 6px;font-size:35px;letter-spacing:-1px}.account-hero p,.progress-card p,.status-card p{margin:6px 0 0;color:#8493a6;font-size:12px;line-height:1.55}.positive{color:#65dfbd}.negative{color:#ff8f99}.metric-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.metric-grid article{min-width:0;margin:0;padding:15px}.metric-grid strong{display:block;margin-top:8px;overflow-wrap:anywhere;font-size:16px}.progress-card>div:first-child{display:flex;justify-content:space-between;gap:12px}.progress-track{height:8px;margin-top:14px;overflow:hidden;border-radius:99px;background:#29364a}.progress-track i{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,#54cfae,#7ae0c4)}.status-card strong{display:block;margin-top:8px;font-size:15px;line-height:1.5}
.log-toolbar{display:flex;align-items:end;gap:10px;margin-bottom:10px}.log-toolbar label{flex:1}.log-toolbar select{width:100%}.log-toolbar button{min-width:82px}.log-task{margin:8px 2px 14px;color:#7f90a5;font-size:12px}.log-list article{margin-bottom:10px;padding:15px;background:#151f2d;border:1px solid #27364a;border-radius:14px}.log-list article>div{display:flex;align-items:center;justify-content:space-between;gap:8px}.log-list time{color:#748398;font-size:11px}.log-kind{padding:4px 7px;border-radius:6px;background:#213b38;color:#77ddc1;font-size:11px}.log-list p{margin:11px 0;color:#dbe4ed;font-size:14px;line-height:1.55}.log-list details{margin:0}.log-list summary{color:#8090a4;font-size:12px}.log-list pre{max-height:340px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;padding:10px;border-radius:8px;background:#0b111b;color:#aab7c7;font-size:10px}.empty-state{padding:34px 20px;text-align:center;color:#8594a7}.empty-state strong{color:#dce5ee}.empty-state p{font-size:13px}
.log-pagination{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:12px;margin:16px 0 4px}.log-pagination span{text-align:center;color:#9aa8b9;font-size:13px}.log-pagination button:last-child{justify-self:stretch}
.mobile-tabs{position:fixed;z-index:20;left:50%;bottom:0;display:grid;grid-template-columns:repeat(3,1fr);width:min(560px,100%);padding:8px 12px calc(8px + env(safe-area-inset-bottom));transform:translateX(-50%);border-top:1px solid #253246;background:rgba(12,18,28,.96);backdrop-filter:blur(14px)}.mobile-tabs button{display:flex;min-height:54px;flex-direction:column;align-items:center;justify-content:center;gap:3px;background:transparent;color:#748397;font-size:11px}.mobile-tabs button i{height:20px;font-style:normal;font-size:20px;line-height:20px}.mobile-tabs button.active{color:#70dec0;background:#162a2a}
@media(max-width:370px){.mobile-grid,.control-grid{grid-template-columns:1fr}.mobile-grid .wide{grid-column:auto}.metric-grid{grid-template-columns:1fr}.mobile-shell{padding-left:10px;padding-right:10px}}
</style>
