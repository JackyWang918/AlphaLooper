<script setup lang="ts">
import { onMounted, ref } from 'vue'
import ResearchPanel from './components/ResearchPanel.vue'
import LiveOrder from './components/LiveOrder.vue'
import AutomaticTrading from './components/AutomaticTrading.vue'
import BrowserTests from './components/BrowserTests.vue'

const tabs = [
  { id: 'browser', label: '浏览器连接', hint: '01 · 02' },
  { id: 'automatic', label: '自动实盘交易', hint: '05' },
  { id: 'tools', label: '其他操作', hint: '03 · 04' },
] as const
type TabId = typeof tabs[number]['id']
const savedTab = localStorage.getItem('alphalooper.active-tab')
const activeTab = ref<TabId>(tabs.some(tab => tab.id === savedTab) ? savedTab as TabId : 'browser')
function selectTab(id: TabId) {
  activeTab.value = id
  localStorage.setItem('alphalooper.active-tab', id)
}
function navigateTabs(event: KeyboardEvent, index: number) {
  let target = index
  if (event.key === 'ArrowRight') target = (index + 1) % tabs.length
  else if (event.key === 'ArrowLeft') target = (index + tabs.length - 1) % tabs.length
  else if (event.key === 'Home') target = 0
  else if (event.key === 'End') target = tabs.length - 1
  else return
  event.preventDefault()
  selectTab(tabs[target]!.id)
  document.getElementById(`tab-${tabs[target]!.id}`)?.focus()
}

type Filled = { side: string; symbol: string; quote: string; price: string; quantity: string; submitted: boolean }
type BrowserState = { connected: boolean; url: string; title: string; fill_supported: boolean; symbol?: string; quote?: string; reason?: string; price_step?: string; quantity_step?: string }
const browser = ref<BrowserState>({ connected: false, url: '', title: '', fill_supported: false })
const pending = ref(false)
const message = ref('正在检查本地服务…')
const failed = ref(false)
const tradeUrl = ref(localStorage.getItem('alphalooper.trade-url') ?? 'https://www.binance.com/zh-CN/alpha/bsc/0x10d4183389e99233db3cc981c43443ebd28ebd5e')
const filled = ref<Filled | null>(null)
const history = ref<{ time: string; message: string }[]>([])

async function execute(action: 'status' | 'launch' | 'open') {
  if (pending.value) return
  pending.value = true
  failed.value = false
  filled.value = null
  message.value = action === 'launch' ? '正在启动 Chrome，请稍候…' : '正在读取浏览器状态…'
  try {
    const response = await fetch(`/api/browser/${action}`, {
      method: action === 'status' ? 'GET' : 'POST',
      headers: { 'Content-Type': 'application/json', 'X-AlphaLooper-Client': 'local-ui' },
      ...(action === 'open' ? { body: JSON.stringify({ url: tradeUrl.value }) } : {}),
      signal: AbortSignal.timeout(55000),
    })
    const result = await response.json()
    if (!response.ok) {
      throw new Error(typeof result.detail === 'string' ? result.detail : '请检查链接、价格和数量，数值必须大于零。')
    }
    browser.value = result
    filled.value = result.filled ?? null
    if (action === 'open') localStorage.setItem('alphalooper.trade-url', tradeUrl.value)
    message.value = result.connected
      ? 'Chrome 已连接。请在 Chrome 中手动登录和处理验证。'
      : 'Chrome 尚未启动，点击下方按钮开始。'
  } catch (error) {
    failed.value = true
    message.value = error instanceof Error ? error.message : '操作失败，请检查后端服务。'
  } finally {
    history.value.unshift({ time: new Date().toLocaleTimeString(), message: message.value })
    history.value = history.value.slice(0, 20)
    pending.value = false
  }
}
function openTaskPage(url: string) {
  tradeUrl.value = url
  void execute('open')
}

onMounted(() => execute('status'))
</script>

<template>
  <main>
    <header><div><p class="eyebrow">本地交易辅助控制台</p><h1>AlphaLooper</h1></div><span class="badge">自动实盘控制台</span></header>
    <p class="intro">先连接浏览器并登录，再打开你指定的 Alpha 交易页面。</p>
    <p role="status" :class="['notice', { error: failed }]">{{ message }}</p>
    <nav class="workspace-tabs" role="tablist" aria-label="控制台功能">
      <button v-for="(tab,index) in tabs" :id="`tab-${tab.id}`" :key="tab.id" role="tab"
        :aria-selected="activeTab===tab.id" :aria-controls="`panel-${tab.id}`"
        :tabindex="activeTab===tab.id?0:-1" @click="selectTab(tab.id)" @keydown="navigateTabs($event,index)">
        <span>{{tab.label}}</span><small>{{tab.hint}}</small>
      </button>
    </nav>
    <div v-show="activeTab==='browser'" id="panel-browser" role="tabpanel" aria-labelledby="tab-browser">
    <section>
      <div class="section-title"><h2>01 / 浏览器连接</h2><span>{{ browser.connected ? '已连接' : '未连接' }}</span></div>
      <p class="muted">使用独立 Chrome 用户目录保存登录状态。你可以直接在浏览器中操作。</p>
      <div class="actions">
        <button :disabled="pending" @click="execute('launch')">{{ pending ? '处理中…' : browser.connected ? '显示 Chrome' : '启动 Chrome' }}</button>
        <button class="secondary" :disabled="pending" @click="execute('status')">刷新状态</button>
      </div>
      <div v-if="browser.connected" class="page-info"><strong>{{ browser.title || '空白页面' }}</strong><p>{{ browser.url }}</p><p v-if="browser.symbol">已识别：{{ browser.symbol }} / {{ browser.quote }}</p><p>{{ browser.reason }}</p></div>
    </section>
    <section>
      <h2>02 / 打开交易页面</h2>
      <label for="trade-url">币安 Alpha 交易链接</label>
      <input id="trade-url" v-model="tradeUrl" type="url" placeholder="粘贴你的币安 Alpha 网页交易链接" :disabled="pending" />
      <button :disabled="pending || !browser.connected || !tradeUrl.trim()" @click="execute('open')">打开页面</button>
    </section>
    </div>
    <div v-show="activeTab==='automatic'" id="panel-automatic" role="tabpanel" aria-labelledby="tab-automatic">
      <AutomaticTrading :url="tradeUrl" :connected="browser.connected" :symbol="browser.symbol" :quote="browser.quote" :fill-supported="browser.fill_supported" :browser-busy="pending" :browser-reason="browser.reason" @refresh-browser="execute('status')" @open-task-page="openTaskPage" />
    </div>
    <div v-show="activeTab==='tools'" id="panel-tools" role="tabpanel" aria-labelledby="tab-tools">
    <BrowserTests :url="tradeUrl" :connected="browser.connected" :symbol="browser.symbol" :quote="browser.quote" :fill-supported="browser.fill_supported" :browser-busy="pending" @refresh-browser="execute('status')" />
    <LiveOrder :url="tradeUrl" :connected="browser.connected" :symbol="browser.symbol" :quote="browser.quote" :fill-supported="browser.fill_supported" :browser-busy="pending" :browser-reason="browser.reason" @refresh-browser="execute('status')" />
    <ResearchPanel v-model:url="tradeUrl" />
    <section>
      <h2>操作反馈</h2>
      <p class="muted">此处显示本次打开控制台后的操作记录。</p>
      <ul class="history"><li v-for="(entry, index) in history" :key="index"><time>{{ entry.time }}</time><span>{{ entry.message }}</span></li></ul>
    </section>
    </div>
  </main>
</template>

<style scoped>
.workspace-tabs{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;padding:6px;background:#101723;border:1px solid #314057;border-radius:12px}
.workspace-tabs button{margin:0;display:flex;align-items:center;justify-content:center;gap:10px;background:transparent;color:#a7b5c8;padding:14px 10px}
.workspace-tabs button[aria-selected="true"]{background:#72d8bd;color:#101723;font-weight:600}
.workspace-tabs button:focus-visible{outline:2px solid #e6edf6;outline-offset:2px}
.workspace-tabs small{font-size:11px;opacity:.8;white-space:nowrap}
@media(max-width:640px){.workspace-tabs button{flex-direction:column;gap:5px;font-size:14px}}
</style>
