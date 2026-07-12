import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { QueueItem } from '@/types'
import { wsClient } from '@/api/ws'

export const useQueueStore = defineStore('queue', () => {
  const highItems = ref<QueueItem[]>([])
  const lowItems = ref<QueueItem[]>([])

  function updateFromSnapshot(snapshot: { high: QueueItem[]; low: QueueItem[] }) {
    highItems.value = snapshot.high || []
    lowItems.value = snapshot.low || []
  }

  wsClient.on('queue_update', (msg) => {
    if (msg.data) {
      updateFromSnapshot(msg.data)
    }
  })

  return { highItems, lowItems, updateFromSnapshot }
})
