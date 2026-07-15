import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { QueueItem } from '@/types'
import { wsClient } from '@/api/ws'
import client from '@/api/client'

export const useQueueStore = defineStore('queue', () => {
  const highItems = ref<QueueItem[]>([])
  const lowItems = ref<QueueItem[]>([])
  let pollTimer: ReturnType<typeof setInterval> | null = null

  function updateFromSnapshot(snapshot: { high: QueueItem[]; low: QueueItem[] }) {
    highItems.value = snapshot.high || []
    lowItems.value = snapshot.low || []
  }

  // HTTP 轮询获取队列（每 1.5s）
  async function pollQueue() {
    try {
      const { data } = await client.get('/api/queue')
      updateFromSnapshot(data)
    } catch {
      // ignore
    }
  }

  function startPolling() {
    if (pollTimer) return
    pollTimer = setInterval(pollQueue, 1500)
    pollQueue() // 立即拉一次
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
    highItems.value = []
    lowItems.value = []
  }

  // WebSocket 实时更新（比轮询更快）
  wsClient.on('queue_update', (msg) => {
    // if (msg.data) {
    //   updateFromSnapshot(msg.data)
    // }
  })

  return { highItems, lowItems, updateFromSnapshot, startPolling, stopPolling }
})
