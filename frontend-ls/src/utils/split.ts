/**
 * 话术分割（**必须与后端 server/livestream/services/script_manager.py:split_script_text 一致**）
 *
 * · 分割符留空 → **完全不分割**：整条当一句念（换行也不切）
 * · 填了分割符 → 先按换行拆成行，再按分割符切；分隔符保留在句尾
 * · 半角/全角的坑自动兜底：填 "." 而文案里写的是「。」也能切
 * · 只丢「纯分隔符」的碎片，有实际字就保留（哪怕只有 1 个字）
 * · 切不出多句 → 返回单元素数组（整条当一句）
 *
 * 这里算出来的句数就是列表里显示的「分句「。」N 句」，改这边记得同步改后端。
 */

/** 半角/全角的等价分隔符（只有在「原样一个都切不出来」时才用它兜底） */
const SEP_EQUIV: Record<string, string> = {
  '.': '。', ',': '，', '!': '！', '?': '？', ';': '；', ':': '：',
  '。': '.', '，': ',', '！': '!', '？': '?', '；': ';', '：': ':',
}

/** 优先用用户填的；一个都切不出来才用它对应的全角/半角写法 */
function resolveSeps(text: string, sep: string): string[] {
  if (text.includes(sep)) return [sep]
  const alt = SEP_EQUIV[sep]
  if (alt && text.includes(alt)) return [alt]
  return [sep]
}

const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

/** 只由分隔符/空白组成 → 丢掉；有实际字就保留（哪怕只有 1 个字） */
function hasContent(piece: string, seps: string[]): boolean {
  let core = piece
  for (const s of seps) core = core.split(s).join('')
  return !!core.trim()
}

/** 按 seps 里任意一个切 chunk，分隔符保留在句尾 */
function splitKeep(chunk: string, seps: string[]): string[] {
  const usable = seps.filter(Boolean).sort((a, b) => b.length - a.length)
  if (!usable.length) return chunk.trim() ? [chunk.trim()] : []
  const pat = new RegExp('(' + usable.map(escapeRe).join('|') + ')')
  const toks = chunk.split(pat)
  const out: string[] = []
  for (let i = 0; i < toks.length; i += 2) {
    let piece = (toks[i] ?? '').trim()
    if (i + 1 < toks.length) piece += toks[i + 1] // 句尾补回分隔符
    if (piece && hasContent(piece, usable)) out.push(piece)
  }
  return out
}

export function splitScriptText(text: string, sep = ''): string[] {
  const raw = String(text || '').trim()
  if (!raw) return []
  const s = String(sep || '').trim()
  if (!s) return [raw] // 留空 = 不分割（想切就自己填分隔符）

  const seps = resolveSeps(raw, s)
  const chunks = raw
    .split(/[\r\n]+/)
    .map((c) => c.trim())
    .filter(Boolean)

  const out: string[] = []
  for (const ch of chunks) out.push(...splitKeep(ch, seps))
  return out.length ? out : [raw]
}
