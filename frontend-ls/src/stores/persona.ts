import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getPersona } from '@/api/persona'
import type { PersonaConfig } from '@/types'

export const usePersonaStore = defineStore('persona', () => {
  const persona = ref<PersonaConfig>({
    id: 0,
    name: '小助手',
    personality: '',
    style: '',
    knowledge_scope: '',
    forbidden_topics: [],
    danmaku_policy: '',
    danmaku_batch_trigger: 3,
    danmaku_batch_wait: 3,
    danmaku_max_chars: 60,
    // 弹幕安全（默认全开，屏蔽词默认 "1"）
    danmaku_block_words: '1',
    danmaku_block_mode: 'exact',
    danmaku_block_noise: 1,
    danmaku_inject_filter: 1,
    danmaku_max_len: 60,
    danmaku_rate_limit: 3,
    danmaku_fallback: '这个我就不接了啊，咱们还是聊产品。',
    // 口播方式（默认和以前一样：不带称呼、不复述原文）
    danmaku_call_name: 0,
    danmaku_read_msg: 0,
    danmaku_name_max: 6,
    danmaku_read_msg_max: 24,
    danmaku_reply_templates: '',
    updated_at: null,
  })
  const loading = ref(false)

  async function fetch() {
    loading.value = true
    try {
      persona.value = await getPersona()
    } finally {
      loading.value = false
    }
  }

  return { persona, loading, fetch }
})
