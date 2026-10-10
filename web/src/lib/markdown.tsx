import type { ReactNode } from 'react'

// A small, safe Markdown subset for assistant output: paragraphs, headings, lists, GFM tables,
// code, bold, italic, inline code and https links. Nothing is injected as HTML.

const separatorRow = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/

/** Cells of one table row: outer pipes dropped, `\|` kept inside a cell. */
function cellsOf(line: string): string[] {
  const trimmed = line
    .trim()
    .replace(/^\|/, '')
    .replace(/(?<!\\)\|$/, '')
  return trimmed.split(/(?<!\\)\|/).map((cell) => cell.trim().replace(/\\\|/g, '|'))
}

/** A table starts at a header row with pipes followed by a `---|---` separator row. */
function startsTable(lines: string[], at: number): boolean {
  const next = lines[at + 1]
  return (
    next !== undefined &&
    lines[at].includes('|') &&
    next.includes('|') &&
    separatorRow.test(next) &&
    cellsOf(lines[at]).length === cellsOf(next).length
  )
}

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
    if (startsTable(lines, index)) {
      const head = cellsOf(line)
      const align = cellsOf(lines[index + 1]).map((cell) =>
        cell.startsWith(':') && cell.endsWith(':')
          ? 'center'
          : cell.endsWith(':')
            ? 'right'
            : undefined,
      )
      index += 2
      const rows: string[][] = []
      while (index < lines.length && lines[index].trim() && lines[index].includes('|')) {
        const cells = cellsOf(lines[index++])
        rows.push(head.map((_, n) => cells[n] ?? ''))
      }
      const tid = id++
      blocks.push(
        <div key={tid} className="mw-md-table" role="region" aria-label="Таблица" tabIndex={0}>
          <table>
            <thead>
              <tr>
                {head.map((cell, n) => (
                  <th key={n} scope="col" style={{ textAlign: align[n] }}>
                    {renderInline(cell, `th${tid}-${n}`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, r) => (
                <tr key={r}>
                  {row.map((cell, n) => (
                    <td key={n} style={{ textAlign: align[n] }}>
                      {renderInline(cell, `td${tid}-${r}-${n}`)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
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
      !numbered.test(lines[index]) &&
      !startsTable(lines, index)
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
    .replace(/^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/gm, '')
    .replace(/\s*\|\s*/g, ', ')
    .replace(/^, |, $/gm, '')
    .replace(/^\s{0,3}#{1,6}\s*/gm, '')
    .replace(/^\s*[-*•]\s+/gm, '')
    .replace(/[*_`#>|]/g, '')
    .replace(/\n{2,}/g, '\n')
    .trim()
}
