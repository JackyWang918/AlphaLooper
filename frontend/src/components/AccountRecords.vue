<script setup lang="ts">
import { onMounted, ref } from 'vue'
type Order = {order_id:string;created_at:string;symbol:string;quote:string;side:string;quantity:string;gross:string;average_price:string;status:string;estimated_fee:string}
type Stat = {chain:string;address:string;symbol:string;quote:string;buy_total:string;sell_total:string;estimated_fee:string;estimated_points:string;estimated_realized_pnl:string|null;recorded_quantity:string|null;cost_status:string}
type Ledger = {book:string;orders:Order[];stats:Stat[]}
const props=defineProps<{url:string;connected:boolean;browserBusy:boolean}>()
const book=ref(localStorage.getItem('account-book') || '本机账户')
const ledger=ref<Ledger|null>(null)
const busy=ref(false), error=ref(''), feedback=ref('')
async function load() {
  busy.value=true;error.value='';feedback.value=''
  try {
    const response=await fetch(`/api/account/orders?book=${encodeURIComponent(book.value.trim())}`)
    if(!response.ok) throw new Error('载入订单账本失败，请确认后端已更新并完成迁移。')
    ledger.value=await response.json()
    localStorage.setItem('account-book',book.value.trim())
  } catch(e) {error.value=String(e)} finally {busy.value=false}
}
onMounted(load)
async function read() {
  busy.value=true;error.value='';feedback.value=''
  try {
    const response=await fetch('/api/account/orders/read',{method:'POST',
      headers:{'Content-Type':'application/json','X-AlphaLooper-Client':'local-ui'},
      body:JSON.stringify({url:props.url,book:book.value.trim()}),signal:AbortSignal.timeout(55000)})
    const body=await response.json()
    if(!response.ok) throw new Error(typeof body.detail==='string'?body.detail:'读取失败')
    ledger.value=body
    localStorage.setItem('account-book',book.value.trim())
    feedback.value=body.updated ? '已更新第一笔历史委托，重复读取不会重复记账。' : body.rejected ? '订单结果与已保存记录冲突，未覆盖。' : '未取得第一笔历史委托及订单 ID，请检查页面。'
  } catch(e) {error.value=String(e)} finally {busy.value=false}
}
</script>
<template>
  <section id="account-records">
    <div class="section-title"><h2>真实订单账本与统计</h2><span class="badge">只读 · 费用估算</span></div>
    <p class="muted">单笔流程：挂单 → 每分钟检查当前委托 → 委托消失后核对第一笔历史委托 → 更新账本 → 下一笔。当前仅提供手动核对入口，自动巡检和系统下单尚未接入。</p>
    <p class="muted">手动核对时，请将历史委托按最新在前显示，并展开第一笔以显示订单 ID。只读取这一笔，不导入其他历史订单。</p>
    <label>账户账本名称<input v-model="book" :disabled="busy" maxlength="80" /></label>
    <p class="muted">切换平台账户时请使用不同账本名称。程序不会自动识别账户身份。统计只包含已读取订单，按币种与计价币分别累计，不代表全部账户资产或今日积分。</p>
    <button :disabled="busy || !book.trim()" @click="load">载入账本</button>
    <button :disabled="busy || browserBusy || !connected || !book.trim() || !url.trim()" @click="read">{{busy?'处理中…':'核对第一笔历史委托'}}</button>
    <p v-if="!connected" class="muted">请先连接独立 Chrome，并手动登录打开历史委托。</p>
    <p v-if="error" role="alert" class="notice error">{{error}}</p>
    <p v-if="feedback" role="status">{{feedback}}</p>
    <template v-if="ledger">
      <h3>账本：{{ledger.book}}</h3>
      <p class="notice">买卖手续费各按实际成交额 × 0.01% 估算；预计积分仅按买入成交额 × 4。预计已实现盈亏按订单创建顺序分摊平均买入成本并扣估算费用，不包含剩余持仓浮盈亏。历史成本缺失或成交顺序未确认时显示未知。</p>
      <p v-if="!ledger.orders.length">暂无订单记录。读取时缺少 ID 的订单不会入账。</p>
      <div v-for="stat in ledger.stats" :key="stat.chain+stat.address+stat.symbol+stat.quote" class="summary">
        <h3>{{stat.symbol}} / {{stat.quote}}</h3>
        <dl><dt>累计买入额</dt><dd>{{stat.buy_total}} {{stat.quote}}</dd>
          <dt>累计卖出额</dt><dd>{{stat.sell_total}} {{stat.quote}}</dd>
          <dt>预计积分（已读取范围）</dt><dd>{{stat.estimated_points}}</dd>
          <dt>估算手续费</dt><dd>{{stat.estimated_fee}} {{stat.quote}}</dd>
          <dt>预计已实现盈亏</dt><dd>{{stat.estimated_realized_pnl ?? '未知'}} {{stat.quote}}</dd>
          <dt>已记录订单剩余数量</dt><dd>{{stat.recorded_quantity ?? '未知'}} {{stat.symbol}}</dd></dl>
        <p class="muted">{{stat.cost_status}}</p>
      </div>
      <div class="table-wrap"><table v-if="ledger.orders.length"><thead><tr>
        <th>订单 ID</th><th>创建时间</th><th>币种</th><th>方向</th><th>状态</th><th>已成交数量</th><th>成交均价</th><th>成交额</th><th>估算手续费</th>
      </tr></thead><tbody><tr v-for="order in ledger.orders" :key="order.order_id">
        <td>{{order.order_id}}</td><td>{{order.created_at}}</td><td>{{order.symbol}}</td><td>{{order.side}}</td><td>{{order.status}}</td><td>{{order.quantity}}</td><td>{{order.average_price}}</td><td>{{order.gross}} {{order.quote}}</td><td>{{order.estimated_fee}} {{order.quote}}</td>
      </tr></tbody></table></div>
    </template>
  </section>
</template>
<style scoped>
.table-wrap{overflow-x:auto;margin-top:20px}table{border-collapse:collapse;min-width:100%;font-size:13px}th,td{text-align:left;padding:10px;border-bottom:1px solid #314057;white-space:nowrap}button{margin:8px 12px 8px 0}h3{font-size:16px}dl{display:grid;grid-template-columns:minmax(150px,1fr) 2fr;gap:10px}dd{margin:0;overflow-wrap:anywhere}p{overflow-wrap:anywhere}
</style>
