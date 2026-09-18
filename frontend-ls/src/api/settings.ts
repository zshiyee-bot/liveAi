import client from './client'

export interface AppSettingsData {
  livetalking_base_url: string
  llm_api_key: string
  llm_base_url: string
  llm_model: string
  embedding_api_key: string
  embedding_base_url: string
  embedding_model: string
}

export async function getSettings(): Promise<AppSettingsData> {
  const { data } = await client.get('/api/settings')
  return data.data
}

export async function updateSettings(payload: Partial<AppSettingsData>): Promise<AppSettingsData> {
  const { data } = await client.put('/api/settings', payload)
  return data.data
}

export async function reloadSettings(): Promise<void> {
  await client.post('/api/settings/reload')
}

// ── 语音合成（豆包 TTS）──────────────────────────────────────────
// 存储与素材页「本链音色」共用（data/tts_config.json），不写 DB
export interface TtsConfigData {
  has_key: boolean
  key_masked: string
  resource_id: string
  engine?: string
  ref_file?: string
  note?: string
}

export async function getTtsConfig(): Promise<TtsConfigData> {
  const { data } = await client.get('/api/tts/config')
  return data.data
}

export async function updateTtsConfig(payload: {
  doubao_api_key?: string
  doubao_resource_id?: string
}): Promise<TtsConfigData> {
  const { data } = await client.put('/api/tts/config', payload)
  return data.data
}
