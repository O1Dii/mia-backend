import { useEffect, useState, type AnchorHTMLAttributes, type MouseEvent } from 'react'

// A tiny history router: the OAuth return (/store/connected) needs real paths, not hashes.
const base = import.meta.env.BASE_URL.replace(/\/$/, '')

function currentPath(): string {
  const path = location.pathname
  return (base && path.startsWith(base) ? path.slice(base.length) : path) || '/'
}

export type Place = { path: string; search: URLSearchParams }

function read(): Place {
  return { path: currentPath(), search: new URLSearchParams(location.search) }
}

export function navigate(to: string, replace = false) {
  const url = base + to
  if (replace) history.replaceState(null, '', url)
  else history.pushState(null, '', url)
  window.dispatchEvent(new Event('mia:navigate'))
  window.scrollTo(0, 0)
}

export function usePlace(): Place {
  const [place, setPlace] = useState(read)
  useEffect(() => {
    const update = () => setPlace(read())
    window.addEventListener('popstate', update)
    window.addEventListener('mia:navigate', update)
    return () => {
      window.removeEventListener('popstate', update)
      window.removeEventListener('mia:navigate', update)
    }
  }, [])
  return place
}

export function Link({
  to,
  onClick,
  ...rest
}: { to: string } & Omit<AnchorHTMLAttributes<HTMLAnchorElement>, 'href'>) {
  return (
    <a
      {...rest}
      href={base + to}
      onClick={(event: MouseEvent<HTMLAnchorElement>) => {
        onClick?.(event)
        if (
          event.defaultPrevented ||
          event.button !== 0 ||
          event.metaKey ||
          event.ctrlKey ||
          event.shiftKey ||
          rest.target === '_blank'
        )
          return
        event.preventDefault()
        navigate(to)
      }}
    />
  )
}
