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
    danmaku_batch_wait: 6,
    danmaku_max_chars: 60,
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
