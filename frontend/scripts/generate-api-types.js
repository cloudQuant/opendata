// Auto-generate TypeScript API types from OpenAPI spec
// Cross-platform script that works with both Unix and Windows

const { spawnSync } = require('child_process')
const path = require('path')

// Resolve to frontend directory (where package.json lives)
const frontendDir = path.resolve(__dirname, '..')

// URL base can be overridden by OPENAPI_BASE_URL; defaults to http://localhost:8000
const baseUrl = process.env.OPENAPI_BASE_URL || 'http://localhost:8000'
const openapiUrl = baseUrl.endsWith('/') ? baseUrl + 'openapi.json' : baseUrl + '/openapi.json'

console.log(`Generating TypeScript types from OpenAPI: ${openapiUrl}`)

// Run openapi-typescript with the output file
const result = spawnSync('npx', [
  'openapi-typescript',
  openapiUrl,
  '-o',
  path.resolve(frontendDir, 'src', 'types', 'api.d.ts')
], {
  shell: true,
  stdio: 'inherit',
  cwd: process.cwd() || process.env.SHELL?.split(':').pop() || 'cmd.exe',
})

if (result.status !== 0) {
  console.error('Failed to generate API types:')
  console.error(result.stderr?.toString())
  process.exit(1)
}

console.log('Successfully generated API types to frontend/src/types/api.d.ts')
