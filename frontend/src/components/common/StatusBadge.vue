<script setup lang="ts">
import { computed } from 'vue'

export type StatusType = 'pending' | 'running' | 'completed' | 'success' | 'failed' | 'timeout' | 'cancelled' | 'active' | 'inactive'

export interface StatusBadgeProps {
  /** Status value */
  status: StatusType | string
  /** Custom display text (overrides default mapping) */
  text?: string
  /** Badge size */
  size?: 'large' | 'default' | 'small'
  /** Enable dot indicator */
  dot?: boolean
}

const props = withDefaults(defineProps<StatusBadgeProps>(), {
  text: undefined,
  size: 'small',
  dot: false,
})

interface StatusConfig {
  text: string
  type: '' | 'success' | 'warning' | 'info' | 'danger' | 'primary'
}

const statusConfigMap: Record<string, StatusConfig> = {
  pending: { text: '等待中', type: 'info' },
  running: { text: '执行中', type: 'primary' },
  completed: { text: '已完成', type: 'success' },
  success: { text: '成功', type: 'success' },
  failed: { text: '失败', type: 'danger' },
  timeout: { text: '超时', type: 'warning' },
  cancelled: { text: '已取消', type: 'info' },
  active: { text: '启用', type: 'success' },
  inactive: { text: '禁用', type: 'info' },
}

const statusConfig = computed<StatusConfig>(() => {
  const config = statusConfigMap[props.status.toLowerCase()]
  if (config) {
    return {
      text: props.text ?? config.text,
      type: config.type,
    }
  }
  return {
    text: props.text ?? props.status,
    type: 'info',
  }
})
</script>

<template>
  <span class="status-badge">
    <el-tag
      :type="statusConfig.type"
      :size="size"
      :effect="dot ? 'light' : undefined"
    >
      <span
        v-if="dot"
        :class="['status-dot', statusConfig.type]"
      />
      {{ statusConfig.text }}
    </el-tag>
  </span>
</template>

<style scoped>
.status-badge {
  display: inline-flex;
  align-items: center;
}

.status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  margin-right: 6px;
  display: inline-block;
}

.status-dot.success {
  background-color: #67c23a;
}

.status-dot.danger {
  background-color: #f56c6c;
}

.status-dot.warning {
  background-color: #e6a23c;
}

.status-dot.primary {
  background-color: #409eff;
}

.status-dot.info {
  background-color: #909399;
}
</style>
