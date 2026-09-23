<template>
  <div>
    <h1 style="margin: 0 0 20px 0; font-size: 22px">话术管理</h1>
    <el-row :gutter="20" style="margin-bottom: 20px">
      <el-col :xs="24" :md="11">
        <el-card style="height: 100%">
          <template #header><span><el-icon><Plus /></el-icon> 添加话术</span></template>
          <ScriptForm @created="onCreated" />
        </el-card>
      </el-col>
      <el-col :xs="24" :md="13">
        <el-card style="height: 100%">
          <template #header>
            <span><el-icon><MagicStick /></el-icon> AI 生成话术</span>
            <el-tag size="small" type="info" style="margin-left: 8px">生成一批 / 循环生成</el-tag>
          </template>
          <ScriptAiForm @created="onCreated" />
        </el-card>
      </el-col>
    </el-row>
    <el-card>
      <template #header>
        <span><el-icon><Document /></el-icon> 话术列表</span>
        <el-button size="small" style="float: right" @click="store.fetchAll()" :loading="store.loading">
          刷新
        </el-button>
      </template>
      <el-table :data="store.scripts" stripe v-loading="store.loading" style="width: 100%">
        <el-table-column prop="id" label="ID" width="60" />
        <el-table-column prop="title" label="标题" min-width="150" />
        <el-table-column label="类型" width="80">
          <template #default="{ row }">
            <el-tag :type="row.type === 'audio' ? 'warning' : row.type === 'video' ? 'danger' : ''" size="small">
              {{ typeLabel(row.type) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="内容预览" min-width="200">
          <template #default="{ row }">
            <el-tag v-if="row.ai_loop?.enabled" size="small" type="success" style="margin-right: 6px">
              AI 循环·剩 {{ row.ai_loop.buffer?.length ?? 0 }} 句
            </el-tag>
            <el-tag v-if="splitCount(row) > 1" size="small" type="warning" style="margin-right: 6px">
              分句「{{ row.split_sep || '换行' }}」{{ splitCount(row) }} 句
            </el-tag>
            <span style="font-size: 13px; color: #666">{{ row.content?.slice(0, 80) || row.file_path || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="播放次数" width="80" prop="play_count" />
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-switch
              :model-value="row.enabled"
              @change="handleToggle(row)"
              size="small"
            />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="210">
          <template #default="{ row }">
            <el-button v-if="row.type === 'text'" size="small" type="primary" @click="handleSetSep(row)" text>
              分割符
            </el-button>
            <el-button size="small" type="danger" @click="handleDelete(row.id)" text>删除</el-button>
            <el-upload
              v-if="row.type === 'audio' || row.type === 'video' || !row.content"
              :show-file-list="false"
              :before-upload="(f: UploadFile) => handleUpload(row.id, f)"
              :accept="row.type === 'video' ? 'video/*' : 'audio/*'"
              style="display: inline-block; margin-left: 4px"
            >
              <el-button size="small" type="primary" text>
                {{ row.type === 'video' ? '上传视频' : '上传音频' }}
              </el-button>
            </el-upload>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { UploadFile } from 'element-plus'
import { useScriptsStore } from '@/stores/scripts'
import { deleteScript, toggleScript, uploadFile, updateScript } from '@/api/scripts'
import { splitScriptText } from '@/utils/split'
import ScriptForm from '@/components/scripts/ScriptForm.vue'
import ScriptAiForm from '@/components/scripts/ScriptAiForm.vue'

const store = useScriptsStore()

onMounted(() => store.fetchAll())

const TYPE_LABELS: Record<string, string> = { text: '文字', audio: '音频', video: '视频' }
function typeLabel(t: string): string { return TYPE_LABELS[t] || t }

// 与后端 server/livestream/services/script_manager.py:split_script_text 保持一致
function splitCount(row: any): number {
  return splitScriptText(row?.content || '', row?.split_sep || '').length
}

/** 给已经保存好的话术设置「分割符」（留空 = 不分割、整条念） */
async function handleSetSep(row: any) {
  try {
    const { value } = await ElMessageBox.prompt(
      `给「${row.title}」设置分割符：播放时会按它<b>一句一句</b>念；<b>留空 = 整条一起念</b>。<br/>例：<code>。</code> 或 <code>，</code> 或 <code>||</code>`,
      '设置分割符',
      {
        inputValue: row.split_sep || '',
        confirmButtonText: '保存',
        cancelButtonText: '取消',
        dangerouslyUseHTMLString: true,
        inputPlaceholder: '留空 = 不分割',
      }
    )
    const sep = String(value || '').slice(0, 8)
    await updateScript(row.id, { split_sep: sep } as any)
    row.split_sep = sep
    ElMessage.success(sep ? `已设为按「${sep}」逐句念` : '已设为整条一起念')
    await store.fetchAll()
  } catch {
    // 取消 / 接口错误都由拦截器处理
  }
}

async function onCreated() {
  await store.fetchAll()
}

async function handleToggle(row: any) {
  try {
    const result = await toggleScript(row.id)
    row.enabled = result.enabled
  } catch {
    // error handled by interceptor
  }
}

async function handleDelete(id: number) {
  try {
    await ElMessageBox.confirm('确认删除？', '提示', { type: 'warning' })
    await deleteScript(id)
    ElMessage.success('已删除')
    await store.fetchAll()
  } catch {
    // cancelled or error
  }
}

async function handleUpload(scriptId: number, file: UploadFile) {
  try {
    await uploadFile(scriptId, file as unknown as File)
    ElMessage.success('文件已上传')
    await store.fetchAll()
  } catch {
    // error handled
  }
  return false
}
</script>
