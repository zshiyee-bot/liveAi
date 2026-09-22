import client from './client'
import type { Script } from '@/types'

export async function getScripts(params?: { enabled_only?: boolean; type_filter?: string }): Promise<Script[]> {
  const { data } = await client.get('/api/scripts', { params })
  return data
}

export async function createScript(payload: { title: string; type: string; content: string; tags: string[]; split_sep?: string }): Promise<Script> {
  const { data } = await client.post('/api/scripts', payload)
  return data
}

/** AI 生成话术：只生成、不落库 —— 前端填进「内容」框让用户确认后再保存。
 *  多轮 = 多调几次，降低雷同；生成慢，超时放宽到 3 分钟。 */
export async function aiGenerateScripts(payload: {
  requirements: string
  count: number
  rounds: number
  max_chars?: number
}): Promise<string[]> {
  const { data } = await client.post('/api/scripts/ai_generate', payload, { timeout: 180000 })
  return data.items || []
}

/** 新建「AI 循环话术」：先按直播时长生成第一段，之后播放中自动一段接一段续写。
 *  生成慢（第一段可能 50 条），超时放宽到 3 分钟。 */
export async function createAiLoopScript(payload: {
  title?: string
  requirements: string
  per_segment: number
  max_chars: number
  total_minutes?: number
  tags?: string[]
}): Promise<Script> {
  const { data } = await client.post('/api/scripts/ai_loop', payload, { timeout: 180000 })
  return data
}

export async function updateScript(id: number, payload: Partial<Script>): Promise<Script> {
  const { data } = await client.put(`/api/scripts/${id}`, payload)
  return data
}

export async function deleteScript(id: number): Promise<void> {
  await client.delete(`/api/scripts/${id}`)
}

export async function toggleScript(id: number): Promise<{ enabled: boolean }> {
  const { data } = await client.post(`/api/scripts/${id}/toggle`)
  return data
}

export async function uploadFile(scriptId: number, file: File): Promise<{ file_path: string }> {
  const form = new FormData()
  form.append('script_id', String(scriptId))
  form.append('file', file)
  const { data } = await client.post('/api/scripts/upload-file', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 120000,
  })
  return data
}
