import type { ReactNode } from 'react'

// A small, safe Markdown subset for assistant output: paragraphs, headings, lists, code, bold,
// italic, inline code and https links. Nothing is injected as HTML.

const inline = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*|\[[^\]]+\]\(https:\/\/[^)\s]+\))/g

function renderInline(text: string, prefix: string): ReactNode[] {
  return text.split(inline).map((part, index) => {
    const key = `${prefix}-${index}`
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4)
      return <strong key={key}>{part.slice(2, -2)}</strong>
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2)
      return <code key={key}>{part.slice(1, -1)}</code>
    if (part.startsWith('*') && part.endsWith('*') && part.length > 2)
      return <em key={key}>{part.slice(1, -1)}</em>
    const link = /^\[([^\]]+)\]\((https:\/\/[^)\s]+)\)$/.exec(part)
    if (link)
      return (
        <a key={key} href={link[2]} target="_blank" rel="noopener noreferrer">
          {link[1]}
        </a>
      )
    return part
  })
}

export function Markdown({ text }: { text: string }) {
  const blocks: ReactNode[] = []
  const lines = text.replace(/\r\n/g, '\n').split('\n')
  let index = 0
  let id = 0
  while (index < lines.length) {
    const line = lines[index]
    if (!line.trim()) {
      index++
      continue
    }
    if (line.trim().startsWith('```')) {
      const code: string[] = []
      index++
      while (index < lines.length && !lines[index].trim().startsWith('```'))
        code.push(lines[index++])
      index++
      blocks.push(
        <pre key={id++}>
          <code>{code.join('\n')}</code>
        </pre>,
      )
      continue
    }
    const heading = /^#{1,6}\s+(.*)$/.exec(line)
    if (heading) {
      blocks.push(
        <p key={id++} className="mw-md-heading">
          {renderInline(heading[1], `h${id}`)}
        </p>,
      )
      index++
      continue
    }
    const bullet = /^\s*[-*•]\s+/
    const numbered = /^\s*\d{1,3}[.)]\s+/
    if (bullet.test(line) || numbered.test(line)) {
      const ordered = numbered.test(line)
      const pattern = ordered ? numbered : bullet
      const items: string[] = []
      while (index < lines.length && pattern.test(lines[index]))
        items.push(lines[index++].replace(pattern, ''))
      const rendered = items.map((item, n) => <li key={n}>{renderInline(item, `l${id}-${n}`)}</li>)
      blocks.push(ordered ? <ol key={id++}>{rendered}</ol> : <ul key={id++}>{rendered}</ul>)
      continue
    }
    const paragraph: string[] = []
    while (
      index < lines.length &&
      lines[index].trim() &&
      !lines[index].trim().startsWith('```') &&
      !/^#{1,6}\s/.test(lines[index]) &&
      !bullet.test(lines[index]) &&
      !numbered.test(lines[index])
    )
      paragraph.push(lines[index++])
    blocks.push(<p key={id++}>{renderInline(paragraph.join('\n'), `p${id}`)}</p>)
  }
  return <div className="mw-md">{blocks}</div>
}

/** Markdown to text a speech synthesizer can read: no marks, links keep their label. */
export function plainText(markdown: string): string {
  return markdown
    .replace(/```[\s\S]*?```/g, ' Фрагмент кода показан на экране. ')
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/^\s{0,3}#{1,6}\s*/gm, '')
    .replace(/^\s*[-*•]\s+/gm, '')
    .replace(/[*_`#>|]/g, '')
    .replace(/\n{2,}/g, '\n')
    .trim()
}
