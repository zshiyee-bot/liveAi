import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { QueueItem } from '@/types'
import { wsClient } from '@/api/ws'
import client from '@/api/client'

export const useQueueStore = defineStore('queue', () => {
  const highItems = ref<QueueItem[]>([])
  const lowItems = ref<QueueItem[]>([])
  const currentItem = ref<{ id: string; source: string; content_preview: string } | null>(null)
  let pollTimer: ReturnType<typeof setInterval> | null = null

  function updateFromSnapshot(snapshot: { high: QueueItem[]; low: QueueItem[] }) {
    highItems.value = snapshot.high || []
    lowItems.value = snapshot.low || []
  }

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
    pollQueue()
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
    highItems.value = []
    lowItems.value = []
    currentItem.value = null
  }

  wsClient.on('queue_update', (msg) => {
    if (msg.data) {
      updateFromSnapshot(msg.data)
    }
  })

  wsClient.on('playback_started', (msg) => {
    if (msg.item_id) {
      currentItem.value = {
        id: msg.item_id,
        source: msg.source || '',
        content_preview: msg.content_preview || '',
      }
    }
  })

  return { highItems, lowItems, currentItem, updateFromSnapshot, startPolling, stopPolling }
})
