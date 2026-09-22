<template>
  <el-form :model="ai" label-width="80px">
    <el-form-item label="模式">
      <el-radio-group v-model="ai.mode">
        <el-radio value="once">生成一批</el-radio>
        <el-radio value="loop">循环生成</el-radio>
      </el-radio-group>
    </el-form-item>

    <el-form-item v-if="ai.mode === 'loop'" label="标题">
      <el-input v-model="ai.title" placeholder="留空就按下面的要求自动起名" />
    </el-form-item>

    <el-form-item label="要求">
      <el-input
        v-model="ai.requirements"
        type="textarea"
        :rows="3"
        placeholder="告诉 AI 你想要什么话术，例如：宠物零食带货，劝大家下单，口语化一点"
      />
    </el-form-item>

    <el-form-item label="分割符">
      <div style="width: 100%">
        <el-input v-model="ai.splitSep" placeholder="例如：。 或 ， 或 ||（留空=生成多少条就存多少条）" style="max-width: 320px" />
        <div style="font-size: 12px; color: #909399; line-height: 1.7; margin-top: 4px">
          <template v-if="ai.splitSep.trim()">
            生成出来的每一条再按「{{ ai.splitSep }}」切成多句，播放时<b>一句一句送队列</b>。
            <span v-if="splitPreview" style="color: #e6a23c">{{ splitPreview }}</span>
          </template>
          <template v-else>
            AI 每次生成的内容本身就是一句一条，播放时已经是一句一句的；想再切细就填分割符。
          </template>
        </div>
      </div>
    </el-form-item>

    <!-- ① 生成一批 -->
    <template v-if="ai.mode === 'once'">
      <el-form-item label="生成">
        <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap">
          <span style="font-size: 13px; color: #606266">每轮条数</span>
          <el-input-number v-model="ai.count" :min="1" :max="50" size="small" controls-position="right" style="width: 100px" />
          <span style="font-size: 13px; color: #606266">生成轮数</span>
          <el-input-number v-model="ai.rounds" :min="1" :max="10" size="small" controls-position="right" style="width: 100px" />
          <el-button type="primary" :loading="ai.loading" @click="handleGenerate">{{ lines.length ? '再生成一批' : 'AI 生成' }}</el-button>
        </div>
      </el-form-item>
      <el-form-item label="结果">
        <div style="width: 100%">
          <el-input
            v-model="ai.text"
            type="textarea"
            :rows="10"
            placeholder="生成结果会出现在这里：一行一条话术，可以直接改，也可以手写补充几条。"
          />
          <div style="display: flex; align-items: center; gap: 8px; margin-top: 8px; flex-wrap: wrap">
            <el-button type="primary" plain :loading="ai.saving" :disabled="!lines.length" @click="handleSaveAll">
              全部保存为话术（{{ lines.length }} 条）
            </el-button>
            <el-button text @click="ai.text = ''">清空</el-button>
          </div>
          <div style="font-size: 12px; color: #909399; line-height: 1.7; margin-top: 4px">
            保存后就是话术库里的普通话术：<b>一行 = 一条</b><span v-if="ai.splitSep.trim()">，每条再按「{{ ai.splitSep }}」切成多句播放</span>。
            多轮生成会多调几次模型，尽量不重复。
          </div>
        </div>
      </el-form-item>
    </template>

    <!-- ② 循环生成 -->
    <template v-else>
      <el-form-item label="参数">
        <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap">
          <span style="font-size: 13px; color: #606266">每段条数</span>
          <el-input-number v-model="ai.perSegment" :min="3" :max="50" size="small" controls-position="right" style="width: 100px" />
          <span style="font-size: 13px; color: #606266">每条字数</span>
          <el-input-number v-model="ai.maxChars" :min="10" :max="200" size="small" controls-position="right" style="width: 100px" />
          <span style="font-size: 13px; color: #606266">首段时长</span>
          <el-input-number v-model="ai.totalMinutes" :min="0" :max="1440" size="small" controls-position="right" style="width: 110px" />
          <span style="font-size: 13px; color: #606266">分钟</span>
        </div>
      </el-form-item>
      <el-form-item label=" ">
        <div style="width: 100%">
          <el-button type="primary" :loading="ai.loading" @click="handleLoop">生成并保存</el-button>
        </div>
      </el-form-item>
      <el-form-item label=" ">
        <div style="font-size: 12px; color: #909399; line-height: 1.8">
          直接存成一条<b>循环话术</b>：播放时一句一句送队列，<b>剩 3 句左右就自动再生成一段</b>，
          一段接一段永不停 —— 话术不会长期固定，也就不容易被平台按重复话术检测。
          「首段时长」只决定第一段生成多少句（填 0 = 就用每段条数）。
          不想用了在下面列表里关掉开关或删掉。
        </div>
      </el-form-item>
    </template>
  </el-form>
</template>

<script setup lang="ts">
import { reactive, computed } from 'vue'
import { ElMessage } from 'element-plus'
import { aiGenerateScripts, createAiLoopScript, saveScriptsBatch } from '@/api/scripts'
import { splitScriptText } from '@/utils/split'

const emit = defineEmits<{ created: [] }>()

const ai = reactive({
  mode: 'once' as 'once' | 'loop',
  title: '',
  requirements: '',
  splitSep: '',
  count: 5,
  rounds: 1,
  maxChars: 40,
  perSegment: 10,
  totalMinutes: 0,
  text: '',
  loading: false,
  saving: false,
})

const lines = computed(() =>
  ai.text
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter(Boolean)
)

// 生成一批：预览「按分割符一共会切成几句」
const splitPreview = computed(() => {
  if (!lines.value.length) return ''
  const total = lines.value.reduce((n, l) => n + splitScriptText(l, ai.splitSep).length, 0)
  const pieces = splitScriptText(lines.value[0], ai.splitSep)
  if (pieces.length <= 1) return `${lines.value.length} 条，每条 1 句`
  return `${lines.value.length} 条 → 共 ${total} 句，例：${pieces.slice(0, 3).map((p) => `「${p}」`).join('')}`
})

async function handleGenerate() {
  if (!ai.requirements.trim()) {
    ElMessage.warning('先写一下你想要什么话术')
    return
  }
  ai.loading = true
  try {
    const items = await aiGenerateScripts({
      requirements: ai.requirements.trim(),
      count: ai.count,
      rounds: ai.rounds,
      max_chars: ai.maxChars,
    })
    if (!items.length) {
      ElMessage.warning('AI 没有生成内容，换个描述再试')
      return
    }
    const merged = [...lines.value, ...items].join('\n')
    ai.text = merged
    ElMessage.success(`AI 生成了 ${items.length} 条，已放进结果框（确认/修改后点「全部保存为话术」）`)
  } catch {
    // error handled by interceptor
  } finally {
    ai.loading = false
  }
}

async function handleSaveAll() {
  if (!lines.value.length) {
    ElMessage.warning('结果框里还没有内容')
    return
  }
  ai.saving = true
  try {
    const made = await saveScriptsBatch({
      items: lines.value.map((content) => ({ content })),
      split_sep: ai.splitSep,
    })
    ElMessage.success(`已保存 ${made.length} 条话术`)
    ai.text = ''
    emit('created')
  } catch {
    // error handled by interceptor
  } finally {
    ai.saving = false
  }
}

async function handleLoop() {
  if (!ai.requirements.trim()) {
    ElMessage.warning('先写一下你想要什么话术')
    return
  }
  ai.loading = true
  try {
    const script = await createAiLoopScript({
      title: ai.title.trim() || undefined,
      requirements: ai.requirements.trim(),
      per_segment: ai.perSegment,
      max_chars: ai.maxChars,
      total_minutes: ai.totalMinutes,
      split_sep: ai.splitSep,
    })
    ElMessage.success(
      `已创建循环话术「${script.title}」（首段 ${script.ai_loop?.buffer?.length ?? 0} 句，开播后自动续写）`
    )
    ai.title = ''
    emit('created')
  } catch {
    // error handled by interceptor
  } finally {
    ai.loading = false
  }
}
</script>
