import client from './client'

/** 整体语速（直播控制面板的滑块）—— 话术和弹幕回复一起生效 */
export interface VoiceRate {
  rate: number
  enabled: boolean
  min?: number
  max?: number
}

export async function getVoiceRate(): Promise<VoiceRate> {
  const { data } = await client.get('/api/tts-rate')
  return data
}

/** 改完立即生效：从**下一条**朗读开始用新语速（正在播的那条不变），不用重启 */
export async function setVoiceRate(payload: {
  rate?: number
  enabled?: boolean
}): Promise<VoiceRate> {
  const { data } = await client.put('/api/tts-rate', payload)
  return data
}
