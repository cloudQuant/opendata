<script setup lang="ts">
import { RouterView } from 'vue-router'
import { logger } from '@/utils/logger'
import ErrorBoundary from '@/components/common/ErrorBoundary.vue'

function handleGlobalError(error: Error, instance: unknown, info: string) {
  logger.error('Global error caught', { error: error.message, info, stack: error.stack })
}
</script>

<template>
  <ErrorBoundary @error="handleGlobalError">
    <RouterView />
  </ErrorBoundary>
</template>

<style>
* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

html,
body,
#app {
  height: 100%;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial,
    'Noto Sans', sans-serif, 'Apple Color Emoji', 'Segoe UI Emoji', 'Segoe UI Symbol',
    'Noto Color Emoji';
}
</style>
