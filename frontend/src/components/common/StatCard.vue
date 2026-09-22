<script setup lang="ts">
import type { Component } from 'vue'

export interface StatCardProps {
  /** Icon component from Element Plus */
  icon?: Component
  /** Stat value to display */
  value: string | number
  /** Label text below value */
  label: string
  /** Visual style variant */
  type?: 'primary' | 'success' | 'danger' | 'warning' | 'info'
  /** Enable hover animation */
  hoverable?: boolean
}

 
const _props = withDefaults(defineProps<StatCardProps>(), {
  type: 'primary',
  hoverable: true,
})
</script>

<template>
  <el-card :class="['stat-card', { hoverable: hoverable }]">
    <div class="stat-content">
      <div
        v-if="icon"
        :class="['stat-icon', type]"
      >
        <el-icon :size="24">
          <component :is="icon" />
        </el-icon>
      </div>
      <div class="stat-info">
        <div class="stat-value">
          {{ value }}
        </div>
        <div class="stat-label">
          {{ label }}
        </div>
      </div>
    </div>
  </el-card>
</template>

<style scoped>
.stat-card {
  cursor: default;
}

.stat-card.hoverable {
  cursor: pointer;
  transition: transform 0.2s ease, box-shadow 0.2s ease;
}

.stat-card.hoverable:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
}

.stat-content {
  display: flex;
  align-items: center;
  gap: 16px;
}

.stat-icon {
  width: 48px;
  height: 48px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.stat-icon.primary {
  background-color: #e6f7ff;
  color: #1890ff;
}

.stat-icon.success {
  background-color: #f6ffed;
  color: #52c41a;
}

.stat-icon.danger {
  background-color: #fff1f0;
  color: #ff4d4f;
}

.stat-icon.warning {
  background-color: #fffbe6;
  color: #faad14;
}

.stat-icon.info {
  background-color: #f4f4f5;
  color: #909399;
}

.stat-info {
  flex: 1;
  min-width: 0;
}

.stat-value {
  font-size: 24px;
  font-weight: bold;
  color: #303133;
  line-height: 1.2;
}

.stat-label {
  font-size: 14px;
  color: #909399;
  margin-top: 4px;
}
</style>
