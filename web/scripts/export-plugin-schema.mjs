import { mkdir, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { z } from 'zod'
import { recipeSchema, exampleRecipe } from '../src/plugins/catalog.ts'

const root = fileURLToPath(new URL('../../contracts/plugins/v1/', import.meta.url))
const schema = z.toJSONSchema(recipeSchema)
schema.$id = 'https://mia.local/contracts/plugins/v1/recipe.schema.json'
schema.title = 'MIA text recipe v1'
// Zod refinements are validated at runtime; express the placeholder in JSON Schema too.
schema.properties.prompt.pattern = '\\{\\{input\\}\\}'
await mkdir(`${root}/examples`, { recursive: true })
await writeFile(`${root}/recipe.schema.json`, `${JSON.stringify(schema, null, 2)}\n`)
await writeFile(`${root}/examples/day-planner.json`, `${JSON.stringify(exampleRecipe, null, 2)}\n`)

const { applicationSchema, exampleApplication } = await import('../src/plugins/catalog.ts')
const rootV2 = fileURLToPath(new URL('../../contracts/plugins/v2/', import.meta.url))
const schemaV2 = z.toJSONSchema(applicationSchema)
schemaV2.$id = 'https://mia.local/contracts/plugins/v2/application.schema.json'
schemaV2.title = 'MIA application v2 — skill, actions, view, downloads'
await mkdir(`${rootV2}/examples`, { recursive: true })
await writeFile(`${rootV2}/application.schema.json`, `${JSON.stringify(schemaV2, null, 2)}\n`)
await writeFile(
  `${rootV2}/examples/focus-board.json`,
  `${JSON.stringify(exampleApplication, null, 2)}\n`,
)
