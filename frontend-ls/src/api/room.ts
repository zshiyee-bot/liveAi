/**
 * 房间标识（一台服务器带多场独立直播时用）
 *
 * 运营页用 /ls/?room=xxx 打开，然后：
 *   · 所有 /ls/api/* 请求自动带 X-Room-Key 头
 *   · WebSocket 自动带 ?room=xxx
 * 于是每个标签页只管自己那一场：弹幕、播放队列、LLM 记忆、WS 推送互不串台。
 *
 * 不传 room = 服务器的默认房间 → 单场直播的行为和以前完全一样。
 */
export function roomKey(): string {
  try {
    return new URLSearchParams(window.location.search).get('room') || ''
  } catch {
    return ''
  }
}

export const ROOM_KEY = roomKey()
