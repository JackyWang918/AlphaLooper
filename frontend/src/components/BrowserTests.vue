<script setup lang="ts">
import { computed, ref } from 'vue'

const props = defineProps<{url:string; connected:boolean; symbol?:string; quote?:string; fillSupported?:boolean; browserBusy?:boolean}>()
const emit = defineEmits<{refreshBrowser:[]}>()
type Action = 'fill_total'|'buy_balance'|'sell_balance'|'orders'|'cancel_all'|'sell_slider'
type Order = {symbol:string; side:string; price:string; quote:string}
type FormResult = {values:{price:string; quantity:string; quote_amount:string}; validation:Record<string,{valid:boolean;message:string;ariaInvalid:string|null}>;page_errors:string[];slider_value?:string|null;slider_max?:string|null}
const side = ref('buy'), price = ref(''), total = ref(''), sellPrice = ref('')
const busy = ref<Action|null>(null), error = ref('')
const buyBalance = ref<string|null>(null), sellBalance = ref<string|null>(null)
const orders = ref<Order[]|null>(null), cancelMessage = ref('')
const fillResult = ref<FormResult|null>(null), sliderResult = ref<FormResult|null>(null)
const blocked = computed(() => busy.value || props.browserBusy || !props.connected || !props.fillSupported || !props.symbol || !props.quote)
const invalidFields = (result:FormResult) => Object.entries(result.validation).filter(([,v])=>!v.valid||v.ariaInvalid==='true').map(([key,v])=>`${({price:'价格',quantity:'数量',quote_amount:'成交额'} as Record<string,string>)[key]}：${v.message||'平台标记为无效'}`)

async function run(action:Action) {
  if (blocked.value) return
  busy.value=action;error.value=''
  if(action==='cancel_all') cancelMessage.value=''
  if(action==='fill_total') fillResult.value=null
  if(action==='sell_slider') sliderResult.value=null
  if(action==='buy_balance') buyBalance.value=null
  if(action==='sell_balance') sellBalance.value=null
  if(action==='orders'||action==='cancel_all') orders.value=null
  try {
    const response=await fetch('/api/browser/page-test', {
      method:'POST', headers:{'Content-Type':'application/json','X-AlphaLooper-Client':'local-ui'},
      body:JSON.stringify({action,request_id:crypto.randomUUID(),url:props.url,expected_symbol:props.symbol,expected_quote:props.quote,
        side:action==='sell_slider'?'sell':side.value,
        price:action==='fill_total'?price.value:action==='sell_slider'?sellPrice.value:'1',quantity:'1',
        quote_amount:action==='fill_total'?total.value:undefined}),
      signal:AbortSignal.timeout(55000),
    })
    const result=await response.json()
    if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'请检查价格、成交额及交易页面。')
    if(action==='fill_total')fillResult.value=result
    if(action==='sell_slider')sliderResult.value=result
    if(action==='buy_balance')buyBalance.value=`${result.available} ${result.currency}`
    if(action==='sell_balance')sellBalance.value=`${result.available} ${result.currency}`
    if(action==='orders')orders.value=result.orders
    if(action==='cancel_all') {
      cancelMessage.value=result.message
      orders.value=result.orders??null
    }
  }catch(e){error.value=e instanceof Error?e.message:'页面测试失败'}
  finally{busy.value=null}
}
</script>

<template>
  <section>
    <div class="section-title"><h2>03 / 仅填表</h2><span class="badge">页面测试</span></div>
    <p class="muted">在受控 Chrome 打开交易页面。自动任务运行时请先暂停，再点击测试按钮。</p>
    <button class="secondary" :disabled="!!busy||browserBusy" @click="emit('refreshBrowser')">重新识别交易页面</button>
    <p v-if="!connected||!fillSupported" class="notice">请先连接浏览器并识别交易表单。</p>
    <div class="fields">
      <label>填表方向<select v-model="side" :disabled="!!busy"><option value="buy">买入</option><option value="sell">卖出</option></select></label>
      <label>测试价格<input v-model="price" inputmode="decimal" :disabled="!!busy" /></label>
      <label>测试成交额（{{quote||'USDT'}}）<input v-model="total" inputmode="decimal" :disabled="!!busy" /></label>
    </div>
    <button :disabled="!!blocked||!price.trim()||!total.trim()" @click="run('fill_total')">{{busy==='fill_total'?'正在填写…':'填写价格和成交额（不下单）'}}</button>
    <div v-if="fillResult" class="notice" role="status">
      <p>平台实际价格：{{fillResult.values.price}} · 成交额：{{fillResult.values.quote_amount}} {{quote}} · 换算数量：{{fillResult.values.quantity}} {{symbol}}</p>
      <p v-if="!invalidFields(fillResult).length&&!fillResult.page_errors.length">未读取到输入错误提示。未点击买卖提交。</p>
      <p v-for="message in [...invalidFields(fillResult),...fillResult.page_errors]" :key="message">{{message}}</p>
    </div>
    <hr />
    <h3>可用余额</h3>
    <div class="actions">
      <button class="secondary" :disabled="!!blocked" @click="run('buy_balance')">读取买入可用 USDT</button>
      <button class="secondary" :disabled="!!blocked" @click="run('sell_balance')">读取卖出可用代币数量</button>
    </div>
    <p v-if="buyBalance!==null" role="status">买入可用：{{buyBalance}}（截取至一位小数）</p>
    <p v-if="sellBalance!==null" role="status">卖出可用：{{sellBalance}}</p>
    <hr />
    <h3>当前委托</h3>
    <div class="actions">
      <button class="secondary" :disabled="!!blocked" @click="run('orders')">读取当前委托</button>
      <button class="cancel-all" :disabled="!!blocked" @click="run('cancel_all')">撤销当前委托的所有订单</button>
    </div>
    <p class="muted">撤销按钮会实际撤销平台“当前委托”面板中的全部订单。其他测试不会下单或撤单。</p>
    <p v-if="cancelMessage" class="notice" role="status">{{cancelMessage}}</p>
    <p v-if="orders?.length===0" role="status">当前没有委托。</p>
    <table v-if="orders?.length"><thead><tr><th>代币名称</th><th>方向</th><th>价格</th></tr></thead><tbody>
      <tr v-for="(order,index) in orders" :key="index"><td>{{order.symbol}}</td><td>{{order.side}}</td><td>{{order.price}} {{order.quote}}</td></tr>
    </tbody></table>
    <hr />
    <h3>卖出进度条</h3>
    <label>卖出测试价格<input v-model="sellPrice" inputmode="decimal" :disabled="!!busy" /></label>
    <button :disabled="!!blocked||!sellPrice.trim()" @click="run('sell_slider')">{{busy==='sell_slider'?'正在拖动…':'填写价格并拖到最右端（不卖出）'}}</button>
    <div v-if="sliderResult" class="notice" role="status">
      <p>平台实际价格：{{sliderResult.values.price}} · 数量：{{sliderResult.values.quantity}} {{symbol}} · 成交额：{{sliderResult.values.quote_amount}} {{quote}}</p>
      <p v-if="sliderResult.slider_value!=null">进度条读数：{{sliderResult.slider_value}} / {{sliderResult.slider_max??'平台未提供最大值'}}。未点击卖出提交。</p>
      <p v-else>已执行向右拖动；平台未提供比例读数，请对照页面查看数量。未点击卖出提交。</p>
      <p v-for="message in [...invalidFields(sliderResult),...sliderResult.page_errors]" :key="message">{{message}}</p>
    </div>
    <p v-if="busy" class="muted" role="status">正在操作受控交易页面…</p>
    <p v-if="error" class="notice error" role="alert">{{error}}</p>
  </section>
</template>

<style scoped>
hr{border:0;border-top:1px solid #314057;margin:24px 0}
.cancel-all{background:#fa4965;color:#fff}
table{width:100%;border-collapse:collapse;margin-top:14px}th,td{text-align:left;padding:10px;border-bottom:1px solid #314057}
</style>
