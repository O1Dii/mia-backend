import { z } from 'zod'

export const recipeSchema = z.strictObject({
  schemaVersion: z.literal('1.0'),
  id: z.string().regex(/^community\.[a-z0-9-]{2,48}$/),
  version: z.string().regex(/^\d+\.\d+\.\d+$/),
  name: z.string().min(2).max(60),
  description: z.string().min(10).max(240),
  author: z.string().min(1).max(60),
  runtime: z.literal('prompt'),
  permissions: z.tuple([z.literal('ai.generate')]),
  prompt: z
    .string()
    .min(10)
    .max(8000)
    .refine((s) => s.includes('{{input}}'), 'Добавьте {{input}} в prompt'),
})
export type Recipe = z.infer<typeof recipeSchema>
export type Plugin = {
  id: string
  name: string
  description: string
  category: 'Повседневное' | 'Продуктивность' | 'AI'
  color: string
  art: string
  tagline: string
  permissions: string[]
  ai?: boolean
  recipe?: Recipe
  manifest?: ApplicationManifest
}

export const catalog: Plugin[] = [
  {
    id: 'mia.digest',
    name: 'Коротко о главном',
    description:
      'Свежие новости по вашим темам. Интернет находит источники, ваша модель собирает главное.',
    category: 'AI',
    color: 'violet',
    art: 'digest',
    tagline: 'Меньше шума. Больше смысла.',
    permissions: ['ai.generate', 'network.public', 'state.read', 'state.write'],
    ai: true,
  },
  {
    id: 'mia.timer',
    name: 'Таймер',
    description:
      'Для пасты аль денте, короткого перерыва или маленького эксперимента. Время под контролем.',
    category: 'Повседневное',
    color: 'mint',
    art: 'timer',
    tagline: 'У всего есть своё время.',
    permissions: ['browser.audio', 'state.read', 'state.write'],
  },
  {
    id: 'mia.clock',
    name: 'Мировое время',
    description: 'Минск, Лондон или Токио. Держите близких и свою команду в одном часовом поясе.',
    category: 'Повседневное',
    color: 'blue',
    art: 'clock',
    tagline: 'Ближе, чем кажется.',
    permissions: ['state.read', 'state.write'],
  },
  {
    id: 'mia.notes',
    name: 'Мысли вслух',
    description:
      'Поймайте идею, запишите список дел и освободите место в голове. Всё остаётся у вас.',
    category: 'Продуктивность',
    color: 'peach',
    art: 'notes',
    tagline: 'Хорошие идеи не ждут.',
    permissions: ['state.read', 'state.write'],
  },
  {
    id: 'mia.focus',
    name: 'В потоке',
    description: '25 минут на важное. 5 минут на себя. Создайте пространство для глубокой работы.',
    category: 'Продуктивность',
    color: 'yellow',
    art: 'focus',
    tagline: 'Одна задача. Весь фокус.',
    permissions: ['browser.audio', 'state.read', 'state.write'],
  },
  {
    id: 'mia.alarm',
    name: 'Мягкий старт',
    description: 'Будильник для вашего ритма. Напомнит о встрече, прогулке или начале нового дня.',
    category: 'Повседневное',
    color: 'pink',
    art: 'alarm',
    tagline: 'Начните в своём ритме.',
    permissions: ['browser.audio', 'state.read', 'state.write'],
  },
  {
    id: 'mia.web',
    name: 'Интернет',
    description:
      'Научите свою модель искать в интернете, находить свежие новости и читать публичные страницы.',
    category: 'AI',
    color: 'blue',
    art: 'clock',
    tagline: 'Новые знания для любой модели.',
    permissions: ['network.public', 'state.read'],
    ai: true,
  },
]

export const permissions: Record<string, string> = {
  'ai.generate': 'Отправка введённого текста выбранной AI-модели',
  'browser.audio': 'Звуковой сигнал в открытой вкладке',
  'storage.local': 'Сохранение данных плагина в этом браузере',
  'state.read': 'Чтение состояния этого плагина',
  'state.write': 'Изменение состояния через чат, виджет и подключённое приложение',
  'network.public': 'Поиск новостей и чтение публичных HTTPS-сайтов через плагин «Интернет»',
}

const fieldSchema = z.strictObject({
  type: z.enum(['string', 'integer', 'boolean']),
  description: z.string().max(300).default(''),
  maxLength: z.number().int().min(1).max(16000).default(1000),
  minimum: z.number().int().min(-1000000).default(0),
  maximum: z.number().int().max(1000000).default(1000000),
})
export const applicationSchema = z.strictObject({
  schemaVersion: z.literal('2.0'),
  id: z.string().regex(/^community\.[a-z0-9-]{2,48}$/),
  version: z.string().regex(/^\d+\.\d+\.\d+$/),
  name: z.string().min(2).max(60),
  description: z.string().min(10).max(240),
  author: z.string().min(1).max(60),
  skill: z.string().min(20).max(6000),
  permissions: z.array(z.enum(['state.read', 'state.write', 'ai.generate'])).max(3),
  actions: z
    .array(
      z.strictObject({
        name: z.string().regex(/^[a-z][a-z0-9_]{0,23}$/),
        description: z.string().min(5).max(300),
        inputs: z.record(z.string().regex(/^[a-z][a-z0-9_]{0,31}$/), fieldSchema),
        effect: z.enum(['state.patch', 'model.prompt']),
        prompt: z.string().max(8000).nullable().optional(),
      }),
    )
    .min(1)
    .max(10),
  view: z.strictObject({
    type: z.enum(['fields', 'html']).default('fields'),
    fields: z.array(z.string()).max(10).default([]),
    html: z.string().max(24000).default(''),
  }),
  downloads: z
    .array(
      z.strictObject({
        platform: z.enum(['web', 'android', 'ios', 'desktop']),
        url: z
          .string()
          .regex(/^https:\/\/[^\s]+$/)
          .max(2048),
        label: z.string().min(1).max(60),
      }),
    )
    .max(4)
    .default([]),
})
export type ApplicationManifest = z.infer<typeof applicationSchema>
export const exampleApplication: ApplicationManifest = applicationSchema.parse({
  schemaVersion: '2.0',
  id: 'community.focus-board',
  version: '1.0.0',
  name: 'Мой фокус',
  description:
    'Ваше приложение и виджет главной задачи. Попросите Мию сменить фокус — плитка обновится.',
  author: 'Вы',
  skill:
    'Когда пользователь просит сменить главный фокус или задачу в приложении «Мой фокус», вызови set_focus с task. Результат автоматически появится в виджете.',
  permissions: ['state.read', 'state.write', 'ai.generate'],
  actions: [
    {
      name: 'set_focus',
      description: 'Set the main task in the focus board widget',
      inputs: { task: { type: 'string', maxLength: 240 } },
      effect: 'state.patch',
    },
  ],
  view: { type: 'fields', fields: ['task'] },
  downloads: [],
})

export function applicationPlugin(manifest: ApplicationManifest): Plugin {
  return {
    id: manifest.id,
    name: manifest.name,
    description: manifest.description,
    category: 'AI',
    color: 'violet',
    art: 'recipe',
    tagline: `От ${manifest.author} · skill + view`,
    permissions: manifest.permissions,
    ai: true,
    manifest,
  }
}

export function recipeManifest(recipe: Recipe): ApplicationManifest {
  return applicationSchema.parse({
    schemaVersion: '2.0',
    id: recipe.id,
    version: recipe.version,
    name: recipe.name,
    description: recipe.description,
    author: recipe.author,
    skill: `Для задачи «${recipe.name}» используй run с исходным текстом input. ${recipe.description}`,
    permissions: ['state.read', 'ai.generate'],
    actions: [
      {
        name: 'run',
        description: recipe.description,
        inputs: { input: { type: 'string', maxLength: 16000 } },
        effect: 'model.prompt',
        prompt: recipe.prompt,
      },
    ],
    view: { type: 'fields', fields: ['answer'] },
    downloads: [],
  })
}

export const exampleRecipe: Recipe = {
  schemaVersion: '1.0',
  id: 'community.day-planner',
  version: '1.0.0',
  name: 'План моего дня',
  description:
    'Превратите список задач в реалистичный план дня с перерывами и понятными приоритетами.',
  author: 'Вы',
  runtime: 'prompt',
  permissions: ['ai.generate'],
  prompt:
    'Составь реалистичный план дня по этим задачам. Уточни приоритеты, предложи перерывы. Не выдумывай встречи и не выполняй действия. Ответь по-русски. Задачи:\n{{input}}',
}

export function recipePlugin(recipe: Recipe): Plugin {
  return {
    id: recipe.id,
    name: recipe.name,
    description: recipe.description,
    category: 'AI',
    color: 'violet',
    art: 'recipe',
    tagline: `От ${recipe.author}`,
    permissions: recipe.permissions,
    ai: true,
    recipe,
  }
}
