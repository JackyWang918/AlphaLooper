<script setup lang="ts">
import { onMounted, ref } from 'vue'

type Filled = { side: string; symbol: string; quote: string; price: string; quantity: string; submitted: boolean }
type BrowserState = { connected: boolean; url: string; title: string; fill_supported: boolean; symbol?: string; quote?: string; reason?: string; price_step?: string; quantity_step?: string }
const browser = ref<BrowserState>({ connected: false, url: '', title: '', fill_supported: false })
const pending = ref(false)
const message = ref('正在检查本地服务…')
const failed = ref(false)
const tradeUrl = ref(localStorage.getItem('alphalooper.trade-url') ?? 'https://www.binance.com/zh-CN/alpha/bsc/0x10d4183389e99233db3cc981c43443ebd28ebd5e')
const filled = ref<Filled | null>(null)
const side = ref('buy')
const price = ref('')
const quantity = ref('')
const history = ref<{ time: string; message: string }[]>([])

async function execute(action: 'status' | 'launch' | 'open' | 'fill') {
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
      ...(action === 'fill' ? { body: JSON.stringify({ url: tradeUrl.value, side: side.value, price: price.value, quantity: quantity.value, expected_symbol: browser.value.symbol, expected_quote: browser.value.quote }) } : {}),
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
    if (action === 'fill') message.value = '价格与数量已填写并回读核对，未提交订单。'
  } catch (error) {
    failed.value = true
    message.value = error instanceof Error ? error.message : '操作失败，请检查后端服务。'
  } finally {
    history.value.unshift({ time: new Date().toLocaleTimeString(), message: message.value })
    history.value = history.value.slice(0, 20)
    pending.value = false
  }
}

onMounted(() => execute('status'))
</script>

<template>
  <main>
    <header><div><p class="eyebrow">本地交易辅助控制台</p><h1>AlphaLooper</h1></div><span class="badge">仅填表验证阶段</span></header>
    <p class="intro">先连接浏览器并登录，再打开你指定的 Alpha 交易页面。</p>
    <p role="status" :class="['notice', { error: failed }]">{{ message }}</p>
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
    <section>
      <h2>03 / 仅填表</h2>
      <p class="muted">填写前核对链、合约地址、币种和计价币。此操作只填写，不点击下单按钮。弹窗或验证请在 Chrome 中手动处理。</p>
      <p v-if="browser.fill_supported" class="muted">价格步长：{{ browser.price_step }}；数量步长：{{ browser.quantity_step }}。数量单位为 {{ browser.symbol }}。</p>
      <div class="fields">
        <label>方向<select v-model="side" :disabled="pending"><option value="buy">买入</option><option value="sell">卖出</option></select></label>
        <label>价格<input v-model="price" :disabled="pending" inputmode="decimal" placeholder="指定价格" /></label>
        <label>数量<input v-model="quantity" :disabled="pending" inputmode="decimal" placeholder="指定数量" /></label>
      </div>
      <button :disabled="pending || !browser.fill_supported || !price.trim() || !quantity.trim()" @click="execute('fill')">仅填表并核对</button>
      <p v-if="filled" class="notice">回读结果：{{ filled.side === 'buy' ? '买入' : '卖出' }} {{ filled.symbol }}，价格 {{ filled.price }} {{ filled.quote }}，数量 {{ filled.quantity }}。未提交订单。</p>
    </section>
    <section>
      <h2>操作反馈</h2>
      <p class="muted">此处显示本次打开控制台后的操作记录。</p>
      <ul class="history"><li v-for="(entry, index) in history" :key="index"><time>{{ entry.time }}</time><span>{{ entry.message }}</span></li></ul>
    </section>
  </main>
</template>
