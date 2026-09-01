import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { assertSafePortalAuthEnv } from './scripts/assert-safe-auth-env'

export default defineConfig(() => {
  assertSafePortalAuthEnv(process.env)
  return {
    base: '/',
    plugins: [react()],
  }
})
