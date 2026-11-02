/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly DEV: boolean
  readonly PROD: boolean
  readonly MODE: string
  readonly BASE_URL: string
  /** Exactly 'true' enables the reviewed run-control page; unset means absent. */
  readonly VITE_REVIEWED_RUNTIME_ENABLED?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
