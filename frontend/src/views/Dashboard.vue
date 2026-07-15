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
        <!-- 房间号 -->
        <el-col :span="5">
          <el-input v-model="roomId" placeholder="直播间房号" size="default" :disabled="store.running">
            <template #prepend>房间</template>
          </el-input>
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
          <el-button v-if="store.running" @click="handleInterrupt" type="warning" plain>
            打断
          </el-button>
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
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { ElMessage } from 'element-plus'
import { useLivestreamStore } from '@/stores/livestream'
import { useQueueStore } from '@/stores/queue'
import LiveChat from '@/components/dashboard/LiveChat.vue'
import QueueStatus from '@/components/dashboard/QueueStatus.vue'
import { wsClient } from '@/api/ws'

const store = useLivestreamStore()
const queueStore = useQueueStore()
const roomId = ref('')

const canStart = computed(() => roomId.value && store.sessionId)

onMounted(() => {
  wsClient.connect('/ws')
  store.fetchStatus()
  store.fetchSessions()
})

onUnmounted(() => {
  wsClient.disconnect()
  queueStore.stopPolling()
})

async function handleStart() {
  try {
    await store.startLive(roomId.value, store.sessionId)
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
