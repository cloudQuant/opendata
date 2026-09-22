<script setup lang="ts">
import type { Component } from 'vue'

export interface EmptyStateProps {
  /** Empty state description text */
  text?: string
  /** Icon component (Element Plus icon) */
  icon?: Component
  /** Action button text */
  actionText?: string
  /** Action button type */
  actionType?: 'primary' | 'success' | 'warning' | 'danger' | 'info'
}

 
const _props = withDefaults(defineProps<EmptyStateProps>(), {
  text: '暂无数据',
  icon: undefined,
  actionText: '',
  actionType: 'primary',
})

const emit = defineEmits<{
  (e: 'action-click'): void
}>()

function handleAction() {
  emit('action-click')
}
</script>

<template>
  <div class="empty-state">
    <el-empty :description="text">
      <template #image>
        <el-icon
          v-if="icon"
          :size="80"
          class="empty-icon"
        >
          <component :is="icon" />
        </el-icon>
      </template>
      <el-button
        v-if="actionText"
        :type="actionType"
        @click="handleAction"
      >
        {{ actionText }}
      </el-button>
    </el-empty>
  </div>
</template>

<style scoped>
.empty-state {
  padding: 40px 20px;
  display: flex;
  justify-content: center;
  align-items: center;
}

.empty-icon {
  color: #c0c4cc;
}
</style>
