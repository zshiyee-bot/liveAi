<template>
  <div>
    <h1 style="margin: 0 0 20px 0; font-size: 22px">系统配置</h1>

    <el-card style="margin-bottom: 20px">
      <template #header><span>服务连接</span></template>
      <el-form :model="form" label-width="160px" style="max-width: 600px">
        <el-form-item label="LiveTalking URL">
          <el-input v-model="form.livetalking_base_url" placeholder="http://127.0.0.1:8010" />
        </el-form-item>
      </el-form>
    </el-card>

    <el-card style="margin-bottom: 20px">
      <template #header><span>LLM 配置</span></template>
      <el-form :model="form" label-width="160px" style="max-width: 600px">
        <el-form-item label="API Key">
          <el-input v-model="form.llm_api_key" type="password" show-password placeholder="sk-..." />
        </el-form-item>
        <el-form-item label="Base URL">
          <el-input v-model="form.llm_base_url" placeholder="https://dashscope.aliyuncs.com/compatible-mode/v1" />
        </el-form-item>
        <el-form-item label="Model">
          <el-input v-model="form.llm_model" placeholder="qwen-plus" />
        </el-form-item>
      </el-form>
    </el-card>

    <el-card style="margin-bottom: 20px">
      <template #header><span>Embedding 配置</span></template>
      <el-form :model="form" label-width="160px" style="max-width: 600px">
        <el-form-item label="API Key">
          <el-input v-model="form.embedding_api_key" type="password" show-password placeholder="sk-..." />
        </el-form-item>
        <el-form-item label="Base URL">
          <el-input v-model="form.embedding_base_url" placeholder="https://dashscope.aliyuncs.com/compatible-mode/v1" />
        </el-form-item>
        <el-form-item label="Model">
          <el-input v-model="form.embedding_model" placeholder="text-embedding-v4" />
        </el-form-item>
      </el-form>
    </el-card>

    <el-card style="margin-bottom: 20px">
      <template #header><span>语音合成（豆包 TTS）</span></template>
      <el-form :model="tts" label-width="160px" style="max-width: 600px">
        <el-form-item label="当前状态">
          <el-tag :type="tts.has_key ? 'success' : 'info'" size="small">
            {{ tts.has_key ? '已配置 ' + tts.key_masked : '未配置 Key' }}
          </el-tag>
          <span v-if="tts.engine && tts.engine !== 'doubao'"
                style="margin-left: 10px; color: #e6a23c; font-size: 12px">
            启动时不是 --tts doubao，音色不生效（双击 start.bat 即为豆包）
          </span>
        </el-form-item>
        <el-form-item label="API Key">
          <el-input v-model="ttsKeyInput" type="password" show-password
                    placeholder="火山引擎控制台 → API Key 管理（留空则不改动已存的 Key）" />
        </el-form-item>
        <el-form-item label="音色类型">
          <el-select v-model="tts.resource_id" style="width: 100%">
            <el-option label="声音复刻音色（seed-icl-2.0）" value="seed-icl-2.0" />
            <el-option label="预置大模型音色（seed-tts-2.0）" value="seed-tts-2.0" />
          </el-select>
        </el-form-item>
        <el-form-item label="当前音色">
          <span style="font-size: 13px; color: #606266">{{ tts.ref_file || '（未设置，用默认）' }}</span>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="ttsSaving" @click="handleTtsSave">保存语音配置</el-button>
          <span style="margin-left: 12px; color: #909399; font-size: 12px">
            保存即生效（无需重启）；「用哪条链的哪个音色」在素材页按素材链绑定
          </span>
        </el-form-item>
      </el-form>
    </el-card>

    <el-row>
      <el-button type="primary" @click="handleSave" :loading="saving">保存配置</el-button>
      <el-button @click="handleReload" :loading="reloading" type="warning">应用并重启服务</el-button>
      <el-button @click="handleReset">重置</el-button>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { getSettings, updateSettings, reloadSettings, getTtsConfig, updateTtsConfig } from '@/api/settings'

const saving = ref(false)
const reloading = ref(false)

const form = reactive({
  livetalking_base_url: '',
  llm_api_key: '',
  llm_base_url: '',
  llm_model: '',
  embedding_api_key: '',
  embedding_base_url: '',
  embedding_model: '',
})

onMounted(async () => {
  try {
    const s = await getSettings()
    Object.assign(form, s)
  } catch { /* ignore */ }
  await loadTts()
})

async function handleSave() {
  saving.value = true
  try {
    await updateSettings(form)
    ElMessage.success('配置已保存')
  } finally {
    saving.value = false
  }
}

async function handleReload() {
  reloading.value = true
  try {
    await updateSettings(form)
    await reloadSettings()
    ElMessage.success('配置已应用，LLM 和 Embedding 已重载')
  } catch { /* error handled by interceptor */ }
  finally {
    reloading.value = false
  }
}

const ttsSaving = ref(false)
const ttsKeyInput = ref('')
const tts = reactive({
  has_key: false,
  key_masked: '',
  resource_id: 'seed-icl-2.0',
  engine: '',
  ref_file: '',
})

async function loadTts() {
  try { Object.assign(tts, await getTtsConfig()) } catch { /* ignore */ }
}

async function handleTtsSave() {
  ttsSaving.value = true
  try {
    const payload: any = { doubao_resource_id: tts.resource_id }
    if (ttsKeyInput.value.trim()) payload.doubao_api_key = ttsKeyInput.value.trim()
    Object.assign(tts, await updateTtsConfig(payload))
    ttsKeyInput.value = ''
    ElMessage.success('语音配置已保存并立即生效（双击 start.bat 也会自动用豆包复刻音色）')
  } catch { /* error handled by interceptor */ }
  finally {
    ttsSaving.value = false
  }
}

async function handleReset() {
  try {
    const s = await getSettings()
    Object.assign(form, s)
  } catch { /* ignore */ }
}
</script>
