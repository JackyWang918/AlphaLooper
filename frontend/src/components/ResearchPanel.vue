<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'

type Candle = { time: number; close_time: number; open: string; high: string; low: string; close: string; volume: string; quote_volume: string }
type Snapshot = { symbol: string; token: string; quote: string; fetched_at: number; book_time: number; latency_ms: number; candles: Candle[]; bids: [string,string][]; asks: [string,string][]; ticker: Record<string,string> }
type Estimate = { center: string; volatility: string; buy: string; sell: string; quantity: string; warnings: string[] }
type Order = { side: string; price: string; quantity: string; filled: string; placed_at: number; cancel_requested: boolean }
type State = { symbol?: string; clock?: number; inventory?: string; cost?: string; proceeds?: string; session_loss?: string; exiting?: boolean; budget_stopped?: boolean; message?: string; order?: Order | null }
type Risk = { covered: boolean; loss: string | null; loss_pct: string | null; exit_net: string | null }
const props = defineProps<{ url: string }>()
const quote = ref('USDT')
const config = ref({ amount:'50', window:15, buy_offset:'0.5', sell_offset:'0.5', fee_bps:'1', stop_pct:'2', budget:'10', reserve:'2', wait_seconds:300, exit_seconds:15, exit_level:6 })
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
const risk = ref<Risk | null>(null)
const busy = ref(false)
const error = ref('')
const quantity = ref('')
const fillPrice = ref('')
const logs = ref<string[]>([])
const fingerprint = ref('')
const simulationKey = ref('')
const now = ref(Date.now())
const autoRefresh = ref(false)
const lastAttempt = ref(0)
const timer = setInterval(() => {
  now.value=Date.now()
  if(autoRefresh.value && feeConfirmed.value && !busy.value && now.value-lastAttempt.value>=10000) void refresh()
},1000)
onBeforeUnmount(() => clearInterval(timer))
const key = computed(() => JSON.stringify([props.url, quote.value, config.value]))
const outdated = computed(() => !snapshot.value || fingerprint.value !== key.value || now.value-snapshot.value.book_time>15000)
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
async function api(path:string, body:unknown) {
  const response=await fetch(`/api/research/${path}`,{method:'POST', headers:{'Content-Type':'application/json','X-AlphaLooper-Client':'local-ui'},body:JSON.stringify(body), signal:AbortSignal.timeout(65000)})
  const result=await response.json()
  if(!response.ok) throw new Error(typeof result.detail==='string'?result.detail:'参数无效，请检查数值范围。')
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
function reset() { state.value={}; risk.value=null; simulationKey.value=''; logs.value=[]; quantity.value=''; fillPrice.value='' }
async function simulate(kind:string, advance=0) {
  if(busy.value || outdated.value || !feeConfirmed.value) return
  if(simulationKey.value && simulationKey.value!==key.value) { error.value='沙盒参数或币种已变化，请重置沙盒后继续。'; return }
  busy.value=true; error.value=''
  try {
    const result=await api('simulate',{state:state.value,event:{id:crypto.randomUUID(),kind,advance_seconds:advance,quantity:kind==='fill'?quantity.value:'0',price:kind==='fill'?fillPrice.value:'0'},snapshot:snapshot.value,config:config.value})
    state.value=result.state; risk.value=result.risk; simulationKey.value=key.value
    logs.value.unshift(`${new Date((state.value.clock ?? 0)*1000).toLocaleTimeString()} · ${state.value.message}`)
    logs.value=logs.value.slice(0,100)
    fillPrice.value=state.value.order?.price ?? ''
  } catch(e) { error.value=e instanceof Error?e.message:'模拟失败' }
  finally {busy.value=false}
}
</script>

<template>
  <section class="research">
    <div class="section-title"><h2>行情与策略研究</h2><span class="badge">模拟专用 · 不连接下单</span></div>
    <p class="muted">使用上方交易链接读取公开行情，无需登录。研究参数为初始试验值，尚未验证收益或损耗。</p>
    <fieldset :disabled="busy">
      <div class="fields research-fields">
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
        <label>主动退出买盘档位<input v-model.number="config.exit_level" type="number" /></label>
      </div></details>
      <label class="check"><input v-model="feeConfirmed" type="checkbox" /> 我已核对手续费假设（买入和卖出各 {{config.fee_bps || '—'}} 基点 = {{feePercent}}%，按手动填写值计算）</label>
      <button :disabled="!feeConfirmed || !url.trim()" @click="refresh">{{busy?'读取中…':'获取行情并试算'}}</button>
      <label class="check"><input v-model="autoRefresh" type="checkbox" /> 每 10 秒刷新行情（不自动推进沙盒或填写浏览器）</label>
    </fieldset>
    <p v-if="error" class="notice error" role="alert">{{error}}</p>
    <template v-if="snapshot && estimate">
      <p :class="['notice',{error:outdated}]">{{snapshot.token}} / {{snapshot.quote}} · {{snapshot.symbol}} · 请求 {{snapshot.latency_ms}} ms · 盘口距今 {{Math.max(0,Math.floor((now-snapshot.book_time)/1000))}} 秒。{{outdated?'数据过期或参数已变化，请刷新。':'可用于本次试算。'}}</p>
      <div class="metrics"><div><small>成交量加权均价</small><strong>{{fmt(estimate.center)}}</strong></div><div><small>收盘价标准差</small><strong>{{fmt(estimate.volatility)}}</strong></div><div><small>建议买价</small><strong>{{fmt(estimate.buy)}}</strong></div><div><small>历史模型卖价</small><strong>{{fmt(estimate.sell)}}</strong></div></div>
      <p class="muted">建议买入数量 {{fmt(estimate.quantity)}} {{snapshot.token}}；24h 成交量 {{fmt(snapshot.ticker.volume)}} {{snapshot.token}}，成交额 {{fmt(snapshot.ticker.quoteVolume)}} {{snapshot.quote}}。</p>
      <p v-for="warning in estimate.warnings" :key="warning" class="muted">{{warning}}</p>
      <h3>1 分钟 K 线与成交量（仅已收盘，最多 60 根）</h3>
      <svg viewBox="0 0 800 235" role="img" aria-label="一分钟K线与成交量" class="chart">
        <line x1="0" y1="170" x2="800" y2="170" stroke="#455670" />
        <g v-for="r in chart" :key="r.time"><title>{{new Date(r.time).toLocaleTimeString()}} 开 {{r.open}} 高 {{r.high}} 低 {{r.low}} 收 {{r.close}} 量 {{r.volume}}</title><line :x1="r.x" :x2="r.x" :y1="r.highY" :y2="r.lowY" :stroke="r.color"/><rect :x="r.x-r.width/2" :y="r.top" :width="r.width" :height="r.height" :fill="r.color"/><rect :x="r.x-r.width/2" :y="230-r.volHeight" :width="r.width" :height="r.volHeight" :fill="r.color" opacity=".6"/></g>
      </svg>
      <p class="muted">{{candles[0]?new Date(candles[0].time).toLocaleString():''}} → {{candles.length?new Date(candles[candles.length-1]!.time).toLocaleString():''}}。悬停查看单根数据；图表使用数值近似，策略计算使用 Decimal。</p>
      <div class="book"><div v-for="(rows,side) in {bids:snapshot.bids,asks:snapshot.asks}" :key="side"><h3>{{side==='bids'?'买盘':'卖盘'}}</h3><table><thead><tr><th>档</th><th>价格</th><th>数量</th></tr></thead><tbody><tr v-for="(row,i) in rows.slice(0,6)" :key="i"><td>{{i+1}}</td><td>{{fmt(row[0])}}</td><td>{{fmt(row[1])}}</td></tr></tbody></table></div></div>
      <h3>订单流程沙盒</h3>
      <p class="muted">时间按钮推进虚拟时钟，复用当前盘口快照；不是历史回测。成交由你手动输入，触价不会自动成交。亏损在虚拟自然分钟结束检查。刷新网页会清空沙盒。</p>
      <p>状态：{{state.message ?? '未开始'}}<br/>剩余持仓 {{fmt(state.inventory)}} · 本轮成本 {{fmt(state.cost)}} · 已卖出净收入 {{fmt(state.proceeds)}} · 累计损耗 {{fmt(state.session_loss)}}</p>
      <p v-if="risk">预计本轮退出亏损：{{risk.covered?`${fmt(risk.loss)}（${fmt(risk.loss_pct)}%）`:'买盘不足，无法估算全部退出成本'}}；主动退出 {{state.exiting?'是':'否'}}。</p>
      <p v-if="state.order">模拟{{state.order.side==='buy'?'买':'卖'}}单：{{state.order.price}} × {{state.order.quantity}}；已成交 {{state.order.filled}}；{{state.order.cancel_requested?'等待撤单确认':'挂单等待'}}</p>
      <div class="actions wrap"><button :disabled="busy||outdated" @click="simulate('tick')">评估当前状态</button><button :disabled="busy||outdated" @click="simulate('tick',60)">推进 1 分钟</button><button :disabled="busy||outdated" @click="simulate('tick',300)">推进 5 分钟</button><button :disabled="busy||outdated" @click="simulate('tick',config.exit_seconds)">推进退出间隔</button></div>
      <div class="fields"><label>模拟本次成交数量<input v-model="quantity" inputmode="decimal" /></label><label>模拟成交价格<input v-model="fillPrice" inputmode="decimal" /></label></div>
      <div class="actions wrap"><button :disabled="busy||outdated||!state.order" @click="simulate('fill')">记录模拟成交</button><button :disabled="busy||outdated||!state.order?.cancel_requested" @click="simulate('cancel_confirm')">确认模拟撤单</button><button class="secondary" :disabled="busy" @click="reset">重置沙盒</button></div>
      <ul class="history"><li v-for="(line,i) in logs" :key="i">{{line}}</li></ul>
    </template>
  </section>
</template>

<style scoped>
fieldset {border:0;padding:0;margin:0;min-width:0}.research-fields{grid-template-columns:repeat(3,minmax(0,1fr))}.check{display:flex;gap:10px;align-items:center;margin-top:20px;line-height:1.6}.check input{width:auto;margin:0}.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.metrics div{padding:14px;background:#101723;border-radius:8px}.metrics small{display:block;color:#a7b5c8}.metrics strong{display:block;margin-top:10px;overflow-wrap:anywhere}.chart{width:100%;background:#101723;border-radius:8px}.book{display:grid;grid-template-columns:1fr 1fr;gap:20px}table{width:100%;font-size:13px;text-align:right;border-collapse:collapse}td,th{padding:8px;border-bottom:1px solid #314057}.wrap{flex-wrap:wrap}details{margin-top:20px}summary{cursor:pointer;margin-bottom:15px}h3{margin-top:26px;font-size:16px}@media(max-width:640px){.research-fields,.metrics,.book{grid-template-columns:1fr 1fr}.book{grid-template-columns:1fr}}
</style>
