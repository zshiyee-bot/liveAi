import type { WSMessage } from '@/types'
import { ROOM_KEY } from './room'

type MessageHandler = (msg: WSMessage) => void

class WSClient {
  private ws: WebSocket | null = null
  private url: string = ''
  private handlers: Map<string, Set<MessageHandler>> = new Map()
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private reconnectDelay = 1000

  connect(url: string) {
    this.url = url
    this._connect()
  }

  private _connect() {
    if (this.ws?.readyState === WebSocket.OPEN) return

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    // 多房间部署：WS 也要带房间标识，否则会收到别的房间的队列/弹幕推送
    let path = this.url
    if (ROOM_KEY && !path.includes('room=')) {
      path += (path.includes('?') ? '&' : '?') + 'room=' + encodeURIComponent(ROOM_KEY)
    }
    const wsUrl = path.startsWith('ws') ? path : `${protocol}//${window.location.host}${path}`

    this.ws = new WebSocket(wsUrl)

    this.ws.onopen = () => {
      console.log('[WS] Connected')
      this.reconnectDelay = 1000
    }

    this.ws.onmessage = (event) => {
      try {
        const msg: WSMessage = JSON.parse(event.data)
        // 忽略心跳
        if (msg.type === 'ping' || msg.type === 'pong') return
        const handlers = this.handlers.get(msg.type)
        if (handlers) {
          handlers.forEach((h) => h(msg))
        }
        // 也通知 '*' 通配符
        const all = this.handlers.get('*')
        if (all) all.forEach((h) => h(msg))
      } catch {
        // ignore
      }
    }

    this.ws.onclose = () => {
      console.log('[WS] Disconnected')
      this.reconnectTimer = setTimeout(() => this._connect(), this.reconnectDelay)
      this.reconnectDelay = Math.min(this.reconnectDelay * 2, 30000)
    }

    this.ws.onerror = () => {
      this.ws?.close()
    }
  }

  on(type: string, handler: MessageHandler) {
    if (!this.handlers.has(type)) {
      this.handlers.set(type, new Set())
    }
    this.handlers.get(type)!.add(handler)
  }

  off(type: string, handler: MessageHandler) {
    this.handlers.get(type)?.delete(handler)
  }

  disconnect() {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
    }
    this.ws?.close()
    this.ws = null
  }
}

export const wsClient = new WSClient()
