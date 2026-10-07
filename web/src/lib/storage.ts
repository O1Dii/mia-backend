import { useCallback, useEffect, useState } from 'react'
import { z } from 'zod'
import { toast } from 'sonner'

// Only non-secret plugin data is written here. Provider keys never enter this module.
export function useStored<T>(
  key: string,
  schema: z.ZodType<T>,
  initial: T,
): [T, (next: T | ((old: T) => T)) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key)
      return raw === null ? initial : schema.parse(JSON.parse(raw))
    } catch {
      return initial
    }
  })
  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify(value))
    } catch {
      toast.error('Не удалось сохранить данные в браузере. Проверьте свободное место.')
    }
  }, [key, value])
  const update = useCallback((next: T | ((old: T) => T)) => setValue(next), [])
  return [value, update]
}
