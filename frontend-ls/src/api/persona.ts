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
}): Promise<PersonaConfig> {
  const { data } = await client.put('/api/persona', payload)
  return data
}
