/**
 * 话术分割（与后端 server/livestream/services/script_manager.py:split_script_text 保持一致）
 *
 * · 先按换行切：AI 一次生成多条话术时是用换行拼起来的
 * · 再按分割符切；分隔符保留在句尾（"你好。" 这样 TTS 停顿更自然）
 * · 分隔符为空 → 只按换行切
 * · 切不出多句 → 返回单元素数组（整条当一句）
 */
const SPLIT_MIN_LEN = 2

export function splitScriptText(text: string, sep = ''): string[] {
  const raw = String(text || '').trim()
  if (!raw) return []
  const chunks = raw
    .split(/[\r\n]+/)
    .map((c) => c.trim())
    .filter(Boolean)
  const s = String(sep || '').trim()
  if (!s) return chunks.length ? chunks : [raw]

  const out: string[] = []
  for (const ch of chunks) {
    const parts = ch.split(s)
    parts.forEach((p, i) => {
      let piece = p.trim()
      if (!piece) return
      if (i < parts.length - 1) piece += s
      if (piece.length >= SPLIT_MIN_LEN) out.push(piece)
    })
  }
  return out.length ? out : [raw]
}
