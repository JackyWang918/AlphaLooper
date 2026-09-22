<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

type Candle = { time: number; close_time: number; open: string; high: string; low: string; close: string; volume: string; quote_volume: string }
type Snapshot = { symbol: string; token: string; quote: string; fetched_at: number; book_time: number; latency_ms: number; candles: Candle[]; bids: [string,string][]; asks: [string,string][]; ticker: Record<string,string> }
type Estimate = { center: string; volatility: string; buy: string; sell: string; quantity: string; warnings: string[] }
type Order = { side: string; price: string; quantity: string; filled: string; placed_at: number; cancel_requested: boolean }
type State = { symbol?: string; clock?: number; inventory?: string; cost?: string; proceeds?: string; session_loss?: string; exiting?: boolean; budget_stopped?: boolean; first_buy_at?: number | null; completed?: boolean; target_reached?: boolean; message?: string; order?: Order | null }
type LedgerOrder = Order & { id: string; status: string }
type Fill = { id: string; order_id: string; side: string; quantity: string; price: string; gross: string; fee: string; fee_currency: string; clock: number }
type Task = { id: string; url: string; version: number; created_at: number; config: typeof config.value; state: State; snapshot: Snapshot; summary: Record<string, string | boolean | null>; orders: LedgerOrder[]; fills: Fill[]; events: { id: string; clock: number; message: string }[] }
const props = defineProps<{ url: string }>()
const emit = defineEmits<{ 'update:url': [value: string] }>()
const quote = ref('USDT')
const config = ref({ amount:'50', window:15, buy_offset:'0.5', sell_offset:'0.5', fee_bps:'1', stop_pct:'2', budget:'10', reserve:'2', wait_seconds:300, exit_seconds:15, exit_level:6, max_hold_seconds:1800, target_points:'32768', points_per_u:'4' })
const feePercent = computed(() => {
  const value = config.value.fee_bps.trim()
  if (!/^\d+(?:\.\d+)?$/.test(value)) return '—'
  // Convert basis points to percent by shifting decimal digits, without rounding.
  const [whole = '0', fraction = ''] = value.split('.')
  const digits = whole.padStart(3, '0')
  const integer = digits.slice(0, -2).replace(/^0+(?=\d)/, '')
  const decimals = (digits.slice(-2) + fraction).replace(/0+$/, '')
  return decimals ? `${integer}.${decimals}` : integer
})
const feeConfirmed = ref(false)
const snapshot = ref<Snapshot | null>(null)
const estimate = ref<Estimate | null>(null)
const state = ref<State>({})
const busy = ref(false)
const error = ref('')
const quantity = ref('')
const fillPrice = ref('')
const logs = ref<string[]>([])
const fingerprint = ref('')
const simulationKey = ref('')
const task = ref<Task | null>(null)
const taskList = ref<{id:string; created_at:number; symbol:string; completed:boolean}[]>([])
const selectedTask = ref('')
const newTaskId = ref(crypto.randomUUID())
const pendingEvent = ref<{ expected_version:number; event:{id:string; kind:string; advance_seconds:number; quantity:string; price:string}; snapshot:Snapshot } | null>(null)
const valuationCurrent = computed(() => !!task.value && now.value-task.value.snapshot.fetched_at<=15000)
const statusLabel: Record<string,string> = {open:'挂单中',partial:'部分成交',cancel_requested:'待撤单确认',filled:'全部成交',cancelled:'已撤单'}
const now = ref(Date.now())
const autoRefresh = ref(false)
const lastAttempt = ref(0)
const timer = setInterval(() => {
  now.value=Date.now()
  if(autoRefresh.value && feeConfirmed.value && !busy.value && now.value-lastAttempt.value>=10000) void refresh()
},1000)
onBeforeUnmount(() => clearInterval(timer))
const key = computed(() => JSON.stringify([props.url, quote.value, config.value]))
const outdated = computed(() => !snapshot.value || fingerprint.value !== key.value || now.value-snapshot.value.fetched_at>15000)
const candles = computed(() => (snapshot.value?.candles ?? []).filter(c=>c.close_time<(snapshot.value?.fetched_at ?? 0)))
const chart = computed(() => {
  const rows=candles.value.slice(-60)
  if (!rows.length) return []
  const high=Math.max(...rows.map(r=>Number(r.high))), low=Math.min(...rows.map(r=>Number(r.low)))
  const span=high-low || high*0.001 || 1
  const vol=Math.max(...rows.map(r=>Number(r.volume)),1)
  const y=(v:string)=>15+(high-Number(v))/span*145
  return rows.map((r,i)=>({ ...r, x:10+i*780/rows.length, width:Math.max(2,500/rows.length), highY:y(r.high), lowY:y(r.low), top:Math.min(y(r.open),y(r.close)), height:Math.max(1,Math.abs(y(r.open)-y(r.close))), volHeight:Number(r.volume)/vol*55, color:Number(r.close)>=Number(r.open)?'#72d8bd':'#f58b96' }))
})
const fmt=(v:unknown)=>v == null?'—':Number(v).toLocaleString(undefined,{maximumFractionDigits:8})
async function api(path:string, body?:unknown) {
  const response=await fetch(`/api/research/${path}`,{method:body === undefined?'GET':'POST', headers:{'Content-Type':'application/json','X-AlphaLooper-Client':'local-ui'},body:JSON.stringify(body), signal:AbortSignal.timeout(65000)})
  const result=await response.json().catch(()=>{throw new Error('后端响应异常，请确认服务已启动且数据库迁移已完成。')})
  if(!response.ok) {
    throw Object.assign(new Error(typeof result.detail==='string'?result.detail:'参数无效，请检查数值范围。'), {status:response.status})
  }
  return result
}
async function refresh() {
  if(busy.value) return
  busy.value=true; error.value=''
  lastAttempt.value=Date.now()
  const requestedKey=key.value
  try {
    const result=await api('preview',{url:props.url,quote:quote.value,config:config.value})
    snapshot.value=result.snapshot; estimate.value=result.estimate; fingerprint.value=requestedKey
  } catch(e) { error.value=e instanceof Error?e.message:'请求失败'; snapshot.value=null; estimate.value=null }
  finally { busy.value=false }
}
function applyTask(result:Task) {
  task.value=result; state.value=result.state
  selectedTask.value=result.id
  localStorage.setItem('alphalooper.research-task',result.id)
  logs.value=result.events.slice(0,100).map(e=>`${new Date(e.clock*1000).toLocaleTimeString()} · ${e.message}`)
  fillPrice.value=state.value.order?.price ?? ''
}
async function listTasks() { taskList.value=await api('tasks') }
async function loadTask(id:string) {
  if(!id || busy.value) return
  busy.value=true; error.value=''
  try {
    const result:Task=await api(`tasks/${id}`)
    applyTask(result)
    config.value=result.config; quote.value=result.snapshot.quote
    emit('update:url',result.url)
    snapshot.value=null; estimate.value=null; feeConfirmed.value=false
    simulationKey.value=JSON.stringify([result.url, result.snapshot.quote, result.config])
    if(pendingEvent.value && result.events.some(e=>e.id===pendingEvent.value?.event.id)) pendingEvent.value=null
  } catch(e) { error.value=e instanceof Error?e.message:'载入失败' }
  finally {busy.value=false}
}
onMounted(async()=>{
  try {
    await listTasks()
    const saved=localStorage.getItem('alphalooper.research-task')
    if(saved && taskList.value.some(t=>t.id===saved)) await loadTask(saved)
  } catch(e) { error.value=e instanceof Error?e.message:'读取任务失败' }
})
function reset() {
  if(pendingEvent.value) { error.value='有结果未确认的事件，请先重试或重新载入当前任务核对。'; return }
  state.value={}; simulationKey.value=''; logs.value=[]; quantity.value=''; fillPrice.value=''
  task.value=null; selectedTask.value=''; newTaskId.value=crypto.randomUUID()
  localStorage.removeItem('alphalooper.research-task')
}
async function retryEvent() {
  if(busy.value || !task.value || !pendingEvent.value) return
  busy.value=true; error.value=''
  try {
    const result=await api(`tasks/${task.value.id}/events`,pendingEvent.value)
    applyTask(result); pendingEvent.value=null
    await listTasks()
  } catch(e) {
    if(e && typeof e==='object' && 'status' in e && (e.status===422 || e.status===409)) pendingEvent.value=null
    error.value=e instanceof Error?e.message:'事件结果未确认，请重试或重新载入核对。'
  }
  finally {busy.value=false}
}
async function simulate(kind:string, advance=0) {
  if(busy.value || outdated.value || !feeConfirmed.value || !snapshot.value || pendingEvent.value) return
  if(simulationKey.value && simulationKey.value!==key.value) { error.value='任务参数或币种已变化，请载入原任务或新建模拟任务。'; return }
  busy.value=true; error.value=''
  try {
    if(!task.value) {
      applyTask(await api('tasks',{id:newTaskId.value,url:props.url,quote:quote.value,config:config.value,snapshot:snapshot.value}))
      simulationKey.value=key.value
    }
    pendingEvent.value={expected_version:task.value!.version,event:{id:crypto.randomUUID(),kind,advance_seconds:advance,quantity:kind==='fill'?quantity.value:'0',price:kind==='fill'?fillPrice.value:'0'},snapshot:snapshot.value}
  } catch(e) { error.value=e instanceof Error?e.message:'创建任务失败' }
  finally {busy.value=false}
  if(pendingEvent.value) await retryEvent()
}
</script>

<template>
  <section class="research">
    <div class="section-title"><h2>行情与策略研究</h2><span class="badge">模拟专用 · 不连接下单</span></div>
    <p><a href="#simulation-ledger">查看模拟账本与统计 ↓</a></p>
    <p class="muted">使用上方交易链接读取公开行情，无需登录。研究参数为初始试验值，尚未验证收益或损耗。</p>
    <fieldset :disabled="busy">
      <fieldset :disabled="!!task"><div class="fields research-fields">
        <label>计价币<select v-model="quote"><option>USDT</option><option>USDC</option></select></label>
        <label>单轮预算（含假设买入费）<input v-model="config.amount" inputmode="decimal" /></label>
        <label>观察窗口（分钟）<input v-model.number="config.window" type="number" min="3" max="240" /></label>
        <label>买入偏移系数<input v-model="config.buy_offset" inputmode="decimal" /></label>
        <label>卖出偏移系数<input v-model="config.sell_offset" inputmode="decimal" /></label>
        <label>每侧手续费假设（基点）<input v-model="config.fee_bps" inputmode="decimal" @input="feeConfirmed=false" /></label>
      </div>
      <details><summary>退出与预算参数</summary><div class="fields research-fields">
        <label>亏损阈值（%）<input v-model="config.stop_pct" inputmode="decimal" /></label>
        <label>整场损耗预算<input v-model="config.budget" inputmode="decimal" /></label>
        <label>退出预留（试验值）<input v-model="config.reserve" inputmode="decimal" /></label>
        <label>普通挂单超时（秒）<input v-model.number="config.wait_seconds" type="number" /></label>
        <label>主动退出重挂间隔（秒，试验值）<input v-model.number="config.exit_seconds" type="number" /></label>
        <p>主动退出采用最新已收盘一分钟 K 线收盘价。</p>
      <label>持仓退出时限（秒）<input v-model.number="config.max_hold_seconds" type="number" /></label>
        <label>目标预计积分<input v-model="config.target_points" inputmode="decimal" /></label>
        <label>每 U 买入积分<input v-model="config.points_per_u" inputmode="decimal" /></label>
      </div></details></fieldset>
      <p v-if="task" class="muted">当前任务参数已锁定。修改参数请新建模拟任务，原记录会保留。</p>
      <label class="check"><input v-model="feeConfirmed" type="checkbox" /> 我已核对手续费假设（买入和卖出各 {{config.fee_bps || '—'}} 基点 = {{feePercent}}%，按手动填写值计算）</label>
      <button :disabled="!feeConfirmed || !url.trim()" @click="refresh">{{busy?'读取中…':'获取行情并试算'}}</button>
      <label class="check"><input v-model="autoRefresh" type="checkbox" /> 每 10 秒刷新行情（不自动推进沙盒或填写浏览器）</label>
    </fieldset>
    <p v-if="error" class="notice error" role="alert">{{error}}</p>
    <h3 id="simulation-ledger">模拟账本与统计</h3>
    <p class="muted">仅统计人工录入的模拟成交，不代表账户实盘记录。买入额 × 积分倍率为预计积分；卖出不计分。盈亏包含计价币手续费，浮盈亏按可见买盘退出估算。</p>
    <div class="fields"><label>历史模拟任务<select v-model="selectedTask" :disabled="busy || !!pendingEvent"><option value="">请选择</option><option v-for="item in taskList" :key="item.id" :value="item.id">{{new Date(item.created_at*1000).toLocaleString()}} · {{item.symbol}} · {{item.completed?'已结束':'未结束'}} · {{item.id.slice(0,8)}}</option></select></label></div>
    <div class="actions wrap"><button class="secondary" :disabled="busy || !selectedTask" @click="loadTask(selectedTask)">载入任务记录</button><button class="secondary" :disabled="busy || !!pendingEvent" @click="reset">新建模拟任务（保留历史）</button></div>
    <p v-if="pendingEvent" class="notice">上次事件结果待确认，重复点击会使用同一事件编号。<button :disabled="busy" @click="retryEvent">重试未确认事件</button></p>
    <template v-if="task">
      <p>模拟任务 {{task.id.slice(0,8)}} · {{task.snapshot.token}} / {{task.snapshot.quote}} · {{state.message}}</p>
      <div class="metrics">
        <div><small>累计买入额</small><strong>{{fmt(task.summary.buy_total)}} {{task.snapshot.quote}}</strong></div>
        <div><small>预计积分 / 目标</small><strong>{{fmt(task.summary.estimated_points)}} / {{fmt(task.summary.target_points)}}</strong></div>
        <div><small>手续费合计</small><strong>{{fmt(task.summary.fees)}} {{task.snapshot.quote}}</strong></div>
        <div><small>已实现盈亏</small><strong>{{fmt(task.summary.realized_pnl)}} {{task.snapshot.quote}}</strong></div>
        <div><small>剩余持仓</small><strong>{{fmt(task.summary.inventory)}} {{task.snapshot.token}}</strong></div>
        <div><small>剩余持仓成本（含买入费）</small><strong>{{fmt(task.summary.remaining_cost)}}</strong></div>
        <div><small>预计浮盈亏（含退出费）</small><strong>{{valuationCurrent || task.summary.inventory==='0'?fmt(task.summary.unrealized_pnl):'行情过期，待估值'}}</strong></div>
        <div><small>累计盈亏（已实现 + 浮动）</small><strong>{{valuationCurrent || task.summary.inventory==='0'?fmt(task.summary.total_pnl):'行情过期，待估值'}}</strong></div>
      </div>
      <p class="muted">累计损耗预算已用 {{fmt(task.summary.session_loss)}}；盈利不抵回损耗预算。剩余目标买入额 {{fmt(task.summary.remaining_buy_amount)}}。最后一单按最小委托量和数量步长向上取整，可能略超目标，仍受单轮预算限制。重新获取行情并“评估当前状态”可更新估值。</p>
      <details><summary>委托记录（{{task.orders.length}}）</summary><div class="ledger-table"><table><thead><tr><th>委托编号</th><th>方向</th><th>价格</th><th>数量</th><th>已成交</th><th>状态</th></tr></thead><tbody><tr v-for="order in task.orders" :key="order.id"><td :title="order.id">{{order.id.slice(0,8)}}</td><td>{{order.side==='buy'?'买入':'卖出'}}</td><td>{{order.price}}</td><td>{{order.quantity}}</td><td>{{order.filled}}</td><td>{{statusLabel[order.status]}}</td></tr></tbody></table></div></details>
      <details><summary>成交记录（{{task.fills.length}}）</summary><div class="ledger-table"><table><thead><tr><th>模拟时间</th><th>方向</th><th>价格</th><th>数量</th><th>成交额</th><th>手续费</th></tr></thead><tbody><tr v-for="fill in task.fills" :key="fill.id"><td>{{new Date(fill.clock*1000).toLocaleString()}}</td><td>{{fill.side==='buy'?'买入':'卖出'}}</td><td>{{fill.price}}</td><td>{{fill.quantity}}</td><td>{{fill.gross}}</td><td>{{fill.fee}} {{fill.fee_currency}}</td></tr></tbody></table></div></details>
      <details><summary>任务事件记录（{{task.events.length}}）</summary><ul class="history"><li v-for="entry in task.events" :key="entry.id">{{new Date(entry.clock*1000).toLocaleString()}} · {{entry.message}}</li></ul></details>
    </template>
    <template v-else>
      <p class="notice">{{taskList.length?'尚未载入任务，请选择上方历史任务并点击“载入任务记录”。':'尚未创建模拟任务，因此还没有成交和统计数据。'}}</p>
      <div class="metrics">
        <div><small>累计买入额</small><strong>—</strong></div>
        <div><small>预计积分 / 目标</small><strong>— / {{fmt(config.target_points)}}</strong></div>
        <div><small>手续费合计</small><strong>—</strong></div>
        <div><small>已实现盈亏</small><strong>—</strong></div>
        <div><small>剩余持仓</small><strong>—</strong></div>
        <div><small>剩余持仓成本（含买入费）</small><strong>—</strong></div>
        <div><small>预计浮盈亏（含退出费）</small><strong>—</strong></div>
        <div><small>累计盈亏（已实现 + 浮动）</small><strong>—</strong></div>
      </div>
      <p class="muted">开始步骤：确认手续费 → 获取行情并试算 → 在下方沙盒点击“评估当前状态”创建任务 → 手动记录模拟成交。委托、成交明细和统计会随任务显示并保存。</p>
      <p class="muted">委托记录：尚未载入任务 · 成交记录：尚未载入任务。上方“新建模拟任务”用于准备一轮新的模拟，首次评估时才会保存任务。</p>
    </template>
    <template v-if="snapshot && estimate">
      <p :class="['notice',{error:outdated}]">{{snapshot.token}} / {{snapshot.quote}} · {{snapshot.symbol}} · 请求 {{snapshot.latency_ms}} ms · 行情距今 {{Math.max(0,Math.floor((now-snapshot.fetched_at)/1000))}} 秒。{{outdated?'数据过期或参数已变化，请刷新。':'可用于本次试算。'}}</p>
      <div class="metrics"><div><small>成交量加权均价</small><strong>{{fmt(estimate.center)}}</strong></div><div><small>收盘价标准差</small><strong>{{fmt(estimate.volatility)}}</strong></div><div><small>建议买价</small><strong>{{fmt(estimate.buy)}}</strong></div><div><small>历史模型卖价</small><strong>{{fmt(estimate.sell)}}</strong></div></div>
      <p class="muted">建议买入数量 {{fmt(estimate.quantity)}} {{snapshot.token}}；24h 成交量 {{fmt(snapshot.ticker.volume)}} {{snapshot.token}}，成交额 {{fmt(snapshot.ticker.quoteVolume)}} {{snapshot.quote}}。</p>
      <p v-for="warning in estimate.warnings" :key="warning" class="muted">{{warning}}</p>
      <h3>1 分钟 K 线与成交量（仅已收盘，最多 60 根）</h3>
      <svg viewBox="0 0 800 235" role="img" aria-label="一分钟K线与成交量" class="chart">
        <line x1="0" y1="170" x2="800" y2="170" stroke="#455670" />
        <g v-for="r in chart" :key="r.time"><title>{{new Date(r.time).toLocaleTimeString()}} 开 {{r.open}} 高 {{r.high}} 低 {{r.low}} 收 {{r.close}} 量 {{r.volume}}</title><line :x1="r.x" :x2="r.x" :y1="r.highY" :y2="r.lowY" :stroke="r.color"/><rect :x="r.x-r.width/2" :y="r.top" :width="r.width" :height="r.height" :fill="r.color"/><rect :x="r.x-r.width/2" :y="230-r.volHeight" :width="r.width" :height="r.volHeight" :fill="r.color" opacity=".6"/></g>
      </svg>
      <p class="muted">{{candles[0]?new Date(candles[0].time).toLocaleString():''}} → {{candles.length?new Date(candles[candles.length-1]!.time).toLocaleString():''}}。悬停查看单根数据；图表使用数值近似，策略计算使用 Decimal。</p>
      <h3>订单流程沙盒</h3>
      <p class="muted">时间按钮推进虚拟时钟，复用当前 K 线快照；不是历史回测。成交由你手动输入，触价不会自动成交。亏损在虚拟自然分钟结束检查。任务、委托和成交记录保存在本机，刷新后可恢复。按任务累计，不自动跨日清零。</p>
      <p>状态：{{state.message ?? '未开始'}}<br/>剩余持仓 {{fmt(state.inventory)}} · 本轮成本 {{fmt(state.cost)}} · 已卖出净收入 {{fmt(state.proceeds)}} · 累计损耗 {{fmt(state.session_loss)}}</p>
      <p v-if="state.first_buy_at != null">本轮已持仓 {{Math.floor(((state.clock ?? state.first_buy_at)-state.first_buy_at)/60)}} 分钟；{{config.max_hold_seconds/60}} 分钟后转主动退出。当前主动退出：{{state.exiting?'是':'否'}}。</p>
      <p v-if="state.order">模拟{{state.order.side==='buy'?'买':'卖'}}单：{{state.order.price}} × {{state.order.quantity}}；已成交 {{state.order.filled}}；{{state.order.cancel_requested?'等待撤单确认':'挂单等待'}}</p>
      <div class="actions wrap"><button :disabled="busy||outdated||!!pendingEvent" @click="simulate('tick')">评估当前状态</button><button :disabled="busy||outdated||!!pendingEvent" @click="simulate('tick',60)">推进 1 分钟</button><button :disabled="busy||outdated||!!pendingEvent" @click="simulate('tick',300)">推进 5 分钟</button><button :disabled="busy||outdated||!!pendingEvent" @click="simulate('tick',config.exit_seconds)">推进退出间隔</button></div>
      <div class="fields"><label>模拟本次成交数量<input v-model="quantity" inputmode="decimal" /></label><label>模拟成交价格<input v-model="fillPrice" inputmode="decimal" /></label></div>
      <div class="actions wrap"><button :disabled="busy||outdated||!!pendingEvent||!state.order" @click="simulate('fill')">记录模拟成交</button><button :disabled="busy||outdated||!!pendingEvent||!state.order?.cancel_requested" @click="simulate('cancel_confirm')">确认模拟撤单</button><button class="secondary" :disabled="busy" @click="reset">新建模拟任务（保留历史）</button></div>
      <ul class="history"><li v-for="(line,i) in logs" :key="i">{{line}}</li></ul>
    </template>
  </section>
</template>

<style scoped>
.ledger-table{overflow-x:auto}fieldset {border:0;padding:0;margin:0;min-width:0}.research-fields{grid-template-columns:repeat(3,minmax(0,1fr))}.check{display:flex;gap:10px;align-items:center;margin-top:20px;line-height:1.6}.check input{width:auto;margin:0}.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.metrics div{padding:14px;background:#101723;border-radius:8px}.metrics small{display:block;color:#a7b5c8}.metrics strong{display:block;margin-top:10px;overflow-wrap:anywhere}.chart{width:100%;background:#101723;border-radius:8px}.book{display:grid;grid-template-columns:1fr 1fr;gap:20px}table{width:100%;font-size:13px;text-align:right;border-collapse:collapse}td,th{padding:8px;border-bottom:1px solid #314057}.wrap{flex-wrap:wrap}details{margin-top:20px}summary{cursor:pointer;margin-bottom:15px}h3{margin-top:26px;font-size:16px}@media(max-width:640px){.research-fields,.metrics,.book{grid-template-columns:1fr 1fr}.book{grid-template-columns:1fr}}
</style>
