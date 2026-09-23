<template>
  <el-form :model="form" label-width="80px">
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
        :rows="6"
        placeholder="输入话术文字内容，可一行一句；也可以填下面的「分割符」自动切句"
      />
    </el-form-item>
    <el-form-item v-if="form.type === 'text'" label="分割符">
      <div style="width: 100%">
        <el-input v-model="form.splitSep" placeholder="例如：。 或 ， 或 ||（留空=不分割，整条念）" style="max-width: 320px" />
        <div style="font-size: 12px; color: #909399; line-height: 1.7; margin-top: 4px">
          填了才按这个符号切成多句，播放时<b>一句一句送进队列</b>（弹幕不用等整条念完）；<b>留空 = 整条一起念，不分割</b>。
          <span v-if="splitPieces.length > 1" style="color: #e6a23c">
            当前会切成 {{ splitPieces.length }} 句：{{ previewText }}
          </span>
          <span v-else>当前只有 1 句（不会分割）。</span>
        </div>
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
import { createScript, uploadFile } from '@/api/scripts'
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
