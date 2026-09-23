import client from './client'
import type { PersonaConfig } from '@/types'

export async function getPersona(): Promise<PersonaConfig> {
  const { data } = await client.get('/api/persona')
  return data
}

export async function updatePersona(payload: {
  name: string
  personality: string
  style: string
  knowledge_scope: string
  forbidden_topics: string[]
  // 弹幕聚合回复（可选）
  danmaku_policy?: string
  danmaku_batch_trigger?: number
  danmaku_batch_wait?: number
  danmaku_max_chars?: number
  // 弹幕安全（确定性执行，见 server/.../danmaku_filter.py）
  danmaku_block_words?: string
  danmaku_block_mode?: string
  danmaku_block_noise?: number
  danmaku_inject_filter?: number
  danmaku_max_len?: number
  danmaku_rate_limit?: number
  danmaku_fallback?: string
  // 弹幕口播方式（称呼 / 复述原文）
  danmaku_call_name?: number
  danmaku_read_msg?: number
  danmaku_name_max?: number
  danmaku_read_msg_max?: number
  danmaku_reply_templates?: string
}): Promise<PersonaConfig> {
  const { data } = await client.put('/api/persona', payload)
  return data
}
