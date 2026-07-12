<template>
  <el-card>
    <template #header>
      <span><el-icon><List /></el-icon> 播放队列</span>
      <span style="margin-left: 8px; font-size: 12px; color: #999">
        高优 {{ queue.highItems.length }} / 低优 {{ queue.lowItems.length }}
      </span>
    </template>

    <!-- 高优先级 -->
    <div v-if="queue.highItems.length > 0" style="margin-bottom: 12px">
      <div style="font-size: 13px; color: #e6a23c; font-weight: 600; margin-bottom: 6px">
        <el-tag type="warning" size="small">高优</el-tag> 弹幕回复 / 礼物感谢 / 关注欢迎
      </div>
      <div
        v-for="item in queue.highItems"
        :key="item.id"
        style="padding: 6px 8px; margin-bottom: 4px; background: #fef0c7; border-radius: 4px; font-size: 12px"
      >
        <el-tag size="small" :type="sourceTag(item.source)">{{ sourceLabel(item.source) }}</el-tag>
        <span style="margin-left: 6px; color: #666">{{ item.content_preview }}</span>
      </div>
    </div>

    <!-- 低优先级 -->
    <div v-if="queue.lowItems.length > 0">
      <div style="font-size: 13px; color: #909399; font-weight: 600; margin-bottom: 6px">
        <el-tag type="info" size="small">话术</el-tag> 自动补位
      </div>
      <div
        v-for="item in queue.lowItems"
        :key="item.id"
        style="padding: 6px 8px; margin-bottom: 4px; background: #f4f4f5; border-radius: 4px; font-size: 12px"
      >
        <el-tag size="small" type="info">话术</el-tag>
        <span style="margin-left: 6px; color: #666">{{ item.content_preview }}</span>
      </div>
    </div>

    <div
      v-if="queue.highItems.length === 0 && queue.lowItems.length === 0"
      style="color: #999; text-align: center; padding: 40px 0; font-size: 13px"
    >
      队列为空，等待话术自动补位...
    </div>
  </el-card>
</template>

<script setup lang="ts">
import { useQueueStore } from '@/stores/queue'

const queue = useQueueStore()

function sourceLabel(source: string): string {
  const map: Record<string, string> = { danmaku: '弹幕', gift: '礼物', follow: '关注', script: '话术' }
  return map[source] || source
}

function sourceTag(source: string): string {
  const map: Record<string, string> = { danmaku: '', gift: 'danger', follow: 'success', script: 'info' }
  return map[source] || 'info'
}
</script>
