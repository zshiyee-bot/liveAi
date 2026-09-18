import axios from 'axios'
import { ElMessage } from 'element-plus'

const client = axios.create({
  // 合并进 LiveTalking 后统一挂在前缀 /ls 下（原来是独立进程 8020、根路径）
  baseURL: '/ls',
  timeout: 30000,
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
