<template>
  <div style="max-width: 900px" v-loading="store.loading">
    <el-tabs v-model="activeTab" type="border-card">
      <!-- ── ① 基础人设 ── -->
      <el-tab-pane label="基础人设" name="basic">
        <el-form :model="form" label-width="90px">
          <el-form-item label="主播名称">
            <el-input v-model="form.name" placeholder="如：小助手" style="max-width: 320px" />
          </el-form-item>
          <el-form-item label="性格特点">
            <el-input v-model="form.personality" type="textarea" :rows="2"
                      placeholder="如：热情友好、耐心细致、幽默风趣" />
          </el-form-item>
          <el-form-item label="说话风格">
            <el-input v-model="form.style" type="textarea" :rows="2"
                      placeholder="如：轻松活泼，喜欢用简短的句子" />
          </el-form-item>
          <el-form-item label="知识范围">
            <el-input v-model="form.knowledge_scope" type="textarea" :rows="2"
                      placeholder="如：日常闲聊、生活百科、娱乐八卦" />
          </el-form-item>
          <el-form-item label="禁止话题">
            <el-select v-model="form.forbidden_topics" multiple filterable allow-create
                       placeholder="输入后按回车添加" style="width: 100%" />
          </el-form-item>
          <el-form-item label=" ">
            <el-button type="primary" @click="handleSave" :loading="saving">保存配置</el-button>
            <el-button @click="handleReset">重置</el-button>
          </el-form-item>
          <el-divider content-position="left">System Prompt 预览</el-divider>
          <pre class="prompt-preview">{{ previewText }}</pre>
        </el-form>
      </el-tab-pane>

      <!-- ── ② 弹幕回复 ── -->
      <el-tab-pane label="弹幕回复" name="reply">
        <el-form :model="form" label-width="110px">
          <el-form-item label="回复方式">
            <div class="line">
              <span class="lbl">攒够</span>
              <el-input-number v-model="form.danmaku_batch_trigger" :min="1" :max="50" />
              <span class="lbl">条合并成一句（填 1 = 逐条回复）</span>
            </div>
            <div class="tip">当前：{{ form.danmaku_batch_trigger <= 1 ? '逐条回复（每条弹幕单独回一句）' : `攒 ${form.danmaku_batch_trigger} 条合并` }}</div>
          </el-form-item>
          <el-form-item label="兜底等待">
            <div class="line">
              <el-input-number v-model="form.danmaku_batch_wait" :min="0.5" :max="30" :step="0.5" />
              <span class="lbl">秒</span>
            </div>
            <div class="tip">聚合模式下不够上面条数时，等这么久就把已有的合并回一句（逐条模式用不到）</div>
          </el-form-item>
          <el-form-item label="一句话上限">
            <div class="line">
              <el-input-number v-model="form.danmaku_max_chars" :min="10" :max="200" />
              <span class="lbl">字</span>
            </div>
            <div class="tip">逐条模式也会生效，超长会硬截断（防止被诱导念一长串）</div>
          </el-form-item>
          <el-form-item label="回复策略">
            <el-input v-model="form.danmaku_policy" type="textarea" :rows="3"
                      placeholder="如：优先回答商品、价格、位置类问题；纯表情、刷屏、重复内容不回" />
          </el-form-item>
          <el-form-item label=" ">
            <el-button type="primary" @click="handleSave" :loading="saving">保存配置</el-button>
            <el-button @click="handleReset">重置</el-button>
          </el-form-item>
        </el-form>
      </el-tab-pane>

      <!-- ── ③ 弹幕安全 ── -->
      <el-tab-pane name="safe">
        <template #label>
          弹幕安全
          <el-tag v-if="safeOnCount" size="small" type="success" style="margin-left: 4px">{{ safeOnCount }} 项已开</el-tag>
        </template>
        <el-form :model="form" label-width="110px">
          <el-form-item label="屏蔽词">
            <el-input v-model="form.danmaku_block_words" type="textarea" :rows="3"
                      placeholder="一行一个（也支持逗号分隔），例如：1 / 加微信 / 刷单" />
            <div class="tip">命中的弹幕<b>不回复</b>，但仍显示在弹幕流里（标成"已屏蔽"）</div>
          </el-form-item>
          <el-form-item label="匹配方式">
            <el-radio-group v-model="form.danmaku_block_mode">
              <el-radio value="exact">整条相同才算（推荐）</el-radio>
              <el-radio value="contains">只要包含就屏蔽</el-radio>
            </el-radio-group>
            <div class="tip">选「整条相同」时，屏蔽词 <code>1</code> <b>不会误杀「扣1」</b>；想屏蔽违禁词再选「只要包含」</div>
          </el-form-item>
          <el-row :gutter="16">
            <el-col :span="12">
              <el-form-item label="刷屏噪音" label-width="90px">
                <el-switch v-model="form.danmaku_block_noise" :active-value="1" :inactive-value="0" />
              </el-form-item>
            </el-col>
            <el-col :span="12">
              <el-form-item label="防注入攻击" label-width="90px">
                <el-switch v-model="form.danmaku_inject_filter" :active-value="1" :inactive-value="0" />
              </el-form-item>
            </el-col>
          </el-row>
          <div class="tip" style="margin: -8px 0 14px 0">
            刷屏噪音 = 纯数字 / 纯符号 / 同一个字重复 ｜ 防注入 = 拦「system:」「忽略以上指令」「喵100次」这类（<b>建议一直开着</b>）
          </div>
          <el-row :gutter="16">
            <el-col :span="12">
              <el-form-item label="弹幕长度上限" label-width="110px">
                <el-input-number v-model="form.danmaku_max_len" :min="10" :max="500" />
              </el-form-item>
            </el-col>
            <el-col :span="12">
              <el-form-item label="刷屏限速" label-width="90px">
                <el-input-number v-model="form.danmaku_rate_limit" :min="0" :max="50" />
              </el-form-item>
            </el-col>
          </el-row>
          <div class="tip" style="margin: -8px 0 14px 0">
            长度超上限<b>不回</b>（单位：字） ｜ 限速 = 同一个人每 10 秒最多回几条（0 = 不限）
          </div>
          <el-form-item label="兜底话术">
            <el-input v-model="form.danmaku_fallback" placeholder="这个我就不接了啊，咱们还是聊产品。" />
            <div class="tip">AI 回复被判定异常时（超长、重复刷屏、出现系统提示词等）就念这句</div>
          </el-form-item>
          <el-form-item label=" ">
            <el-button type="primary" @click="handleSave" :loading="saving">保存配置</el-button>
            <el-button @click="handleReset">重置</el-button>
          </el-form-item>
        </el-form>
      </el-tab-pane>

      <!-- ── ④ 口播方式 ── -->
      <el-tab-pane name="voice">
        <template #label>
          口播方式
          <el-tag v-if="form.danmaku_call_name || form.danmaku_read_msg" size="small" type="warning"
                  style="margin-left: 4px">已开启</el-tag>
        </template>
        <el-form :model="form" label-width="110px">
          <el-row :gutter="16">
            <el-col :span="12">
              <el-form-item label="读观众名字" label-width="100px">
                <el-switch v-model="form.danmaku_call_name" :active-value="1" :inactive-value="0" />
              </el-form-item>
            </el-col>
            <el-col :span="12">
              <el-form-item label="转述观众的话" label-width="110px">
                <el-switch v-model="form.danmaku_read_msg" :active-value="1" :inactive-value="0" />
              </el-form-item>
            </el-col>
          </el-row>
          <div class="tip" style="margin: -8px 0 14px 0">
            两个都关 = 只念回复正文（和以前一样）。打开后，下面带 <code>{name}</code>/<code>{msg}</code> 的模板才会参与。
            <br>
            <b>「转述」不是照抄弹幕</b>：真主播不会把观众的话一字不差念出来，而是概括一下再说 ——
            观众说「主播你现在在哪里直播呀」，念出来是「<b>问我人现在在哪儿</b>」。
            这一步由 AI 现场完成（不许照抄，偷懒照抄的会被程序丢掉、自动换成不带复述的句式）。
          </div>
          <el-row :gutter="16">
            <el-col :span="12">
              <el-form-item label="昵称字数上限" label-width="110px">
                <el-input-number v-model="form.danmaku_name_max" :min="1" :max="20" />
              </el-form-item>
            </el-col>
            <el-col :span="12">
              <el-form-item label="转述字数上限" label-width="110px">
                <el-input-number v-model="form.danmaku_read_msg_max" :min="2" :max="100" />
              </el-form-item>
            </el-col>
          </el-row>
          <div class="tip" style="margin: -8px 0 14px 0">
            纯数字、像账号 ID 的昵称<b>不念</b>（自动换成不带称呼的句式）；转述超过字数上限也<b>不念</b>，照样回复
          </div>

          <el-divider content-position="left">回复模板</el-divider>
          <div class="tip" style="margin-bottom: 8px">
            占位符：<code>{reply}</code> = AI 回复正文 ｜ <code>{name}</code> = 观众昵称 ｜
            <code>{msg}</code> = <b>AI 对观众那句话的转述</b>（不是原话）。<br>
            <b>权重 = 相对比例</b>：权重 3 和权重 1 就是 3:1 的出现次数（不用凑总数）；
            <b>权重 0 = 这条不用</b>；<b>只留一条</b>就固定只用这一种念法。
          </div>
          <div class="tpl-list">
            <div v-for="(row, i) in tplRows" :key="i" class="tpl-row">
              <el-input v-model="row.text" size="small" placeholder="如：{name}宝子，{reply}" style="flex: 1" />
              <el-input-number v-model="row.weight" size="small" :min="0" :max="100" :step="1" style="width: 110px" />
              <el-button size="small" text type="danger" @click="removeTpl(i)">删除</el-button>
            </div>
            <div v-if="!tplRows.length" class="tip">
              现在用<b>内置默认模板</b>（13 条念法、权重都相等）。点「填入推荐模板」就能在默认基础上改。
            </div>
          </div>
          <div style="margin: 6px 0 12px">
            <el-button size="small" @click="addTpl">+ 添加一条</el-button>
            <el-button size="small" @click="usePresetTemplates">填入推荐模板</el-button>
            <el-button size="small" text @click="clearTpl">清空（用内置默认）</el-button>
          </div>

          <el-divider content-position="left">实时预览</el-divider>
          <div class="preview-box">
            <div class="line">
              <span class="lbl">观众昵称</span>
              <el-input v-model="demoName" size="small" style="width: 130px" />
              <span class="lbl">观众的弹幕</span>
              <el-input v-model="demoMsg" size="small" style="width: 180px" />
            </div>
            <div class="line" style="margin-top: 6px">
              <span class="lbl">AI 转述 <span style="color: #c0c4cc">（实际由 AI 现场概括，这里只是示例）</span></span>
              <el-input v-model="demoPara" size="small" style="width: 180px" />
            </div>
            <div style="margin-top: 10px">
              会说成：<b class="out">「{{ demoRendered }}」</b>
            </div>
            <div class="tpl-stat">
              <div v-for="(t, i) in templateStatus" :key="i" :style="{ color: t.ok ? '#67c23a' : '#c0c4cc' }">
                {{ t.ok ? '✓' : '✗' }} {{ t.text }}
                <b v-if="t.ok"> {{ t.pct }}%</b>
                <span v-if="!t.ok" style="color: #f56c6c"> —— {{ t.why }}</span>
              </div>
            </div>
          </div>

          <el-form-item label=" " style="margin-top: 16px">
            <el-button type="primary" @click="handleSave" :loading="saving">保存配置</el-button>
            <el-button @click="handleReset">重置</el-button>
          </el-form-item>
        </el-form>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { usePersonaStore } from '@/stores/persona'
import { updatePersona } from '@/api/persona'

const store = usePersonaStore()
const saving = ref(false)
const activeTab = ref('basic')

const DEMO_REPLY = '主播就在直播间'
const DEFAULT_TEMPLATE_LINES = [
  // ① 不带称呼
  '{reply}', '说到这个，{reply}', '来，我统一回一下，{reply}',
  '有人问{msg}，{reply}', '{msg}，{reply}',
  // ② 带称呼（后缀混用）
  '{name}，{reply}', '来，{name}，{reply}', '{name}宝子，{reply}',
  '{name}家人，{reply}', '{name}宝宝，{reply}', '{name}这个问题问得好，{reply}',
  // ③ 带称呼 + 复述原文
  '{name}问{msg}，{reply}', '刚才{name}问{msg}，{reply}',
]

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
  danmaku_block_words: '1',
  danmaku_block_mode: 'exact',
  danmaku_block_noise: 1,
  danmaku_inject_filter: 1,
  danmaku_max_len: 60,
  danmaku_rate_limit: 3,
  danmaku_fallback: '',
  danmaku_call_name: 0,
  danmaku_read_msg: 0,
  danmaku_name_max: 6,
  danmaku_read_msg_max: 24,
  danmaku_reply_templates: '',
})

const demoName = ref('小明')
const demoMsg = ref('主播你现在在哪里直播呀')
const demoPara = ref('问我人现在在哪儿')

/** 弹幕安全里开了几项（页签上的小标签） */
const safeOnCount = computed(() => {
  let n = 0
  if (form.danmaku_block_words.trim()) n++
  if (form.danmaku_block_noise) n++
  if (form.danmaku_inject_filter) n++
  if (form.danmaku_max_len) n++
  if (form.danmaku_rate_limit) n++
  return n
})

// ── 模板列表（模板 + 权重）；存储格式：一行一个 `模板<TAB>权重` ──
const tplRows = ref<{ text: string; weight: number }[]>([])

function parseTpl(raw: string): { text: string; weight: number }[] {
  return String(raw || '')
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter(Boolean)
    .map((line) => {
      let text = line
      let weight = 1
      if (line.includes('\t')) {
        const i = line.lastIndexOf('\t')
        text = line.slice(0, i).trim()
        const w = Number(line.slice(i + 1).trim())
        weight = Number.isFinite(w) ? w : 1
      } else {
        const m = line.match(/[\s]+[*x×#]\s*(\d+(?:\.\d+)?)\s*$/)
        if (m) {
          text = line.slice(0, m.index).trim()
          weight = Number(m[1])
        }
      }
      return { text, weight: Math.max(0, Math.min(100, weight)) }
    })
    .filter((r) => r.text)
}

function serializeTpl(rows: { text: string; weight: number }[]): string {
  return rows
    .filter((r) => r.text.trim())
    .map((r) => `${r.text.trim()}\t${r.weight}`)
    .join('\n')
}

function addTpl() {
  tplRows.value.push({ text: '{reply}', weight: 1 })
}
function removeTpl(i: number) {
  tplRows.value.splice(i, 1)
}
function clearTpl() {
  tplRows.value = []
}

// ── 预览用的清洗（与后端 services/danmaku_filter.py 同规则，只用于界面示意）──
const SPEAK_KEEP = /[^\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7afA-Za-z0-9]/g
function speakable(s: string): string {
  return String(s || '').replace(SPEAK_KEEP, '').trim()
}
function cleanName(s: string, max: number): string {
  const n = speakable(s)
  if (!n) return ''
  if (/^\d+$/.test(n)) return ''
  if ((n.match(/\d/g) || []).length >= 2) return ''
  return n.slice(0, max)
}
function cleanMsg(s: string, max: number): string {
  const m = speakable(s)
  if (!m || m.length > max) return ''
  return m
}

/** 转述清洗（与后端 clean_paraphrase 同规则）：照抄原话的一律不要 */
function cleanPara(para: string, original: string, max: number): string {
  const p = speakable(para).replace(/^[，。！？、,.!?;；:：'"“”‘’(]+/, '').replace(/[，。！？、,.!?;；:：'"“”‘’)]+$/, '').trim()
  if (!p || p.length > max) return ''
  const np = p.replace(/\W+/g, '')
  const no = speakable(original).replace(/\W+/g, '')
  if (no && np) {
    if (np === no) return ''
    if (np.length >= 6 && (np.includes(no) || no.includes(np)) && Math.min(np.length, no.length) >= 0.8 * Math.max(np.length, no.length)) return ''
  }
  return p
}

const usableTemplates = computed(() => {
  if (tplRows.value.length) {
    return tplRows.value.filter((r) => r.text.trim()).map((r) => ({ text: r.text.trim(), weight: r.weight }))
  }
  return DEFAULT_TEMPLATE_LINES.map((t) => ({ text: t, weight: 1 }))
})

function renderTpl(tpl: string, name: string, msg: string, reply: string): string {
  let m = msg
  // 与后端 render_reply 同规则：模板里 {msg} 前面已经有「问」时，去掉转述开头的「问」，
  // 否则会念成「小明问问我人在哪儿」
  const idx = tpl.indexOf('{msg}')
  if (m && idx > 0 && tpl.slice(0, idx).trimEnd().endsWith('问')) {
    const stripped = m.replace(/^\s*问(一下|问|了|的)?\s*/, '')
    m = stripped || m
  }
  return tpl
    .replace(/\{name\}/g, name)
    .replace(/\{msg\}/g, m)
    .replace(/\{reply\}/g, reply)
    .replace(/^[\s，,、。:：;；]+/, '')
    .replace(/[，,]{2,}/g, '，')
    .trim()
}

/** 每条模板当前能不能用 + 实际占比（权重 / 可用权重之和） */
const templateStatus = computed(() => {
  const name = form.danmaku_call_name ? cleanName(demoName.value, form.danmaku_name_max) : ''
  const msg = form.danmaku_read_msg ? cleanPara(demoPara.value, demoMsg.value, form.danmaku_read_msg_max) : ''
  const judged = usableTemplates.value.map((r) => {
    const t = r.text
    if (!t.includes('{reply}')) return { text: t, weight: r.weight, ok: false, why: '缺 {reply} 占位符' }
    if (t.includes('{name}') && !form.danmaku_call_name) return { text: t, weight: r.weight, ok: false, why: '「读观众名字」关着' }
    if (t.includes('{name}') && !name) return { text: t, weight: r.weight, ok: false, why: '这个昵称念不出来' }
    if (t.includes('{msg}') && !form.danmaku_read_msg) return { text: t, weight: r.weight, ok: false, why: '「念弹幕原文」关着' }
    if (t.includes('{msg}') && !msg) return { text: t, weight: r.weight, ok: false, why: '转述为空/太长/照抄了原话' }
    if (r.weight <= 0) return { text: t, weight: r.weight, ok: false, why: '权重为 0（这条不用）' }
    return { text: t, weight: r.weight, ok: true, why: '' }
  })
  const total = judged.filter((x) => x.ok).reduce((n, x) => n + x.weight, 0)
  return judged.map((x) => ({ ...x, pct: total > 0 && x.ok ? Math.round((x.weight / total) * 100) : 0 }))
})

/** 预览：用权重最高的那条渲染（运行时按权重随机挑） */
const demoRendered = computed(() => {
  const name = form.danmaku_call_name ? cleanName(demoName.value, form.danmaku_name_max) : ''
  const msg = form.danmaku_read_msg ? cleanPara(demoPara.value, demoMsg.value, form.danmaku_read_msg_max) : ''
  const ok = templateStatus.value.filter((t) => t.ok)
  if (!ok.length) return DEMO_REPLY
  const top = ok.reduce((a, b) => (b.weight > a.weight ? b : a), ok[0])
  return renderTpl(top.text, name, msg, DEMO_REPLY)
})

function usePresetTemplates() {
  tplRows.value = DEFAULT_TEMPLATE_LINES.map((t) => ({ text: t, weight: 1 }))
  ElMessage.success('已填入推荐模板（13 条，权重可单独调）')
}

const previewText = computed(() => {
  const parts = [
    `你是${form.name || '小助手'}，一位正在直播的主播。`,
    `性格特点：${form.personality || '...'}`,
    `说话风格：${form.style || '...'}`,
    `知识范围：${form.knowledge_scope || '...'}`,
  ]
  if (form.forbidden_topics.length > 0) parts.push(`禁止谈论：${form.forbidden_topics.join('、')}`)
  if (form.danmaku_policy) parts.push(`弹幕回复策略：${form.danmaku_policy}`)
  parts.push(
    form.danmaku_batch_trigger <= 1
      ? '弹幕逐条回复：每条弹幕单独回一句'
      : `每攒够 ${form.danmaku_batch_trigger} 条弹幕（或等满 ${form.danmaku_batch_wait} 秒）合并成一句（≤${form.danmaku_max_chars}字）播报`
  )
  const safe: string[] = []
  if (form.danmaku_inject_filter) safe.push('防注入')
  if (form.danmaku_block_noise) safe.push('刷屏噪音')
  if (form.danmaku_block_words.trim()) {
    safe.push(`屏蔽词「${form.danmaku_block_words.trim().split(/\r?\n/).join('/')}」`)
  }
  if (safe.length) parts.push(`弹幕安全（命中不回）：${safe.join('、')}`)
  if (form.danmaku_call_name) parts.push(`回复时称呼观众昵称（最多 ${form.danmaku_name_max} 字）`)
  if (form.danmaku_read_msg) parts.push(`回复时会转述观众的话（AI 概括，最多 ${form.danmaku_read_msg_max} 字）`)
  return parts.join('\n')
})

function fill(p: any) {
  form.name = p.name
  form.personality = p.personality
  form.style = p.style
  form.knowledge_scope = p.knowledge_scope
  form.forbidden_topics = [...(p.forbidden_topics || [])]
  form.danmaku_policy = p.danmaku_policy || ''
  form.danmaku_batch_trigger = p.danmaku_batch_trigger || 3
  form.danmaku_batch_wait = p.danmaku_batch_wait || 3
  form.danmaku_max_chars = p.danmaku_max_chars || 60
  form.danmaku_block_words = p.danmaku_block_words ?? '1'
  form.danmaku_block_mode = p.danmaku_block_mode || 'exact'
  form.danmaku_block_noise = p.danmaku_block_noise ?? 1
  form.danmaku_inject_filter = p.danmaku_inject_filter ?? 1
  form.danmaku_max_len = p.danmaku_max_len || 60
  form.danmaku_rate_limit = p.danmaku_rate_limit ?? 3
  form.danmaku_fallback = p.danmaku_fallback || ''
  form.danmaku_call_name = p.danmaku_call_name ?? 0
  form.danmaku_read_msg = p.danmaku_read_msg ?? 0
  form.danmaku_name_max = p.danmaku_name_max || 6
  form.danmaku_read_msg_max = p.danmaku_read_msg_max || 24
  form.danmaku_reply_templates = p.danmaku_reply_templates || ''
  tplRows.value = parseTpl(form.danmaku_reply_templates)
}

onMounted(async () => {
  await store.fetch()
  fill(store.persona)
})

async function handleSave() {
  saving.value = true
  try {
    form.danmaku_reply_templates = serializeTpl(tplRows.value)
    await updatePersona({ ...form })
    ElMessage.success('人设已保存')
  } finally {
    saving.value = false
  }
}

async function handleReset() {
  await store.fetch()
  fill(store.persona)
}
</script>

<style scoped>
.line {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.lbl {
  font-size: 13px;
  color: #606266;
}
.tip {
  font-size: 12px;
  color: #909399;
  line-height: 1.7;
  margin-top: 4px;
}
.prompt-preview {
  background: #f5f7fa;
  padding: 12px;
  border-radius: 4px;
  white-space: pre-wrap;
  font-size: 12px;
  margin: 0;
}
.tpl-list {
  max-height: 300px;
  overflow-y: auto;
  padding-right: 4px;
}
.tpl-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
}
.preview-box {
  background: #f5f7fa;
  border-radius: 6px;
  padding: 12px 14px;
  font-size: 13px;
}
.out {
  color: #e6a23c;
}
.tpl-stat {
  margin-top: 10px;
  font-size: 12px;
  line-height: 1.9;
  max-height: 190px;
  overflow-y: auto;
}
</style>
