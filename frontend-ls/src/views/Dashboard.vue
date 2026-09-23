<template>
  <div>
    <h1 style="margin: 0 0 20px 0; font-size: 22px">直播控制面板</h1>

    <!-- Session 选择 + 控制按钮 -->
    <el-card style="margin-bottom: 20px">
      <el-row :gutter="16" align="middle">
        <!-- LiveTalking URL -->
        <!-- <el-col :span="6">
          <el-input v-model="store.ltBaseUrl" placeholder="LiveTalking URL" size="default">
            <template #prepend>URL</template>
          </el-input>
        </el-col> -->
        <!-- Session 选择 -->
        <el-col :span="8">
          <el-select
            v-model="store.sessionId"
            placeholder="选择 LiveTalking Session"
            style="width: 100%"
            :loading="store.fetchingSessions"
          >
            <el-option
              v-for="s in store.ltSessions"
              :key="s.sessionid"
              :label="`${s.sessionid.slice(0, 8)}... (${s.model || '?'} / ${s.transport || '?'})`"
              :value="s.sessionid"
            />
          </el-select>
        </el-col>
        <el-col :span="2">
          <el-button @click="store.fetchSessions()" :loading="store.fetchingSessions" type="primary" plain>
            刷新
          </el-button>
        </el-col>
        <!-- 平台 -->
        <el-col :span="3">
          <el-select v-model="platform" :disabled="store.running" size="default" style="width: 100%">
            <el-option label="抖音" value="douyin" />
            <el-option label="视频号" value="wxlive" />
            <el-option label="B站" value="bilibili" />
          </el-select>
        </el-col>
        <!-- 房间号（仅 B 站需要） -->
        <el-col v-if="platform === 'bilibili'" :span="3">
          <el-input v-model="roomId" placeholder="房间号" size="default" :disabled="store.running" />
        </el-col>
        <!-- 控制按钮 -->
        <el-col :span="5">
          <el-button
            v-if="!store.running"
            type="success"
            :disabled="!canStart"
            @click="handleStart"
          >
            <el-icon><VideoPlay /></el-icon> 开始直播
          </el-button>
          <el-button v-else type="danger" @click="handleStop">
            <el-icon><VideoPause /></el-icon> 停止直播
          </el-button>
          <!-- <el-button v-if="store.running" @click="handleInterrupt" type="warning" plain>
            打断
          </el-button> -->
        </el-col>
      </el-row>
    </el-card>

    <!-- 模拟弹幕测试 -->
    <el-card v-if="store.running" style="margin-bottom: 20px">
      <template #header>
        <span><el-icon><Tools /></el-icon> 弹幕测试</span>
      </template>
      <el-row :gutter="8" align="middle">
        <el-col :span="4">
          <el-select v-model="mockType" size="small" style="width: 100%">
            <el-option label="弹幕" value="danmaku" />
            <el-option label="礼物" value="gift" />
            <el-option label="关注" value="follow" />
          </el-select>
        </el-col>
        <el-col :span="3">
          <el-input v-model="mockSender" size="small" placeholder="发送者" />
        </el-col>
        <el-col :span="6">
          <el-input v-model="mockContent" size="small" placeholder="内容" @keyup.enter="sendMock" />
        </el-col>
        <el-col :span="2">
          <!-- 注意：故意不加 :loading —— Element 的 loading 会把按钮禁用，
               连点两下时第二下会被丢掉，测试弹幕聚合时会误以为"只发出去一条" -->
          <el-button size="small" type="primary" @click="sendMock">发送</el-button>
        </el-col>
        <el-col :span="9">
          <el-button size="small" @click="quickMock('danmaku', '观众A', '主播好厉害！')">弹幕</el-button>
          <el-button size="small" @click="quickMock('danmaku', '观众B', '今天天气怎么样')">知识问答</el-button>
          <el-button size="small" @click="quickMock('gift', '大佬', '火箭 x1')" type="warning">礼物</el-button>
          <el-button size="small" @click="quickMock('follow', '新粉丝', '')" type="success">关注</el-button>
        </el-col>
      </el-row>
    </el-card>

    <!-- 整体语速：话术 / 弹幕回复 一起生效，改了立刻作用于下一条 -->
    <el-card style="margin-bottom: 20px">
      <template #header>
        <span><el-icon><Microphone /></el-icon> 整体语速</span>
        <el-tag size="small" type="success" style="margin-left: 8px">话术 + 弹幕回复都生效</el-tag>
      </template>
      <el-row :gutter="16" align="middle">
        <el-col :xs="24" :md="14">
          <el-slider
            v-model="voiceRate"
            :min="voiceMin"
            :max="voiceMax"
            :step="5"
            show-input
            :disabled="voiceSaving"
            @change="handleSaveRate"
          />
        </el-col>
        <el-col :xs="24" :md="10">
          <el-tag :type="voiceRate === 0 ? 'info' : (voiceRate > 0 ? 'warning' : 'success')" size="small">
            {{ rateLabel }}
          </el-tag>
          <el-button size="small" text style="margin-left: 6px" @click="handleResetRate">恢复原速</el-button>
          <div style="font-size: 12px; color: #909399; line-height: 1.7; margin-top: 6px">
            <b>0 = 豆包原速</b>；负数更慢、正数更快（范围 {{ voiceMin }} ~ {{ voiceMax }}，每次 5）。
            <b>改完立刻生效</b>，从下一条开始念；正在播的那条不受影响，也不用重启服务。
            读弹幕回复和念队列话术走的是同一个语速。
          </div>
        </el-col>
      </el-row>
    </el-card>

    <el-row :gutter="16">
      <!-- 弹幕流 -->
      <el-col :span="14">
        <LiveChat />
      </el-col>
      <!-- 队列状态 -->
      <el-col :span="10">
        <QueueStatus />
      </el-col>
    </el-row>

    <!-- 状态栏 -->
    <div style="margin-top: 16px; color: #909399; font-size: 13px">
      <el-tag :type="store.running ? 'success' : 'info'" size="small">
        {{ store.running ? '直播中' : '已停止' }}
      </el-tag>
      <span v-if="store.running" style="margin-left: 12px">
        房间: {{ store.roomId }} | Session: {{ store.sessionId?.slice(0, 12) }}... |
        弹幕数: {{ store.danmakuCount }}
      </span>
      <!-- 多房间部署才有：/ls/?room=xxx 打开时会显示，单房间不显示 -->
      <el-tag v-if="ROOM_KEY" type="warning" size="small" style="margin-left: 12px">
        运营房间: {{ ROOM_KEY }}
      </el-tag>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { useLivestreamStore } from '@/stores/livestream'
import { useQueueStore } from '@/stores/queue'
import LiveChat from '@/components/dashboard/LiveChat.vue'
import QueueStatus from '@/components/dashboard/QueueStatus.vue'
import client from '@/api/client'
import { ROOM_KEY } from '@/api/room'
import { getVoiceRate, setVoiceRate } from '@/api/voice'

const store = useLivestreamStore()
const queueStore = useQueueStore()
const roomId = ref('')
const platform = ref('douyin')

// ── 整体语速 ────────────────────────────────────────────────
// 存在服务端 data/tts_style.json 的 rate 字段里，不用重启、下一条就生效。
// 读弹幕回复和念队列话术都走 engine._do_send 这一个出口，所以一个值管全部。
const voiceRate = ref(0)
const voiceMin = ref(-50)
const voiceMax = ref(100)
const voiceSaving = ref(false)

const rateLabel = computed(() => {
  const r = voiceRate.value
  if (!r) return '原速（0）'
  return r > 0 ? `偏快 +${r}` : `偏慢 ${r}`
})

async function loadVoiceRate() {
  try {
    const v = await getVoiceRate()
    voiceRate.value = v.rate ?? 0
    if (typeof v.min === 'number') voiceMin.value = v.min
    if (typeof v.max === 'number') voiceMax.value = v.max
  } catch {
    // 老版本服务端没有这个接口 → 保持默认，不打扰用户
  }
}

async function handleSaveRate(val: number | number[]) {
  const r = Array.isArray(val) ? val[0] : val
  voiceSaving.value = true
  try {
    const v = await setVoiceRate({ rate: r })
    voiceRate.value = v.rate
    ElMessage.success(`整体语速已设为 ${v.rate === 0 ? '原速' : v.rate}（下一条生效）`)
  } catch {
    await loadVoiceRate()
  } finally {
    voiceSaving.value = false
  }
}

async function handleResetRate() {
  await handleSaveRate(0)
}

const mockType = ref('danmaku')
const mockSender = ref('测试观众')
const mockContent = ref('主播好厉害！')
const mockSending = ref(false)

async function sendMock() {
  mockSending.value = true
  try {
    const form = new FormData()
    form.append('content', mockContent.value)
    form.append('sender', mockSender.value)
    form.append('msg_type', mockType.value)
    await client.post('/api/mock/danmaku', form)
  } catch {
    // error handled by interceptor
  } finally {
    mockSending.value = false
  }
}

async function quickMock(type: string, sender: string, content: string) {
  mockType.value = type
  mockSender.value = sender
  mockContent.value = content
  await sendMock()
}

const canStart = computed(() => {
  if (!store.sessionId) return false
  if (platform.value === 'bilibili' && !roomId.value) return false
  return true
})

onMounted(() => {
  store.fetchStatus()
  store.fetchSessions()
  loadVoiceRate()
})

async function handleStart() {
  try {
    await store.startLive(roomId.value, store.sessionId, platform.value)
    queueStore.startPolling()
    ElMessage.success('直播已启动')
  } catch {
    // error handled by interceptor
  }
}

async function handleStop() {
  try {
    await store.stopLive()
    queueStore.stopPolling()
    ElMessage.success('直播已停止')
  } catch {
    // error handled by interceptor
  }
}

async function handleInterrupt() {
  try {
    const { interruptLivestream } = await import('@/api/livestream')
    await interruptLivestream()
    ElMessage.info('已打断')
  } catch {
    // ignore
  }
}
</script>
