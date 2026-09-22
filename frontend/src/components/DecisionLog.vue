<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
const props=defineProps<{tasks:{id:string;request:{expected_symbol:string}}[];currentId?:string}>()
type Entry={id:number;time:number;kind:string;reason:string;state:Record<string,unknown>;evidence:Record<string,any>;details:Record<string,any>|null}
const selected=ref(''),kind=ref(''),items=ref<Entry[]>([]),next=ref<number|null>(null),error=ref(''),busy=ref(false),auto=ref(true),config=ref<Record<string,unknown>>({})
const labels:Record<string,string>={control:'任务操作',buy_wait:'暂不买入',buy:'决定买入',sell:'决定卖出',risk:'持仓评估',cancel:'决定撤单',execution:'执行反馈',order_check:'订单巡检',fill:'成交回填',completed:'任务结束',error:'异常暂停'}
let timer:ReturnType<typeof setInterval>|undefined
let version=0
async function load(older=false){
  if(!selected.value)return
  const requestVersion=++version
  busy.value=true;error.value=''
  try{
    const params=new URLSearchParams({limit:'50'})
    if(kind.value)params.set('kind',kind.value)
    if(older&&next.value)params.set('before',String(next.value))
    const res=await fetch(`/api/automatic/${selected.value}/decisions?${params}`,{signal:AbortSignal.timeout(20000)})
    const body=await res.json()
    if(!res.ok)throw new Error(typeof body.detail==='string'?body.detail:'无法读取日志，请确认后端已升级至迁移 0007。')
    if(requestVersion!==version)return
    items.value=older?[...items.value,...body.items]:body.items;next.value=body.next_before;config.value=body.config
  }catch(e){if(requestVersion===version)error.value=e instanceof Error?e.message:'读取失败'}
  finally{if(requestVersion===version)busy.value=false}
}
function older(){auto.value=false;load(true)}
watch(()=>props.tasks,()=>{if(!selected.value)selected.value=props.currentId||props.tasks[0]?.id||''},{immediate:true})
watch([selected,kind],()=>{items.value=[];next.value=null;load()})
onMounted(()=>{if(selected.value)load();timer=setInterval(()=>{if(auto.value&&!busy.value)load()},5000)})
onUnmounted(()=>{clearInterval(timer);version++})
</script>

<template>
<div class="decision-log">
  <h3>策略判断日志</h3>
  <p class="muted">记录每次实际估价、持仓评估、订单巡检和执行反馈。等待计时期间不会重复记一条“未评估”。数据保存在本机，刷新和重启后仍可查看；升级前的判断无法补回。决定买卖不等于已成交，请结合执行反馈、成交回填查看。</p>
  <div class="fields">
    <label>任务<select v-model="selected"><option v-for="task in tasks" :key="task.id" :value="task.id">{{task.request.expected_symbol}} · {{task.id.slice(0,8)}}</option></select></label>
    <label>判断类型<select v-model="kind"><option value="">全部</option><option v-for="(label,key) in labels" :key="key" :value="key">{{label}}</option></select></label>
  </div>
  <div class="actions">
    <button class="secondary" :disabled="busy||!selected" @click="load()">刷新最新记录</button>
    <a v-if="selected" :href="`/api/automatic/${selected}/decisions/export${kind?'?kind='+kind:''}`" download>导出{{kind?'该类型':'全部'}}日志（JSONL）</a>
    <label><input v-model="auto" type="checkbox" /> 每 5 秒更新显示</label>
  </div>
  <p v-if="error" class="notice error">{{error}}</p>
  <p v-else-if="!items.length">{{busy?'正在读取日志…':'暂无判断日志。任务产生新判断后会在这里显示。'}}</p>
  <details v-if="Object.keys(config).length"><summary>查看该任务使用的策略参数</summary><pre>{{JSON.stringify(config,null,2)}}</pre></details>
  <article v-for="entry in items" :key="entry.id">
    <p><time>{{new Date(entry.time).toLocaleString()}}</time> · <strong>{{labels[entry.kind]||entry.kind}}</strong> · #{{entry.id}}</p>
    <p>{{entry.reason}}</p>
    <p v-if="entry.evidence.market_time" class="muted">行情时间 {{new Date(entry.evidence.market_time).toLocaleTimeString()}} · 买一 {{entry.evidence.bids?.[0]?.[0]??'—'}} · 卖一 {{entry.evidence.asks?.[0]?.[0]??'—'}}</p>
    <p v-if="entry.evidence.estimate">价格中心 {{entry.evidence.estimate.center}} · 波动 {{entry.evidence.estimate.volatility}} · 建议买价 {{entry.evidence.estimate.buy}} · 模型卖价 {{entry.evidence.estimate.sell}}</p>
    <p v-if="entry.evidence.risk">评估持仓 {{entry.evidence.position_including_partial?.inventory}} · 预计损耗 {{entry.evidence.risk.loss??'未知'}} U（{{entry.evidence.risk.loss_pct??'未知'}}%） · 盘口深度{{entry.evidence.risk.covered?'足够':'不足'}}</p>
    <p v-if="entry.details?.price">计划委托：{{entry.details.quantity}} @ {{entry.details.price}}</p>
    <details><summary>查看完整判断依据（参数阈值、K 线、盘口、状态及订单关联）</summary><pre>{{JSON.stringify(entry,null,2)}}</pre></details>
  </article>
  <button v-if="next" class="secondary" :disabled="busy" @click="older">加载更早记录（暂停自动刷新）</button>
</div>
</template>

<style scoped>
.decision-log{margin-top:28px;border-top:1px solid #314057;padding-top:12px}article{padding:12px 0;border-bottom:1px solid #314057}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:420px;overflow:auto;background:#101723;padding:12px;font-size:12px}.actions{align-items:center;flex-wrap:wrap}.actions label{display:flex;gap:8px;align-items:center}.actions input{width:auto;margin:0}details{margin:12px 0}summary{cursor:pointer}time{color:#a7b5c8}a{color:#72ddc3}
</style>
