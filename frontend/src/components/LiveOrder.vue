<script setup lang="ts">
import {onMounted,onUnmounted,ref} from 'vue'
const props=defineProps<{url:string;connected:boolean;symbol?:string;quote?:string}>()
type Intent={id:string;state:string;message:string;request:{side:string;price:string;quantity:string;expected_symbol:string};result?:{status:string;gross:string;quantity:string};active:boolean}
const enabled=ref(false),busy=ref(false),error=ref('')
const current=ref<Intent|null>(null),recent=ref<Intent[]>([])
const side=ref('buy'),price=ref(''),quantity=ref('')
const book=ref(localStorage.getItem('account-book')||'本机账户')
let requestId=localStorage.getItem('live-request-id')||crypto.randomUUID()
let timer:ReturnType<typeof setInterval>|undefined
const states:Record<string,string>={preparing:'提交前核对',not_submitted:'未提交',submission_unknown:'提交结果待核实',waiting:'等待平台结果',completed:'已结束并记账'}
async function request(path:string,body?:unknown) {
  const response=await fetch('/api/live/'+path,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json','X-AlphaLooper-Client':'local-ui'},...(body===undefined?{}:{body:JSON.stringify(body)}),signal:AbortSignal.timeout(55000)})
  const result=await response.json()
  if(!response.ok) throw new Error(typeof result.detail==='string'?result.detail:'参数或请求无效')
  return result
}
async function refresh() {
  try {const data=await request('orders');enabled.value=data.enabled;current.value=data.current;recent.value=data.recent}
  catch(e){error.value=String(e)}
}
async function toggle() {
  busy.value=true;error.value=''
  try{await request('enabled',{enabled:!enabled.value});await refresh()}catch(e){error.value=String(e)}finally{busy.value=false}
}
async function submit() {
  busy.value=true;error.value=''
  localStorage.setItem('live-request-id',requestId)
  try {
    await request('orders',{request_id:requestId,book:book.value,url:props.url,side:side.value,price:price.value,quantity:quantity.value,expected_symbol:props.symbol,expected_quote:props.quote})
    await refresh()
  }catch(e){error.value=String(e);await refresh()}finally{busy.value=false}
}
function next() {requestId=crypto.randomUUID();localStorage.setItem('live-request-id',requestId);price.value='';quantity.value='';error.value=''}
async function check(){busy.value=true;error.value='';try{await request('check',{});await refresh()}catch(e){error.value=String(e)}finally{busy.value=false}}
onMounted(()=>{refresh();timer=setInterval(()=>{if(!busy.value) refresh()},5000)})
onUnmounted(()=>clearInterval(timer))
</script>
<template>
<section>
  <div class="section-title"><h2>04 / 实盘单笔下单</h2><span class="badge">{{enabled?'已开启':'默认关闭'}}</span></div>
  <p>此入口会真实提交一笔限价单。由你开启并点击提交后执行；买入含估算手续费上限 50 U。不会自动开启下一笔，也尚未接入自动撤单和策略循环。</p>
  <button class="secondary" :disabled="busy" @click="toggle">{{enabled?'关闭新单提交':'开启实盘单笔下单'}}</button>
  <p class="muted">关闭仅阻止新单，不撤销平台挂单。已有委托由后端每 60 秒巡检，刷新控制台不会重复下单；后端重启后新单开关关闭，继续核对未结束记录。</p>
  <div class="fields">
    <label>账本<input v-model="book" :disabled="busy||!!current" /></label>
    <label>方向<select v-model="side" :disabled="busy||!!current"><option value="buy">买入</option><option value="sell">卖出</option></select></label>
    <label>价格（{{quote||'计价币'}}）<input v-model="price" inputmode="decimal" :disabled="busy||!!current" /></label>
    <label>数量（{{symbol||'代币'}}）<input v-model="quantity" inputmode="decimal" :disabled="busy||!!current" /></label>
  </div>
  <button :disabled="busy||!enabled||!connected||!symbol||!quote||!!current||!price||!quantity" @click="submit">提交这一笔真实限价单</button>
  <button class="secondary" :disabled="busy||!!current" @click="next">准备下一笔（清空输入）</button>
  <button class="secondary" :disabled="busy||!current" @click="check">立即检查当前订单</button>
  <p v-if="error" class="notice error" role="alert">{{error}}</p>
  <p v-if="current" class="notice">{{states[current.state]||current.state}}：{{current.message}}</p>
  <p v-else class="muted">当前没有待确认的系统委托。</p>
  <ul><li v-for="item in recent" :key="item.id">{{item.request.side==='buy'?'买入':'卖出'}} {{item.request.expected_symbol}} · {{item.request.quantity}} @ {{item.request.price}} · {{states[item.state]||item.state}}：{{item.message}}<span v-if="item.result"> 实际成交 {{item.result.quantity}}，成交额 {{item.result.gross}}，平台状态 {{item.result.status}}</span></li></ul>
</section>
</template>
<style scoped>button{margin:8px 12px 8px 0}li{margin:12px 0;overflow-wrap:anywhere}</style>
