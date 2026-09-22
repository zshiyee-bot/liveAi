<template>
  <el-form :model="form" label-width="80px" style="max-width: 640px">
    <el-form-item label="标题">
      <el-input v-model="form.title" placeholder="话术标题" />
    </el-form-item>
    <el-form-item label="类型">
      <el-radio-group v-model="form.type">
        <el-radio value="text">文字</el-radio>
        <el-radio value="audio">音频</el-radio>
        <el-radio value="video">视频</el-radio>
      </el-radio-group>
    </el-form-item>
    <el-form-item v-if="form.type === 'text'" label="内容">
      <el-input
        v-model="form.content"
        type="textarea"
        :rows="5"
        placeholder="输入话术文字内容，可一行一句；也可以填下面的「分割符」自动切句"
      />
    </el-form-item>
    <el-form-item v-if="form.type === 'text'" label="分割符">
      <div style="width: 100%">
        <el-input v-model="form.splitSep" placeholder="例如：。 或 ， 或 ||（留空=只按换行分）" style="max-width: 320px" />
        <div style="font-size: 12px; color: #909399; line-height: 1.7; margin-top: 4px">
          用这个符号把内容切成多句，播放时<b>一句一句送进队列</b>（弹幕不用等整条念完）。
          <span v-if="splitPieces.length > 1" style="color: #e6a23c">
            当前会切成 {{ splitPieces.length }} 句：{{ previewText }}
          </span>
          <span v-else>当前只有 1 句（不会分割）。</span>
        </div>
      </div>
    </el-form-item>

    <el-form-item v-if="form.type === 'text'" label="AI 生成">
      <div style="width: 100%; border: 1px dashed #dcdfe6; border-radius: 6px; padding: 10px 12px">
        <el-radio-group v-model="ai.mode" size="small" style="margin-bottom: 8px">
          <el-radio-button value="once">生成一批（自己确认后保存）</el-radio-button>
          <el-radio-button value="loop">循环生成（直播中自动续写）</el-radio-button>
        </el-radio-group>
        <el-input
          v-model="ai.requirements"
          type="textarea"
          :rows="2"
          placeholder="告诉 AI 你想要什么话术，例如：宠物零食带货，劝大家下单，口语化一点"
        />

        <!-- ① 生成一批 -->
        <template v-if="ai.mode === 'once'">
          <div style="display: flex; align-items: center; gap: 8px; margin-top: 8px; flex-wrap: wrap">
            <span style="font-size: 13px; color: #606266">每轮条数</span>
            <el-input-number v-model="ai.count" :min="1" :max="50" size="small" controls-position="right" style="width: 100px" />
            <span style="font-size: 13px; color: #606266">生成轮数</span>
            <el-input-number v-model="ai.rounds" :min="1" :max="10" size="small" controls-position="right" style="width: 100px" />
            <span style="font-size: 13px; color: #606266">每条字数</span>
            <el-input-number v-model="ai.maxChars" :min="10" :max="200" size="small" controls-position="right" style="width: 100px" />
            <el-button type="primary" plain :loading="ai.loading" @click="handleAiGenerate">AI 生成</el-button>
          </div>
          <div style="font-size: 12px; color: #909399; line-height: 1.7; margin-top: 6px">
            生成结果按行填进上面的「内容」框，你确认/修改后再点「添加话术」（不会自动保存）。
            多轮会多调几次模型，尽量不重复。
          </div>
        </template>

        <!-- ② 循环生成 -->
        <template v-else>
          <div style="display: flex; align-items: center; gap: 8px; margin-top: 8px; flex-wrap: wrap">
            <span style="font-size: 13px; color: #606266">每段条数</span>
            <el-input-number v-model="ai.perSegment" :min="3" :max="50" size="small" controls-position="right" style="width: 100px" />
            <span style="font-size: 13px; color: #606266">每条字数</span>
            <el-input-number v-model="ai.maxChars" :min="10" :max="200" size="small" controls-position="right" style="width: 100px" />
            <span style="font-size: 13px; color: #606266">首段时长</span>
            <el-input-number v-model="ai.totalMinutes" :min="0" :max="1440" size="small" controls-position="right" style="width: 110px" />
            <span style="font-size: 13px; color: #606266">分钟</span>
            <el-button type="primary" plain :loading="ai.loading" @click="handleAiLoop">生成并保存</el-button>
          </div>
          <div style="font-size: 12px; color: #909399; line-height: 1.7; margin-top: 6px">
            会直接存成一条<b>循环话术</b>（标题用上面填的，留空就按要求自动起名）。
            播放时一句一句送队列，<b>剩 3 句左右就自动再生成一段</b>，一段接一段永不停 ——
            话术不会长期固定，也就不容易被平台按重复话术检测。
            「首段时长」只决定第一段生成多少句（填 0 = 就用每段条数）。
          </div>
        </template>
      </div>
    </el-form-item>

    <el-form-item v-if="form.type === 'audio'" label="音频">
      <el-upload
        :before-upload="handleMediaSelect"
        :show-file-list="!!selectedFile"
        :file-list="mediaFileList"
        accept="audio/*"
      >
        <el-button type="primary" plain>选择音频文件</el-button>
      </el-upload>
    </el-form-item>
    <el-form-item v-if="form.type === 'video'" label="视频">
      <el-upload
        :before-upload="handleMediaSelect"
        :show-file-list="!!selectedFile"
        :file-list="mediaFileList"
        accept="video/*"
      >
        <el-button type="primary" plain>选择视频文件（25fps, mp4）</el-button>
      </el-upload>
    </el-form-item>
    <el-form-item label="标签">
      <el-select
        v-model="form.tags"
        multiple
        filterable
        allow-create
        placeholder="输入标签按回车"
        style="width: 100%"
      />
    </el-form-item>
    <el-form-item label=" ">
      <el-button type="primary" @click="handleSubmit" :loading="submitting" :disabled="!canSubmit">
        添加话术
      </el-button>
    </el-form-item>
  </el-form>
</template>

<script setup lang="ts">
import { ref, reactive, computed } from 'vue'
import { ElMessage } from 'element-plus'
import type { UploadFile } from 'element-plus'
import { createScript, uploadFile, aiGenerateScripts, createAiLoopScript } from '@/api/scripts'
import { splitScriptText } from '@/utils/split'

const emit = defineEmits<{ created: [] }>()

const form = reactive({
  title: '',
  type: 'text' as string,
  content: '',
  splitSep: '',
  tags: [] as string[],
})
const selectedFile = ref<File | null>(null)
const submitting = ref(false)

const ai = reactive({
  mode: 'once' as 'once' | 'loop',
  requirements: '',
  count: 5,
  rounds: 1,
  maxChars: 40,
  perSegment: 10,
  totalMinutes: 0,
  loading: false,
})

const splitPieces = computed(() => splitScriptText(form.content, form.splitSep))
const previewText = computed(() =>
  splitPieces.value.slice(0, 6).map((p) => `「${p}」`).join(' ') +
  (splitPieces.value.length > 6 ? ` …共 ${splitPieces.value.length} 句` : '')
)

const canSubmit = computed(() => {
  if (!form.title) return false
  if (form.type === 'text' && !form.content) return false
  if ((form.type === 'audio' || form.type === 'video') && !selectedFile.value) return false
  return true
})

const mediaFileList = computed(() => {
  if (!selectedFile.value) return []
  return [{ name: selectedFile.value.name, uid: 1 }]
})

function handleMediaSelect(file: UploadFile) {
  selectedFile.value = file as unknown as File
  return false
}

async function handleAiGenerate() {
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
    const merged = [...(form.content ? [form.content.trim()] : []), ...items].join('\n')
    form.content = merged
    if (!form.title.trim()) form.title = ai.requirements.trim().slice(0, 20)
    ElMessage.success(`AI 生成了 ${items.length} 条，已填入内容框`)
  } catch {
    // error handled by interceptor
  } finally {
    ai.loading = false
  }
}

async function handleAiLoop() {
  if (!ai.requirements.trim()) {
    ElMessage.warning('先写一下你想要什么话术')
    return
  }
  ai.loading = true
  try {
    const script = await createAiLoopScript({
      title: form.title.trim() || undefined,
      requirements: ai.requirements.trim(),
      per_segment: ai.perSegment,
      max_chars: ai.maxChars,
      total_minutes: ai.totalMinutes,
      tags: form.tags,
    })
    ElMessage.success(`已创建循环话术「${script.title}」（首段 ${script.ai_loop?.buffer?.length ?? 0} 句，开播后自动续写）`)
    form.title = ''
    form.tags = []
    emit('created')
  } catch {
    // error handled by interceptor
  } finally {
    ai.loading = false
  }
}

async function handleSubmit() {
  submitting.value = true
  try {
    const script = await createScript({
      title: form.title,
      type: form.type,
      content: form.content,
      tags: form.tags,
      split_sep: form.splitSep,
    })
    if (selectedFile.value) {
      await uploadFile(script.id, selectedFile.value)
    }
    ElMessage.success('话术已添加')
    form.title = ''
    form.content = ''
    form.splitSep = ''
    form.tags = []
    selectedFile.value = null
    emit('created')
  } finally {
    submitting.value = false
  }
}
</script>
