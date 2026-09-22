<script setup lang="ts">
export interface PageHeaderProps {
  /** Main page title */
  title: string
  /** Optional subtitle/description */
  subtitle?: string
  /** Show bottom border */
  bordered?: boolean
  /** Actions slot for action buttons */
  actions?: boolean
}

const props = withDefaults(defineProps<PageHeaderProps>(), {
  subtitle: '',
  bordered: true,
  actions: false,
})
</script>

<template>
  <div :class="['page-header', { bordered }]">
    <div class="header-content">
      <h2 class="title">
        {{ title }}
      </h2>
      <p
        v-if="subtitle"
        class="subtitle"
      >
        {{ subtitle }}
      </p>
    </div>
    <div
      v-if="$slots.actions"
      class="header-actions"
    >
      <slot name="actions" />
    </div>
  </div>
</template>

<style scoped>
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 20px;
}

.page-header.bordered {
  padding-bottom: 16px;
  border-bottom: 1px solid #ebeef5;
}

.header-content {
  flex: 1;
  min-width: 0;
}

.title {
  font-size: 20px;
  font-weight: 600;
  color: #303133;
  margin: 0;
  line-height: 1.4;
}

.subtitle {
  font-size: 14px;
  color: #909399;
  margin: 8px 0 0;
  line-height: 1.5;
}

.header-actions {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 12px;
}
</style>
