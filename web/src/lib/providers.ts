import type { ModelProvider } from './api'

export type ProviderInfo = {
  id: ModelProvider
  name: string
  model: string
  local?: boolean
  url?: string
}

export const providers: ProviderInfo[] = [
  { id: 'openai', name: 'OpenAI', model: 'gpt-4.1-mini' },
  { id: 'anthropic', name: 'Anthropic', model: 'claude-sonnet-4-5' },
  { id: 'gemini', name: 'Google Gemini', model: 'gemini-2.5-flash' },
  { id: 'deepinfra', name: 'DeepInfra', model: 'zai-org/GLM-5.3-Flash' },
  { id: 'groq', name: 'Groq', model: 'llama-3.3-70b-versatile' },
  { id: 'mistral', name: 'Mistral', model: 'mistral-small-latest' },
  { id: 'ollama', name: 'Ollama', model: 'llama3.2', local: true, url: 'http://127.0.0.1:11434' },
  {
    id: 'local',
    name: 'LM Studio / свой сервер',
    model: 'local-model',
    local: true,
    url: 'http://127.0.0.1:1234/v1',
  },
]
