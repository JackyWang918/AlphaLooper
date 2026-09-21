<script setup lang="ts">
import { onMounted, ref } from 'vue'

const status = ref('正在检查连接…')
const pending = ref(false)

async function checkHealth() {
  pending.value = true
  try {
    const response = await fetch('/api/health', { signal: AbortSignal.timeout(5000) })
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    const result = await response.json()
    if (result.status !== 'ok' || result.database !== 'connected') {
      throw new Error('服务状态异常')
    }
    status.value = '后端已连接 · SQLite 可用'
  } catch {
    status.value = '后端未连接，请检查本地后端服务是否启动。'
  } finally {
    pending.value = false
  }
}

onMounted(checkHealth)
</script>

<template>
  <main>
    <p class="eyebrow">本地交易辅助控制台</p>
    <h1>AlphaLooper</h1>
    <p class="intro">开发环境已初始化，浏览器下单功能待接入。</p>
    <section>
      <h2>环境状态</h2>
      <p role="status">{{ status }}</p>
      <button :disabled="pending" @click="checkHealth">
        {{ pending ? '检查中…' : '重新检查' }}
      </button>
    </section>
    <p class="note">当前仅提供环境连接检查，不会启动交易或提交订单。</p>
  </main>
</template>
