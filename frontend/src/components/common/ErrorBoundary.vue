<script setup lang="ts">
import { ref, onErrorCaptured, type Component } from 'vue'

export interface ErrorBoundaryProps {
  /** Custom error title displayed in fallback UI */
  errorTitle?: string
  /** Custom error description displayed in fallback UI */
  errorText?: string
  /** Custom retry button text */
  retryText?: string
  /** Icon component for error state (Element Plus icon) */
  icon?: Component
  /** Whether to show error details in development */
  showDetails?: boolean
}

const props = withDefaults(defineProps<ErrorBoundaryProps>(), {
  errorTitle: '页面出错了',
  errorText: '抱歉，页面渲染时发生了错误，请尝试重新加载',
  retryText: '重试',
  icon: undefined,
  showDetails: import.meta.env.DEV,
})

const emit = defineEmits<{
  (e: 'error', error: Error, instance: unknown, info: string): void
  (e: 'retry'): void
}>()

const hasError = ref(false)
const capturedError = ref<Error | null>(null)
const errorInfo = ref<string>('')

/**
 * Vue 3 errorCaptured lifecycle hook
 * Catches errors from child component rendering
 */
onErrorCaptured((error: Error, instance: unknown, info: string) => {
  hasError.value = true
  capturedError.value = error
  errorInfo.value = info
  emit('error', error, instance, info)
  return false
})

/**
 * Reset error state and retry rendering
 */
function handleRetry() {
  hasError.value = false
  capturedError.value = null
  errorInfo.value = ''
  emit('retry')
}
</script>

<template>
  <div class="error-boundary">
    <!-- Error fallback UI -->
    <div v-if="hasError" class="error-fallback">
      <el-result icon="warning" :title="errorTitle" :sub-title="errorText">
        <template #icon>
          <el-icon :size="64" class="error-icon">
            <component :is="icon" v-if="icon" />
            <svg v-else viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg">
              <path
                fill="currentColor"
                d="M512 64a448 448 0 1 1 0 896 448 448 0 0 1 0-896zm0 192a58.432 58.432 0 0 0-58.24 63.744l23.36 256.384a35.072 35.072 0 0 0 69.76 0l23.296-256.384A58.432 58.432 0 0 0 512 256zm0 512a51.2 51.2 0 1 0 0-102.4 51.2 51.2 0 0 0 0 102.4z"
              />
            </svg>
          </el-icon>
        </template>

        <template #extra>
          <el-button type="primary" @click="handleRetry">
            {{ retryText }}
          </el-button>
        </template>
      </el-result>

      <!-- Error details (dev mode only) -->
      <div v-if="showDetails && capturedError" class="error-details">
        <el-collapse>
          <el-collapse-item title="错误详情" name="error">
            <div class="error-message">
              <strong>错误信息:</strong>
              {{ capturedError.message }}
            </div>
            <div v-if="errorInfo" class="error-info">
              <strong>错误来源:</strong>
              {{ errorInfo }}
            </div>
            <div v-if="capturedError.stack" class="error-stack">
              <strong>调用堆栈:</strong>
              <pre>{{ capturedError.stack }}</pre>
            </div>
          </el-collapse-item>
        </el-collapse>
      </div>
    </div>

    <!-- Normal content slot -->
    <slot v-else />
  </div>
</template>

<style scoped>
.error-boundary {
  width: 100%;
  height: 100%;
}

.error-fallback {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 300px;
  padding: 24px;
}

.error-icon {
  color: #f56c6c;
}

.error-details {
  width: 100%;
  max-width: 800px;
  margin-top: 24px;
}

.error-message,
.error-info {
  margin-bottom: 12px;
  font-size: 14px;
  color: #606266;
}

.error-message strong,
.error-info strong,
.error-stack strong {
  color: #303133;
}

.error-stack {
  font-size: 12px;
}

.error-stack pre {
  margin: 8px 0 0;
  padding: 12px;
  background-color: #f5f7fa;
  border-radius: 4px;
  overflow-x: auto;
  font-family: 'SF Mono', Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace;
  font-size: 12px;
  line-height: 1.6;
  color: #606266;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
