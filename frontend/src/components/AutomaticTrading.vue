<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import DecisionLog from './DecisionLog.vue'
const props = defineProps<{url:string; connected:boolean; symbol?:string; quote?:string; fillSupported?:boolean; browserBusy?:boolean; browserReason?:string}>()
const emit=defineEmits<{refreshBrowser:[];openTaskPage:[url:string]}>()
type Task = {accounting_version?:number;round_start_quote?:string|null;round_plan?:string;buy_rehangs?:number;dust?:string;balances?:{quote_available:string;base_available:string};id:string; active:boolean; phase:string; message:string; request:{url:string;expected_symbol:string; expected_quote:string; config:{target_points:string; current_points?:string; points_per_u?:string; buy_check_seconds?:number}}; inventory:string; cost:string; proceeds:string; buy_total:string; fees:string; realized_pnl:string; session_loss:string; rounds:number; stop_buying:boolean; first_buy_at:number|null; pending:{request_id:string;side:string;price:string;quantity:string;quote_amount?:string}|null; pending_order?:{state:string;message:string;submission_error?:string}; estimate?:{buy:string;buy_blockers?:string[];warnings?:string[]}; market_at?:number; next_buy_check_at?:number; schedule?:{kind:string;at:number|null;reason:string}; risk:{equity:string|null; loss:string|null;loss_pct:string|null;denominator?:string;reason?:string}|null}
const current=ref<Task|null>(null),recent=ref<Task[]>([]),running=ref(false),busy=ref(false),error=ref('')
const statusReady=ref(false),statusError=ref(''),notice=ref('')
const confirmedNotSubmitted=ref(false)
const MAX_BUY_AMOUNT=2000n
function positiveDecimal(value:string){
  if(value.length>40||!/^\d+(?:\.\d+)?$/.test(value))return null
  const [whole,fraction='']=value.split('.')
  const units=BigInt(whole+fraction)
  return units>0n?{units,scale:10n**BigInt(fraction.length)}:null
}
const amountProblem=computed(()=>{
  const parsed=positiveDecimal(amount.value)
  if(!parsed)return '请输入大于 0 的每轮计划买入金额。'
  if(parsed.units>MAX_BUY_AMOUNT*parsed.scale)return '每轮计划买入金额不能超过 2000 U。'
  return ''
})
const blockedReason=computed(()=>{
  if(busy.value)return '正在处理，请稍候。'
  if(props.browserBusy)return '正在识别交易页面，请稍候。'
  if(!statusReady.value)return statusError.value||'正在读取自动任务状态。'
  if(current.value)return '已有自动任务，请使用下方的恢复或结束操作。'
  if(!props.connected)return '尚未连接受控 Chrome。请先在“01 / 浏览器连接”启动 Chrome，再点击这里的“重新识别交易页面”。'
  if(!props.fillSupported||!props.symbol||!props.quote)return '尚未识别交易表单。请在受控 Chrome 打开目标币种、完成登录和平台提示，再点击“重新识别交易页面”。'
  if(!props.url.trim())return '请先填写目标币种的交易链接。'
  if(!book.value.trim())return '请填写账本名称。'
  if(amountProblem.value)return amountProblem.value
  return ''
})
const book=ref('本机账户'),amount=ref('50'),target=ref('32768'),currentPoints=ref('0'),windowSize=ref(15),buyOffset=ref('0.5'),sellOffset=ref('0.5'),rangeWeight=ref('0.5'),buyCheckSeconds=ref(20)
const requiredPoints=computed(()=>{
  const goal=Number(target.value),existing=Number(currentPoints.value)
  return Number.isFinite(goal)&&Number.isFinite(existing)&&existing<=goal?goal-existing:null
})
function fmtPoints(value:number){return Number.isFinite(value)?value.toLocaleString('zh-CN',{maximumFractionDigits:8}):'—'}
function fmtNextAction(task:Task){
  if(!task.schedule?.at)return '等待人工操作'
  const seconds=Math.max(0,Math.ceil(task.schedule.at-Date.now()/1000))
  return `${new Date(task.schedule.at*1000).toLocaleTimeString()}（约 ${seconds} 秒后）`
}
function earnedPoints(task:Task){return Number(task.buy_total)*Number(task.request.config.points_per_u??4)}
function totalPoints(task:Task){return Number(task.request.config.current_points??0)+earnedPoints(task)}
let timer:ReturnType<typeof setInterval>|undefined
let refreshPromise:Promise<void>|null=null
let requestId=localStorage.getItem('automatic-request-id')||crypto.randomUUID()
localStorage.setItem('automatic-request-id',requestId)
async function api(path:string,body?:unknown){
  const res=await fetch(`/api/automatic${path}`,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json','X-AlphaLooper-Client':'local-ui'},body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(120000)})
  const value=await res.json().catch(()=>null)
  if(!res.ok){
    if(typeof value?.detail==='string')throw new Error(value.detail)
    if(Array.isArray(value?.detail)){
      const labels:Record<string,string>={amount:'每轮计划买入金额',target_points:'本任务目标积分',current_points:'启动时已有积分',window:'K 线窗口',buy_offset:'买入波动偏移系数',sell_offset:'卖出波动偏移系数',range_weight:'典型振幅权重',buy_check_seconds:'空仓再次评估等待',book:'账本',url:'交易链接',expected_symbol:'交易币种',expected_quote:'计价币'}
      const messages=value.detail.map((item:{loc?:unknown[];type?:string;msg?:string;ctx?:Record<string,unknown>})=>{
        const field=String(item.loc?.at(-1)??'请求参数'),label=labels[field]??field,ctx=item.ctx??{}
        if(item.type==='less_than_equal')return `${label}不能超过 ${ctx.le}。`
        if(item.type==='greater_than')return `${label}必须大于 ${ctx.gt}。`
        if(item.type==='greater_than_equal')return `${label}不能小于 ${ctx.ge}。`
        if(item.type==='multiple_of')return `${label}必须是 ${ctx.multiple_of} 的倍数。`
        const original=(item.msg??'').replace(/^Value error,\s*/,'')
        if(/[\u3400-\u9fff]/.test(original))return original
        return `${label}格式或取值无效。`
      })
      throw new Error(messages.join(' '))
    }
    throw new Error(`请求失败（HTTP ${res.status}），后端没有返回可读的错误原因。请查看后端终端日志。`)
  }
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
    const task=await api('/start',{request_id:requestId,book:book.value,url:props.url,expected_symbol:props.symbol,expected_quote:props.quote,config:{amount:amount.value,target_points:target.value,current_points:currentPoints.value,window:windowSize.value,buy_offset:buyOffset.value,sell_offset:sellOffset.value,range_weight:rangeWeight.value,buy_check_seconds:buyCheckSeconds.value}})
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
async function forceRestart(){
  if(!current.value||busy.value)return
  if(!window.confirm('确定强制结束当前本地任务吗？平台挂单不会自动撤销；启动新任务前仍需确认平台没有旧挂单。'))return
  busy.value=true;error.value='';notice.value=''
  try{
    const task=await api('/control',{task_id:current.value.id,action:'force_restart'})
    notice.value=task.message
    await refresh()
  }catch(e){error.value=e instanceof Error?e.message:'强制重新开始失败'}finally{busy.value=false}
}
onMounted(()=>{refresh();timer=setInterval(refresh,5000)})
onUnmounted(()=>clearInterval(timer))
</script>

<template>
<section>
  <div class="section-title"><h2>05 / 自动实盘交易</h2><span class="badge">{{running?'自动运行中':current?'已暂停 / 待恢复':'未启动'}}</span></div>
  <p>启动后程序自动估价、买入、卖出并继续下一轮。请先在受控 Chrome 登录、打开目标币种，再刷新浏览器识别结果。</p>
  <p class="muted">普通挂单等待 5 分钟，首次买入之外最多撤单重挂 10 次；累计投入超过本轮计划金额 50% 后转卖。每分钟检查资产损耗，2% 的分母为本轮买入前总 USDT；持仓满 30 分钟转主动退出。损耗预算 10 U，按任务累计。</p>
  <p>受控 Chrome：<span v-if="connected">已连接</span><span v-else>未连接</span> · 交易表单：<span v-if="fillSupported&&symbol&&quote">{{symbol}} / {{quote}}</span><span v-else>尚未识别</span></p>
  <p v-if="browserReason&&!fillSupported" class="muted">页面识别反馈：{{browserReason}}</p>
  <button class="secondary" :disabled="busy||browserBusy" @click="emit('refreshBrowser')">{{browserBusy?'正在识别…':'重新识别交易页面'}}</button>
  <button v-if="statusError" class="secondary" :disabled="busy" @click="refresh">重试读取任务状态</button>
  <fieldset :disabled="busy||!!current">
    <div class="fields">
      <label>账本<input v-model="book" /></label>
      <label>每轮计划买入金额（最多 2,000 U）<input v-model="amount" inputmode="decimal" /></label>
      <label>本任务目标积分（最多 32,768）<input v-model="target" inputmode="decimal" /></label>
      <label>启动时已有积分<input v-model="currentPoints" inputmode="decimal" /></label>
    </div>
    <p class="muted">本任务还需新增：{{requiredPoints===null?'请检查积分输入':fmtPoints(requiredPoints)}} 分。启动后已有积分固定，任务只累计实际买入金额 × 4；卖出不计分。</p>
    <details><summary>估价试验参数</summary><div class="fields">
      <label>1 分钟 K 线窗口<input v-model.number="windowSize" type="number" min="3" max="240" /></label>
      <label>买入波动偏移系数<input v-model="buyOffset" inputmode="decimal" /></label>
      <label>卖出波动偏移系数<input v-model="sellOffset" inputmode="decimal" /></label>
      <label>典型振幅权重<input v-model="rangeWeight" inputmode="decimal" /></label>
      <label>空仓再次评估等待（秒）<input v-model.number="buyCheckSeconds" type="number" min="5" max="300" step="5" /></label>
    </div><p class="muted">最终波动尺度取“收盘价总体标准差”和“分钟高低振幅中位数 × 权重”中的较大值；振幅权重默认 0.5，可降低单根异常长影线的影响。默认 15 根 K 线、买卖偏移各 0.5。参数效果尚未验证。</p></details>
    <p class="muted">空仓等待可设为 5–300 秒（5 秒的倍数），用于一轮结束后或策略明确暂不买入后的下一次评估。首次启动仍立即评估；它不改变订单巡检、损耗检查或退出规则。</p>
    <button :disabled="!!blockedReason" :title="blockedReason" @click="start">{{busy?'正在处理…':'启动自动实盘买卖'}}</button>
  </fieldset>
  <p v-if="blockedReason" class="notice" role="status">暂不能启动：{{blockedReason}}</p>
  <p v-else class="muted">交易页面已识别，可以启动。任务结束后可直接再次启动；每个新任务独立累计目标和预算，继续原任务请用“核对后恢复任务”。</p>
  <p v-if="notice" class="notice" role="status">{{notice}}</p>
  <p v-if="error" class="notice error" role="alert">{{error}}</p>
  <div v-if="current">
    <p class="notice" role="status">{{current.request.expected_symbol}} / {{current.request.expected_quote}}：{{current.message}}</p>
    <div class="notice">
      <strong>下一动作：{{fmtNextAction(current)}}</strong>
      <p>{{current.schedule?.reason??'正在读取调度状态。'}}</p>
    </div>
    <details><summary>固定调度规则</summary><ul>
      <li>后台每 5 秒检查一次是否有到期动作。</li>
      <li>普通挂单最多 5 分钟；跨过每个自然分钟后巡检订单并检查资产损耗。</li>
      <li>持仓满 30 分钟进入主动退出；主动退出卖单每 15 秒撤单核对后重估。</li>
      <li>任务暂停时只读核对已有订单，不提交、不撤单。</li>
    </ul></details>
    <p>本任务空仓再次评估等待：{{current.request.config.buy_check_seconds??20}} 秒。<span v-if="current.market_at">最近行情：{{new Date(current.market_at).toLocaleTimeString()}}。</span></p>
    <div v-if="current.estimate&&!current.pending&&current.inventory==='0'">
      <p>上次买入评估：建议买价 {{current.estimate.buy}}</p>
      <ul><li v-for="reason in (current.estimate.buy_blockers??[])" :key="reason">{{reason}}</li></ul>
    </div>
    <p v-if="!running" class="muted">当前不会自动提交或撤单。处理提示后点击“核对后恢复任务”；后端重启也需要手动恢复。</p>
    <div v-if="!running" class="actions">
      <button class="secondary" :disabled="busy||browserBusy||!connected" @click="emit('openTaskPage',current.request.url)">重新打开本任务交易页面</button>
    </div>
    <p v-if="!running" class="muted">此按钮只恢复本任务原币种页面，不填表、不提交、不撤单，也不会自动恢复任务。</p>
    <p>已完成 {{current.rounds}} 轮 · 累计买入 {{current.buy_total}} U</p>
    <p>积分进度：启动已有 {{current.request.config.current_points??'0'}} 分 + 本任务预计新增 {{fmtPoints(earnedPoints(current))}} 分 = {{fmtPoints(totalPoints(current))}} / {{current.request.config.target_points}} 分。</p>
    <p>代币余额 {{current.inventory}} · 本轮实际支出 {{current.cost}} U · 本轮卖出收入 {{current.proceeds}} U</p>
    <p>已结束轮次现金盈亏 {{current.realized_pnl}} U · 累计亏损轮次损耗 {{current.session_loss}} / 10 U（余额差不重复扣手续费）</p>
    <p>本轮起始 USDT：{{current.round_start_quote??'尚未开始'}} · 计划买入 {{current.round_plan??'—'}} U · 已撤单重挂 {{current.buy_rehangs??0}} / 10 次。</p>
    <p v-if="current.balances">最近已核对可用 USDT {{current.balances.quote_available}} · 上轮保留零头 {{current.dust??'0'}}。</p>
    <p v-if="current.risk">含冻结资产的 K 线估值：{{current.risk.equity??'待核对'}} U；预计损耗 {{current.risk.loss??'待核对'}} U / {{current.risk.loss_pct??'待核对'}}%。{{current.risk.reason}}</p>
    <p v-if="current.first_buy_at">持仓计时起点：{{new Date(current.first_buy_at*1000).toLocaleString()}}（观察到余额增加后，使用对应买单提交时间；重挂不重置）。</p>
    <p v-if="current.pending">当前计划：<template v-if="current.pending.side==='buy'">按 {{current.pending.price}} 买入 {{current.pending.quote_amount}} {{current.request.expected_quote}}</template><template v-else>按 {{current.pending.price}} 将平台卖出数量滑杆拉满（包含已有零头）</template></p>
    <div v-if="current.pending_order?.state==='submission_unknown'" class="notice error">
      <p>这笔订单的提交结果尚未确认，程序正在等待你核对。首次错误：{{current.pending_order.submission_error||current.pending_order.message}}</p>
      <p>请关闭仍然显示的订单确认弹窗，并确认币安“当前委托”中没有这笔订单。程序还会核对当前无挂单且余额与提交前一致。</p>
      <label><input v-model="confirmedNotSubmitted" type="checkbox" :disabled="busy" />我已关闭确认弹窗，并确认平台没有这笔订单。</label>
      <button class="secondary" :disabled="busy||!confirmedNotSubmitted" @click="resolveUnsubmitted">核对未提交并结束旧任务</button>
    </div>
    <div v-if="current.accounting_version!==2" class="notice">
      <p>此为旧版任务，缺少新核算所需起始余额。请先处理平台挂单，再结束旧版记录；旧统计保留，不转换为新盈亏。</p>
      <button class="secondary" :disabled="busy" @click="control('retire_legacy')">核对无挂单并结束旧版记录</button>
    </div>
    <div class="actions">
      <button class="secondary" :disabled="busy||!running" @click="control('pause')">暂停自动操作</button>
      <button :disabled="busy||running" @click="control('resume')">核对后恢复任务</button>
      <button class="secondary" :disabled="busy" @click="control('finish')">停止买入，卖完结束</button>
      <button class="secondary" :disabled="busy" @click="forceRestart">强制重新开始任务</button>
    </div>
    <p class="muted">强制重新开始只结束本地任务并解锁上方参数，不会自动撤销平台挂单。新任务启动前仍会检查当前委托，有旧挂单时不会提交新单。</p>
    <p class="muted">暂停不撤挂单，只读巡检继续。后端重启后保持暂停，需点击恢复。自动撤单使用“全部取消”并确认一次普通撤单弹窗；确认后延迟，首次核对会刷新交易页，再等待委托消失和余额稳定。卖出只填写价格并将平台数量滑杆拉到最右端，平台生成实际卖出量，包含原有零头；无挂单且剩余价值不超过 2 U 即结束本轮。现金盈亏不计剩余代币估值。页面验证、撤单结果未知或余额未更新时暂停或继续等待，不重复点击撤单。</p>
  </div>
  <details v-if="recent.length"><summary>最近任务</summary><ul><li v-for="task in recent" :key="task.id">{{task.id.slice(0,8)}} · {{task.request.expected_symbol}} · {{task.message}} · 买入 {{task.buy_total}} U · 盈亏 {{task.realized_pnl}} U</li></ul></details>
  <DecisionLog :tasks="recent" :current-id="current?.id" />
</section>
</template>

<style scoped>fieldset{border:0;padding:0;margin:0}details{margin:16px 0}summary{cursor:pointer;margin-bottom:12px}</style>
