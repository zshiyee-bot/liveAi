import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { QueueItem } from '@/types'
import { wsClient } from '@/api/ws'

export const useQueueStore = defineStore('queue', () => {
  const highItems = ref<QueueItem[]>([])
  const lowItems = ref<QueueItem[]>([])
  const currentItem = ref<{ id: string; source: string; content_preview: string } | null>(null)

  function updateFromSnapshot(snapshot: {
    high: QueueItem[]
    low: QueueItem[]
    playing?: { id: string; source: string; content_preview: string } | null
  }) {
    highItems.value = snapshot.high || []
    lowItems.value = snapshot.low || []
    // 「正在播放」以快照为准：后端每次队列变化都会把 playing 带上来，播完就是 null。
    // 以前只靠 playback_started 设置、从来不清空 → 没声音了还挂着最后一条。
    if ('playing' in snapshot) {
      const p = snapshot.playing
      currentItem.value = p
        ? { id: p.id, source: p.source, content_preview: p.content_preview }
        : null
    }
  }

  function startPolling() {
    // no-op, 队列现在通过 WS 实时推送，无需 HTTP 轮询
  }

  function stopPolling() {
    highItems.value = []
    lowItems.value = []
    currentItem.value = null
  }

  // 队列变更由后端广播（put_high/put_low/get_next 触发）
  wsClient.on('queue_update', (msg) => {
    if (msg.data) {
      updateFromSnapshot(msg.data)
    }
  })

  // 标记当前正在播放的 item（比快照更及时）
  wsClient.on('playback_started', (msg) => {
    if (msg.item_id) {
      currentItem.value = {
        id: msg.item_id,
        source: msg.source || '',
        content_preview: msg.content_preview || '',
      }
    }
  })

  // 播完了：立刻清掉「正在播放」，别再挂着上一条
  wsClient.on('playback_ended', () => {
    currentItem.value = null
  })

  return { highItems, lowItems, currentItem, updateFromSnapshot, startPolling, stopPolling }
})
