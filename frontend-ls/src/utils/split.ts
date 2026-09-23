/**
 * 话术分割（与后端 server/livestream/services/script_manager.py:split_script_text 保持一致）
 *
 * · 分割符留空 → **完全不分割**：整条当一句念（换行也不切）
 * · 填了分割符 → 先按换行拆成行，再按分割符切；分隔符保留在句尾
 * · 切不出多句 → 返回单元素数组（整条当一句）
 */
const SPLIT_MIN_LEN = 2

export function splitScriptText(text: string, sep = ''): string[] {
  const raw = String(text || '').trim()
  if (!raw) return []
  const s = String(sep || '').trim()
  if (!s) return [raw]            // 留空 = 不分割（想切就自己填分割符）
  const chunks = raw
    .split(/[\r\n]+/)
    .map((c) => c.trim())
    .filter(Boolean)

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
