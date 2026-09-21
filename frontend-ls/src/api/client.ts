import axios from 'axios'
import { ElMessage } from 'element-plus'
import { ROOM_KEY } from './room'

const client = axios.create({
  // 合并进 LiveTalking 后统一挂在前缀 /ls 下（原来是独立进程 8020、根路径）
  baseURL: '/ls',
  timeout: 30000,
})

// 多房间部署：/ls/?room=xxx 打开时，所有接口自动带上房间标识
client.interceptors.request.use((config) => {
  if (ROOM_KEY) {
    config.headers = config.headers ?? {}
    ;(config.headers as Record<string, string>)['X-Room-Key'] = ROOM_KEY
  }
  return config
})

client.interceptors.response.use(
  (resp) => resp,
  (error) => {
    const msg = error.response?.data?.detail || error.message || '请求失败'
    ElMessage.error(msg)
    return Promise.reject(error)
  }
)

export default client
