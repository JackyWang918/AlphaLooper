<script setup lang="ts">
import {computed,onMounted,onUnmounted,ref,watch} from 'vue'
const props=defineProps<{url:string;connected:boolean;symbol?:string;quote?:string;fillSupported:boolean;browserBusy:boolean;browserReason?:string}>()
const emit=defineEmits<{refreshBrowser:[]}>()
type Intent={id:string;created_at:number;state:string;message:string;submission_error?:string;last_check_error?:string;request:{side:string;price:string;quantity:string;expected_symbol:string};result?:{status:string;gross:string;quantity:string};active:boolean}
const enabled=ref(false),busy=ref(false),error=ref('')
const readiness=ref('')
const confirmationFeedback=ref('')
const confirmedNotSubmitted=ref(false)
const current=ref<Intent|null>(null),recent=ref<Intent[]>([])
watch(()=>current.value?.id,()=>{confirmedNotSubmitted.value=false})
const side=ref('buy'),price=ref(''),quantity=ref('')
const book=ref(localStorage.getItem('account-book')||'本机账户')
function decimalParts(value:string) {
  if(value.length>40||!/^\d+(?:\.\d+)?$/.test(value)) return null
  const [whole,fraction='']=value.split('.')
  const units=BigInt(whole+fraction)
  return units>0n ? {units,scale:10n**BigInt(fraction.length)} : null
}
const inputProblem=computed(()=>{
  const p=decimalParts(price.value),q=decimalParts(quantity.value)
  if(!p||!q) return '请输入大于零的价格和代币数量（普通十进制数）。'
  if(side.value==='buy'&&p.units*q.units*10001n>50n*10000n*p.scale*q.scale)
    return '本笔买入含 0.01% 估算手续费超过 50 U 上限，请调整价格或数量。数量填写的是代币数量。'
  return ''
})
const blockedReason=computed(()=>{
  if(busy.value||props.browserBusy) return '正在处理操作，请稍候。'
  if(current.value) return `上一笔系统委托尚未确认结束：${current.value.message}`
  if(previousRequest.value) return '此请求已有处理结果，重复点击不会重新执行。请点击「准备下一笔（清空输入）」，重新填写后再提交。'
  if(!props.connected) return '尚未连接受控 Chrome，请先在「01 / 浏览器连接」启动 Chrome。'
  if(!props.fillSupported||!props.symbol||!props.quote) return '尚未识别交易表单，无法提交。请在程序启动的 Chrome 标签页打开交易页面，完成登录和提示处理，再点击「重新识别表单」。'
  if(!enabled.value) return '实盘单笔下单尚未开启。'
  if(!book.value.trim()) return '请填写账户账本名称。'
  return inputProblem.value
})
const requestId=ref(localStorage.getItem('live-request-id')||crypto.randomUUID())
const previousRequest=computed(()=>recent.value.find(item=>item.id===requestId.value&&!item.active))
let timer:ReturnType<typeof setInterval>|undefined
const states:Record<string,string>={preparing:'提交前核对',not_submitted:'未提交',submission_unknown:'提交结果待核实',waiting:'等待平台结果',completed:'已结束并记账'}
async function request(path:string,body?:unknown) {
  const response=await fetch('/api/live/'+path,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json','X-AlphaLooper-Client':'local-ui'},...(body===undefined?{}:{body:JSON.stringify(body)}),signal:AbortSignal.timeout(55000)})
  const result=await response.json()
  if(!response.ok) throw new Error(typeof result.detail==='string'?result.detail:'参数或请求无效')
  return result
}
async function refresh() {
  try {const data=await request('orders');enabled.value=data.enabled;current.value=data.current;recent.value=data.recent;if(data.current) readiness.value=''}
  catch(e){error.value=String(e)}
}
async function toggle() {
  busy.value=true;error.value=''
  try{await request('enabled',{enabled:!enabled.value});await refresh()}catch(e){error.value=String(e)}finally{busy.value=false}
}
async function submit() {
  if(blockedReason.value) return
  busy.value=true;error.value='';readiness.value=''
  localStorage.setItem('live-request-id',requestId.value)
  try {
    await request('orders',{request_id:requestId.value,book:book.value,url:props.url,side:side.value,price:price.value,quantity:quantity.value,expected_symbol:props.symbol,expected_quote:props.quote})
    await refresh()
  }catch(e){error.value=String(e);await refresh()}finally{busy.value=false}
}
function next() {requestId.value=crypto.randomUUID();localStorage.setItem('live-request-id',requestId.value);price.value='';quantity.value='';error.value='';readiness.value=''}
async function check(){busy.value=true;error.value='';try{await request('check',{});await refresh()}catch(e){error.value=String(e)}finally{busy.value=false}}
async function previewConfirmation(){busy.value=true;error.value='';try{const data=await request('confirmation-preview',{});confirmationFeedback.value='弹窗只读核对：'+data.reason}catch(e){error.value=String(e)}finally{busy.value=false}}
async function resolveUnsubmitted() {
  if(!current.value||!confirmedNotSubmitted.value) return
  busy.value=true;error.value=''
  try {
    await request('resolve-unsubmitted',{request_id:current.value.id,confirmed_not_submitted:true})
    confirmedNotSubmitted.value=false
    await refresh()
  } catch(e) {error.value=String(e)} finally {busy.value=false}
}
async function checkReadiness() {
  busy.value=true;error.value='';readiness.value='正在检查当前委托和最新历史订单，不填写或提交…'
  try {
    const response=await fetch('/api/browser/order-readiness',{method:'POST',headers:{'Content-Type':'application/json','X-AlphaLooper-Client':'local-ui'},body:JSON.stringify({url:props.url,side:side.value,price:price.value,quantity:quantity.value,expected_symbol:props.symbol,expected_quote:props.quote}),signal:AbortSignal.timeout(55000)})
    const data=await response.json()
    if(!response.ok) throw new Error(typeof data.detail==='string'?data.detail:'请填写有效价格和数量，并确认表单已识别。')
    readiness.value='订单读取检查通过：当前无挂单，最新历史记录已核对。未填写、未提交订单。'
  } catch(e) {readiness.value='';error.value=String(e)} finally {busy.value=false}
}
onMounted(()=>{refresh();timer=setInterval(()=>{if(!busy.value) refresh()},5000)})
onUnmounted(()=>clearInterval(timer))
</script>
<template>
<section>
  <div class="section-title"><h2>04 / 实盘单笔下单</h2><span class="badge">{{enabled?'已开启':'默认关闭'}}</span></div>
  <p>此入口会真实提交一笔限价单。由你开启并点击提交后执行；程序填写后会核对平台订单确认单，并自动点击一次“继续”。买入含预计手续费上限 50 U。不会自动开启下一笔，也尚未接入自动撤单和策略循环。</p>
  <button class="secondary" :disabled="busy" @click="toggle">{{enabled?'关闭新单提交':'开启实盘单笔下单'}}</button>
  <p class="muted">关闭仅阻止新单，不撤销平台挂单。已有委托由后端每 60 秒巡检，刷新控制台不会重复下单；后端重启后新单开关关闭，继续核对未结束记录。</p>
  <p role="status">受控浏览器：<span v-if="connected">已连接</span><span v-else>未连接</span> · 交易表单：<span v-if="fillSupported && symbol && quote">{{symbol}} / {{quote}}</span><span v-else>未识别</span></p>
  <p v-if="!fillSupported&&browserReason" class="muted">识别反馈：{{browserReason}}</p>
  <button class="secondary" :disabled="busy||browserBusy||!!current" @click="emit('refreshBrowser')">{{browserBusy?'正在识别…':'重新识别表单'}}</button>
  <p class="muted">提交前会先检查当前无挂单，再读取最新历史订单作核对依据。可先使用下方只读检查定位页面问题。</p>
  <div class="fields">
    <label>账本<input v-model="book" :disabled="busy||!!current" /></label>
    <label>方向<select v-model="side" :disabled="busy||!!current"><option value="buy">买入</option><option value="sell">卖出</option></select></label>
    <label>价格（{{quote||'计价币'}}）<input v-model="price" inputmode="decimal" :disabled="busy||!!current" /></label>
    <label>数量（{{symbol||'代币'}}）<input v-model="quantity" inputmode="decimal" :disabled="busy||!!current" /></label>
  </div>
  <p v-if="blockedReason" class="notice" role="status">暂不能提交：{{blockedReason}}</p>
  <p v-if="price&&quantity&&inputProblem&&blockedReason!==inputProblem" class="notice error">{{inputProblem}}</p>
  <button :disabled="!!blockedReason" @click="submit">提交并确认这一笔真实限价单</button>
  <button class="secondary" :disabled="busy||browserBusy||!!current||!fillSupported||!symbol||!quote||!price||!quantity" @click="checkReadiness">仅检查订单读取（不下单）</button>
  <button class="secondary" :disabled="busy||!!current" @click="next">准备下一笔（清空输入）</button>
  <button class="secondary" :disabled="busy||!current" @click="check">立即检查当前订单</button>
  <button class="secondary" :disabled="busy||!current" @click="previewConfirmation">核对当前弹窗（不点击继续）</button>
  <p v-if="error" class="notice error" role="alert">{{error}}</p>
  <p v-if="readiness" class="notice" role="status">{{readiness}}</p>
  <p v-if="confirmationFeedback" class="notice" role="status">{{confirmationFeedback}}</p>
  <p v-if="current" class="notice">{{states[current.state]||current.state}}：{{current.message}}</p>
  <p v-if="current?.submission_error" class="notice error">首次提交错误：{{current.submission_error}}</p>
  <p v-if="current?.last_check_error" class="muted">最近巡检反馈：{{current.last_check_error}}</p>
  <p v-if="!current" class="muted">当前没有待确认的系统委托。</p>
  <div v-if="current" class="notice">
    <p>如果没有完成平台二次确认：先手动关闭确认弹窗，核对当前无挂单、历史没有本次新订单，再解除本地等待。</p>
    <label><input v-model="confirmedNotSubmitted" type="checkbox" :disabled="busy" />我确认未完成二次确认，已关闭弹窗，且平台没有本次订单。</label>
    <button class="secondary" :disabled="busy||!confirmedNotSubmitted" @click="resolveUnsubmitted">核对未提交并解除等待</button>
    <p class="muted">程序会再次核对无挂单和历史未变化。此操作只结束本地记录，不撤单、不补点确认、不重新下单。</p>
  </div>
  <h3 v-if="recent.length">最近请求记录（保留历史结果）</h3>
  <ul><li v-for="item in recent" :key="item.id">{{new Date(item.created_at*1000).toLocaleString()}} · {{item.request.side==='buy'?'买入':'卖出'}} {{item.request.expected_symbol}} · {{item.request.quantity}} @ {{item.request.price}} · {{states[item.state]||item.state}}：{{item.message}}<span v-if="item.result"> 实际成交 {{item.result.quantity}}，成交额 {{item.result.gross}}，平台状态 {{item.result.status}}</span></li></ul>
</section>
</template>
<style scoped>button{margin:8px 12px 8px 0}li{margin:12px 0;overflow-wrap:anywhere}</style>
