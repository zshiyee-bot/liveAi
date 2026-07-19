<template>
  <el-form :model="form" label-width="80px" style="max-width: 600px">
    <el-form-item label="标题">
      <el-input v-model="form.title" placeholder="话术标题" />
    </el-form-item>
    <el-form-item label="类型">
      <el-radio-group v-model="form.type">
        <el-radio value="text">文字</el-radio>
        <el-radio value="audio">音频</el-radio>
      </el-radio-group>
    </el-form-item>
    <el-form-item v-if="form.type === 'text'" label="内容">
      <el-input
        v-model="form.content"
        type="textarea"
        :rows="3"
        placeholder="输入话术文字内容..."
      />
    </el-form-item>
    <el-form-item v-if="form.type === 'audio'" label="音频">
      <el-upload
        :before-upload="handleAudioSelect"
        :show-file-list="!!selectedFile"
        :file-list="audioFileList"
        accept="audio/*"
      >
        <el-button type="primary" plain>选择音频文件</el-button>
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
import { createScript, uploadAudio } from '@/api/scripts'

const emit = defineEmits<{ created: [] }>()

const form = reactive({ title: '', type: 'text' as string, content: '', tags: [] as string[] })
const selectedFile = ref<File | null>(null)
const submitting = ref(false)

const canSubmit = computed(() => {
  if (!form.title) return false
  if (form.type === 'text' && !form.content) return false
  if (form.type === 'audio' && !selectedFile.value) return false
  return true
})

const audioFileList = computed(() => {
  if (!selectedFile.value) return []
  return [{ name: selectedFile.value.name, uid: 1 }]
})

function handleAudioSelect(file: UploadFile) {
  selectedFile.value = file as unknown as File
  return false
}

async function handleSubmit() {
  submitting.value = true
  try {
    if (form.type === 'audio' && selectedFile.value) {
      // Create text placeholder first, then upload audio
      const script = await createScript({
        title: form.title,
        type: 'audio',
        content: '',
        tags: form.tags,
      })
      await uploadAudio(script.id, selectedFile.value)
    } else {
      await createScript({
        title: form.title,
        type: 'text',
        content: form.content,
        tags: form.tags,
      })
    }
    ElMessage.success('话术已添加')
    form.title = ''
    form.content = ''
    form.tags = []
    selectedFile.value = null
    emit('created')
  } finally {
    submitting.value = false
  }
}
</script>
