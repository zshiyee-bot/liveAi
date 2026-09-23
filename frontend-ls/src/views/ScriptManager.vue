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
        <el-table-column label="分割符" width="210">
          <template #default="{ row }">
            <!-- 直接在这里改：点一下就能全选重填、也能一键清空，
                 不像以前弹个提示框还得先自己删掉旧值 -->
            <template v-if="row.type === 'text'">
              <el-input
                v-model="row.split_sep"
                size="small"
                clearable
                maxlength="8"
                placeholder="留空 = 整条念，不分割"
                @change="handleSepChange(row)"
              />
              <div class="sep-hint" :class="sepHint(row).cls">{{ sepHint(row).text }}</div>
            </template>
            <span v-else style="color: #c0c4cc">-</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-switch
              :model-value="row.enabled"
              @change="handleToggle(row)"
              size="small"
            />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="140">
          <template #default="{ row }">
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

/** 列表里输入框下面那行小字：这一条到底会切成几句 */
function sepHint(row: any): { text: string; cls: string } {
  const sep = String(row?.split_sep || '').trim()
  // 循环话术：句子在生成时就切好了，播放走缓冲区，改这里不影响已经在手的句子
  if (row?.ai_loop?.enabled) {
    return { text: sep ? `循环话术·续写按「${sep}」切` : '循环话术·生成时已切好', cls: 'info' }
  }
  if (!sep) return { text: '整条一起念（不分割）', cls: 'info' }
  const n = splitCount(row)
  if (n > 1) return { text: `会切成 ${n} 句`, cls: 'ok' }
  return { text: '文案里找不到这个符号 → 不会分割', cls: 'warn' }
}

/** 直接在列表里改「分割符」（留空 = 不分割、整条一起念）*/
async function handleSepChange(row: any) {
  const sep = String(row?.split_sep || '').trim().slice(0, 8)
  row.split_sep = sep
  try {
    await updateScript(row.id, { split_sep: sep } as any)
    const n = splitCount(row)
    if (!sep) ElMessage.success('已设为整条一起念（不分割）')
    else if (n > 1) ElMessage.success(`已设为按「${sep}」切成 ${n} 句`)
    else ElMessage.warning(`已保存「${sep}」，但这条文案里找不到这个符号，所以还是 1 句`)
  } catch {
    await store.fetchAll()   // 保存失败 → 拉回真实状态，别让界面骗人
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

<style scoped>
.sep-hint {
  font-size: 12px;
  line-height: 1.5;
  margin-top: 2px;
}
.sep-hint.info { color: #909399; }
.sep-hint.ok   { color: #e6a23c; }
.sep-hint.warn { color: #f56c6c; }
</style>
