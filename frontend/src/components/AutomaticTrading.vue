<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import DecisionLog from './DecisionLog.vue'
const props = defineProps<{url:string; connected:boolean; symbol?:string; quote?:string; fillSupported?:boolean; browserBusy?:boolean; browserReason?:string}>()
const emit=defineEmits<{refreshBrowser:[]}>()
type Task = {id:string; active:boolean; phase:string; message:string; request:{expected_symbol:string; expected_quote:string; config:{target_points:string; buy_check_seconds?:number}}; inventory:string; cost:string; proceeds:string; buy_total:string; fees:string; realized_pnl:string; session_loss:string; rounds:number; stop_buying:boolean; first_buy_at:number|null; pending:{request_id:string;side:string;price:string;quantity:string}|null; pending_order?:{state:string;message:string;submission_error?:string}; estimate?:{buy:string;best_bid?:string;best_ask?:string;buy_blockers?:string[];warnings?:string[]}; market_at?:number; next_buy_check_at?:number; risk:{exit_net:string|null; loss:string|null}|null}
const current=ref<Task|null>(null),recent=ref<Task[]>([]),running=ref(false),busy=ref(false),error=ref('')
const statusReady=ref(false),statusError=ref(''),notice=ref('')
const confirmedNotSubmitted=ref(false)
const blockedReason=computed(()=>{
  if(busy.value)return '正在处理，请稍候。'
  if(props.browserBusy)return '正在识别交易页面，请稍候。'
  if(!statusReady.value)return statusError.value||'正在读取自动任务状态。'
  if(current.value)return '已有自动任务，请使用下方的恢复或结束操作。'
  if(!props.connected)return '尚未连接受控 Chrome。请先在“01 / 浏览器连接”启动 Chrome，再点击这里的“重新识别交易页面”。'
  if(!props.fillSupported||!props.symbol||!props.quote)return '尚未识别交易表单。请在受控 Chrome 打开目标币种、完成登录和平台提示，再点击“重新识别交易页面”。'
  if(!props.url.trim())return '请先填写目标币种的交易链接。'
  return ''
})
const book=ref('本机账户'),amount=ref('50'),target=ref('32768'),windowSize=ref(15),buyOffset=ref('0.5'),sellOffset=ref('0.5'),buyCheckSeconds=ref(5)
let timer:ReturnType<typeof setInterval>|undefined
let refreshPromise:Promise<void>|null=null
let requestId=localStorage.getItem('automatic-request-id')||crypto.randomUUID()
localStorage.setItem('automatic-request-id',requestId)
async function api(path:string,body?:unknown){
  const res=await fetch(`/api/automatic${path}`,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json','X-AlphaLooper-Client':'local-ui'},body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(120000)})
  const value=await res.json()
  if(!res.ok)throw new Error(typeof value.detail==='string'?value.detail:'请检查参数及后端迁移。')
  return value
}
function refresh():Promise<void>{
  if(refreshPromise)return refreshPromise
  refreshPromise=(async()=>{
    try{
      const result=await api('');current.value=result.current;recent.value=result.recent;running.value=result.running;statusReady.value=true;statusError.value=''
      if(!current.value&&recent.value.some(task=>task.id===requestId&&!task.active)){
        requestId=crypto.randomUUID();localStorage.setItem('automatic-request-id',requestId)
      }
    }
    catch(e){statusReady.value=false;statusError.value=e instanceof Error?e.message:'读取任务失败'}
    finally{refreshPromise=null}
  })()
  return refreshPromise
}
async function start(){
  if(blockedReason.value)return
  busy.value=true;error.value='';notice.value=''
  try{
    await refresh()
    if(!statusReady.value||current.value)return
    const task=await api('/start',{request_id:requestId,book:book.value,url:props.url,expected_symbol:props.symbol,expected_quote:props.quote,config:{amount:amount.value,target_points:target.value,window:windowSize.value,buy_offset:buyOffset.value,sell_offset:sellOffset.value,buy_check_seconds:buyCheckSeconds.value}})
    // Rotate only after a definite response. A lost response retries the same ID.
    requestId=crypto.randomUUID();localStorage.setItem('automatic-request-id',requestId)
    if(task.active)current.value=task
    else notice.value='上一任务已结束。再次点击“启动自动实盘买卖”即可开始新任务。'
    await refresh()
  }catch(e){error.value=e instanceof Error?e.message:'启动失败'}finally{busy.value=false}
}
async function control(action:string){
  if(!current.value||busy.value)return
  busy.value=true;error.value=''
  try{await api('/control',{task_id:current.value.id,action});await refresh()}
  catch(e){error.value=e instanceof Error?e.message:'操作失败'}finally{busy.value=false}
}
async function resolveUnsubmitted(){
  if(!current.value||!confirmedNotSubmitted.value||busy.value)return
  busy.value=true;error.value='';notice.value=''
  try{
    const task=await api('/resolve-unsubmitted',{task_id:current.value.id,confirmed_not_submitted:true})
    confirmedNotSubmitted.value=false
    notice.value=task.message
    await refresh()
  }catch(e){error.value=e instanceof Error?e.message:'核对未提交状态失败'}finally{busy.value=false}
}
onMounted(()=>{refresh();timer=setInterval(refresh,5000)})
onUnmounted(()=>clearInterval(timer))
</script>

<template>
<section>
  <div class="section-title"><h2>05 / 自动实盘交易</h2><span class="badge">{{running?'自动运行中':current?'已暂停 / 待恢复':'未启动'}}</span></div>
  <p>启动后程序自动估价、买入、卖出并继续下一轮。请先在受控 Chrome 登录、打开目标币种，再刷新浏览器识别结果。</p>
  <p class="muted">每单等待 5 分钟；每分钟检查 2% 损耗；持仓满 30 分钟转主动退出；损耗预算 10 U；达标后停止买入并清仓。手续费按每侧 0.01% 估算。当前按本任务累计，不在午夜重置。</p>
  <p>受控 Chrome：<span v-if="connected">已连接</span><span v-else>未连接</span> · 交易表单：<span v-if="fillSupported&&symbol&&quote">{{symbol}} / {{quote}}</span><span v-else>尚未识别</span></p>
  <p v-if="browserReason&&!fillSupported" class="muted">页面识别反馈：{{browserReason}}</p>
  <button class="secondary" :disabled="busy||browserBusy" @click="emit('refreshBrowser')">{{browserBusy?'正在识别…':'重新识别交易页面'}}</button>
  <button v-if="statusError" class="secondary" :disabled="busy" @click="refresh">重试读取任务状态</button>
  <fieldset :disabled="busy||!!current">
    <div class="fields">
      <label>账本<input v-model="book" /></label>
      <label>每轮金额（最多 50 U，含估算费用）<input v-model="amount" inputmode="decimal" /></label>
      <label>本任务目标积分（最多 32,768）<input v-model="target" inputmode="decimal" /></label>
    </div>
    <details><summary>估价试验参数</summary><div class="fields">
      <label>1 分钟 K 线窗口<input v-model.number="windowSize" type="number" min="3" max="240" /></label>
      <label>买入波动偏移系数<input v-model="buyOffset" inputmode="decimal" /></label>
      <label>卖出波动偏移系数<input v-model="sellOffset" inputmode="decimal" /></label>
      <label>等待买入时的估价间隔（秒）<input v-model.number="buyCheckSeconds" type="number" min="5" max="300" step="5" /></label>
    </div><p class="muted">默认 15 根 K 线、偏移各 0.5。主动退出采用最新已收盘一分钟 K 线收盘价，每 15 秒尝试撤单核对后重估。参数效果尚未验证。</p></details>
    <p class="muted">估价间隔可设为 5–300 秒（5 秒的倍数），60 表示约一分钟；仅用于空仓等待买入，不改变每分钟订单巡检、损耗检查或退出规则。网络和页面处理会增加实际间隔。启动后参数固定。</p>
    <button :disabled="!!blockedReason" :title="blockedReason" @click="start">{{busy?'正在处理…':'启动自动实盘买卖'}}</button>
  </fieldset>
  <p v-if="blockedReason" class="notice" role="status">暂不能启动：{{blockedReason}}</p>
  <p v-else class="muted">交易页面已识别，可以启动。任务结束后可直接再次启动；每个新任务独立累计目标和预算，继续原任务请用“核对后恢复任务”。</p>
  <p v-if="notice" class="notice" role="status">{{notice}}</p>
  <p v-if="error" class="notice error" role="alert">{{error}}</p>
  <div v-if="current">
    <p class="notice" role="status">{{current.request.expected_symbol}} / {{current.request.expected_quote}}：{{current.message}}</p>
    <p>本任务等待买入估价间隔：{{current.request.config.buy_check_seconds??5}} 秒。<span v-if="current.market_at">最近行情：{{new Date(current.market_at).toLocaleTimeString()}}。</span></p>
    <div v-if="current.estimate&&!current.pending&&current.inventory==='0'">
      <p>上次买入评估：建议买价 {{current.estimate.buy}}</p>
      <ul><li v-for="reason in (current.estimate.buy_blockers??current.estimate.warnings?.filter(w=>w.includes('暂停模拟新买入')||w.includes('历史买价已触及卖一'))??[])" :key="reason">{{reason}}</li></ul>
    </div>
    <p v-if="!running" class="muted">当前不会自动提交或撤单。处理提示后点击“核对后恢复任务”；后端重启也需要手动恢复。</p>
    <p>已完成 {{current.rounds}} 轮 · 累计买入 {{current.buy_total}} U · 目标 {{current.request.config.target_points}} 分（买入额 × 4）</p>
    <p>任务持仓 {{current.inventory}} · 本轮投入成本 {{current.cost}} U · 本轮卖出净收入 {{current.proceeds}} U</p>
    <p>已结束轮次预计盈亏 {{current.realized_pnl}} U · 累计亏损轮次损耗 {{current.session_loss}} / 10 U · 估算手续费 {{current.fees}} U</p>
    <p v-if="current.risk">按最新已收盘 K 线估算退出净收入：{{current.risk.exit_net??'未知'}} U；本轮预计损耗：{{current.risk.loss??'未知'}} U。</p>
    <p v-if="current.first_buy_at">持仓计时起点：{{new Date(current.first_buy_at*1000).toLocaleString()}}（汇总订单无法提供首笔成交精确时间，保守使用买单提交时间）。</p>
    <p v-if="current.pending">当前计划：{{current.pending.side==='buy'?'买入':'卖出'}} {{current.pending.quantity}} @ {{current.pending.price}}</p>
    <div v-if="current.pending_order?.state==='submission_unknown'" class="notice error">
      <p>这笔订单没有完成二次确认，程序正在等待你核对。首次错误：{{current.pending_order.submission_error||current.pending_order.message}}</p>
      <p>请关闭仍然显示的订单确认弹窗，并确认币安“当前委托”中没有这笔订单。程序还会核对当前无挂单且历史订单没有变化。</p>
      <label><input v-model="confirmedNotSubmitted" type="checkbox" :disabled="busy" />我已关闭确认弹窗，并确认平台没有这笔订单。</label>
      <button class="secondary" :disabled="busy||!confirmedNotSubmitted" @click="resolveUnsubmitted">核对未提交并结束旧任务</button>
    </div>
    <div class="actions">
      <button class="secondary" :disabled="busy||!running" @click="control('pause')">暂停自动操作</button>
      <button :disabled="busy||running" @click="control('resume')">核对后恢复任务</button>
      <button class="secondary" :disabled="busy" @click="control('finish')">停止买入，卖完结束</button>
    </div>
    <p class="muted">暂停不撤挂单，只读巡检继续。后端重启后保持暂停，需点击恢复。页面异常、撤单结果未知、持仓不符或余量不足最小委托时会停下并保留记录；不会将余量当作清仓。启动前已有的代币余额不纳入本任务。</p>
  </div>
  <details v-if="recent.length"><summary>最近任务</summary><ul><li v-for="task in recent" :key="task.id">{{task.id.slice(0,8)}} · {{task.request.expected_symbol}} · {{task.message}} · 买入 {{task.buy_total}} U · 盈亏 {{task.realized_pnl}} U</li></ul></details>
  <DecisionLog :tasks="recent" :current-id="current?.id" />
</section>
</template>

<style scoped>fieldset{border:0;padding:0;margin:0}details{margin:16px 0}summary{cursor:pointer;margin-bottom:12px}</style>
