// ── Persona ──
export interface PersonaConfig {
  id: number
  name: string
  personality: string
  style: string
  knowledge_scope: string
  forbidden_topics: string[]
  // ── 弹幕聚合回复（小助手设定）：多条弹幕合并成一句话再播报 ──
  danmaku_policy: string
  danmaku_batch_trigger: number
  danmaku_batch_wait: number
  danmaku_max_chars: number
  // ── 弹幕安全（确定性执行，不依赖模型）──
  danmaku_block_words: string        // 屏蔽词，换行/逗号分隔
  danmaku_block_mode: string         // 'exact' 整条相同 | 'contains' 只要包含
  danmaku_block_noise: number        // 1 = 纯数字/纯符号/重复字不回
  danmaku_inject_filter: number      // 1 = 注入攻击过滤
  danmaku_max_len: number            // 单条弹幕长度上限，超出不回
  danmaku_rate_limit: number         // 每人每 10 秒最多回几条
  danmaku_fallback: string           // 判定异常时的兜底话术
  // ── 弹幕口播方式（称呼 / 复述原文）──
  danmaku_call_name: number
  danmaku_read_msg: number
  danmaku_name_max: number
  danmaku_read_msg_max: number
  danmaku_reply_templates: string    // 模板，一行一个
  updated_at: string | null
}

// ── Script ──
export interface ScriptAiLoop {
  enabled: boolean
  requirements: string
  per_segment: number
  max_chars: number
  total_minutes?: number
  buffer?: string[]
  batch_no?: number
  generated?: number
}

export interface Script {
  id: number
  title: string
  type: 'text' | 'audio' | 'video'
  content: string
  split_sep: string
  ai_loop?: ScriptAiLoop | null
  file_path: string | null
  tags: string[]
  enabled: boolean
  play_count: number
  last_used_at: string | null
  created_at: string | null
  updated_at: string | null
}

// ── Knowledge ──
export interface KnowledgeDoc {
  id: number
  title: string
  content_preview: string
  source_type: 'text' | 'file'
  file_path: string | null
  chunk_count: number
  created_at: string | null
}

// ── Danmaku ──
export interface DanmakuEvent {
  type: string
  platform: string
  sender: string
  content: string
  timestamp: number
}

// ── Queue ──
export interface QueueItem {
  id: string
  type: string
  source: string
  content_preview: string
  level: 'high' | 'low'
}

export interface QueueSnapshot {
  high: QueueItem[]
  low: QueueItem[]
}

// ── Livestream ──
export interface LivestreamStatus {
  running: boolean
  room_id: string
  session_id: string
  queue_high_count: number
  queue_low_count: number
  danmaku_count: number
  is_speaking: boolean
}

export interface LivetalkingSession {
  sessionid: string
  model: string
  speaking: boolean
  avatar_id: string
  transport: string
}

// ── WS Message ──
export interface WSMessage {
  type: string
  [key: string]: any
}
