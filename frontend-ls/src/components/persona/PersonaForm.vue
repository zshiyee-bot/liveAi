<template>
  <el-form :model="form" label-width="100px" style="max-width: 700px" v-loading="store.loading">
    <el-form-item label="主播名称">
      <el-input v-model="form.name" placeholder="如：小助手" />
    </el-form-item>
    <el-form-item label="性格特点">
      <el-input
        v-model="form.personality"
        type="textarea"
        :rows="2"
        placeholder="如：热情友好、耐心细致、幽默风趣"
      />
    </el-form-item>
    <el-form-item label="说话风格">
      <el-input
        v-model="form.style"
        type="textarea"
        :rows="2"
        placeholder="如：轻松活泼，喜欢用简短的句子"
      />
    </el-form-item>
    <el-form-item label="知识范围">
      <el-input
        v-model="form.knowledge_scope"
        type="textarea"
        :rows="2"
        placeholder="如：日常闲聊、生活百科、娱乐八卦"
      />
    </el-form-item>
    <el-form-item label="禁止话题">
      <el-select
        v-model="form.forbidden_topics"
        multiple
        filterable
        allow-create
        placeholder="输入后按回车添加"
        style="width: 100%"
      />
    </el-form-item>
    <!-- ── 弹幕聚合回复：多条弹幕 → 合并成一句话再播报 ── -->
    <el-divider content-position="left">弹幕回复（小助手设定）</el-divider>
    <el-form-item label="聚合触发条数">
      <el-input-number v-model="form.danmaku_batch_trigger" :min="1" :max="50" />
      <span class="hint">弹幕积压超过 {{ Math.max(0, form.danmaku_batch_trigger - 1) }} 条就立刻合并成一句话</span>
    </el-form-item>
    <el-form-item label="兜底等待">
      <el-input-number v-model="form.danmaku_batch_wait" :min="0.5" :max="30" :step="0.5" />
      <span class="hint">秒；不够上面的条数时等这么久就回（避免只有一两条时一直不回）</span>
    </el-form-item>
    <el-form-item label="一句话上限">
      <el-input-number v-model="form.danmaku_max_chars" :min="10" :max="200" />
      <span class="hint">字；多条弹幕会被合并成「一句话」播报，超长只保留重点</span>
    </el-form-item>
    <el-form-item label="回复策略">
      <el-input
        v-model="form.danmaku_policy"
        type="textarea"
        :rows="3"
        placeholder="如：优先回答商品、价格、位置类问题；纯表情、刷屏、重复内容不回"
      />
    </el-form-item>
    <el-form-item label=" ">
      <el-button type="primary" @click="handleSave" :loading="saving">保存配置</el-button>
      <el-button @click="handleReset">重置</el-button>
    </el-form-item>

    <!-- Preview -->
    <el-divider />
    <div style="font-size: 13px; color: #909399">
      <div style="font-weight: 600; margin-bottom: 6px">System Prompt 预览</div>
      <pre style="background: #f5f7fa; padding: 12px; border-radius: 4px; white-space: pre-wrap; font-size: 12px">{{ previewText }}</pre>
    </div>
  </el-form>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { usePersonaStore } from '@/stores/persona'
import { updatePersona } from '@/api/persona'

const store = usePersonaStore()
const saving = ref(false)

const form = reactive({
  name: '',
  personality: '',
  style: '',
  knowledge_scope: '',
  forbidden_topics: [] as string[],
  danmaku_policy: '',
  danmaku_batch_trigger: 3,
  danmaku_batch_wait: 3,
  danmaku_max_chars: 60,
})

const previewText = computed(() => {
  const parts = [
    `你是${form.name || '小助手'}，一位正在直播的主播。`,
    `性格特点：${form.personality || '...'}`,
    `说话风格：${form.style || '...'}`,
    `知识范围：${form.knowledge_scope || '...'}`,
  ]
  if (form.forbidden_topics.length > 0) {
    parts.push(`禁止谈论：${form.forbidden_topics.join('、')}`)
  }
  if (form.danmaku_policy) {
    parts.push(`弹幕回复策略：${form.danmaku_policy}`)
  }
  parts.push(
    `弹幕积压超过 ${Math.max(0, form.danmaku_batch_trigger - 1)} 条时，把多条合并成一句（≤${form.danmaku_max_chars}字）播报`
  )
  return parts.join('\n')
})

onMounted(async () => {
  await store.fetch()
  const p = store.persona
  form.name = p.name
  form.personality = p.personality
  form.style = p.style
  form.knowledge_scope = p.knowledge_scope
  form.forbidden_topics = [...p.forbidden_topics]
  form.danmaku_policy = p.danmaku_policy || ''
  form.danmaku_batch_trigger = p.danmaku_batch_trigger || 3
  form.danmaku_batch_wait = p.danmaku_batch_wait || 3
  form.danmaku_max_chars = p.danmaku_max_chars || 60
})

async function handleSave() {
  saving.value = true
  try {
    await updatePersona({ ...form })
    ElMessage.success('人设已保存')
  } finally {
    saving.value = false
  }
}

function handleReset() {
  store.fetch().then(() => {
    const p = store.persona
    form.name = p.name
    form.personality = p.personality
    form.style = p.style
    form.knowledge_scope = p.knowledge_scope
    form.forbidden_topics = [...p.forbidden_topics]
  form.danmaku_policy = p.danmaku_policy || ''
  form.danmaku_batch_trigger = p.danmaku_batch_trigger || 3
  form.danmaku_batch_wait = p.danmaku_batch_wait || 3
  form.danmaku_max_chars = p.danmaku_max_chars || 60
  })
}
</script>

<style scoped>
.hint {
  margin-left: 10px;
  font-size: 12px;
  color: #909399;
}
</style>
