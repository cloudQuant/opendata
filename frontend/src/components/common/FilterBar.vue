<script setup lang="ts">
import { Search } from '@element-plus/icons-vue'

export interface FilterOption {
  label: string
  value: string | number
}

export interface FilterConfig {
  key: string
  label: string
  placeholder?: string
  options: FilterOption[]
  modelValue?: string | number | undefined
}

export interface FilterBarProps {
  /** Search input placeholder */
  searchPlaceholder?: string
  /** Current search value */
  searchValue?: string
  /** Show search input */
  showSearch?: boolean
  /** Clearable search input */
  clearable?: boolean
  /** Filter configurations */
  filters?: FilterConfig[]
}

const props = withDefaults(defineProps<FilterBarProps>(), {
  searchPlaceholder: '搜索...',
  searchValue: '',
  showSearch: true,
  clearable: true,
  filters: () => [],
})

const emit = defineEmits<{
  (e: 'update:searchValue', value: string): void
  (e: 'search', value: string): void
  (e: 'clear'): void
  (e: 'filter-change', key: string, value: string | number | undefined): void
}>()

function handleSearchInput(value: string) {
  emit('update:searchValue', value)
  emit('search', value)
}

function handleClear() {
  emit('update:searchValue', '')
  emit('clear')
}

function handleFilterChange(key: string, value: string | number | undefined) {
  emit('filter-change', key, value)
}
</script>

<template>
  <div class="filter-bar">
    <div
      v-if="filters.length > 0"
      class="filter-left"
    >
      <slot name="filters">
        <el-select
          v-for="filter in filters"
          :key="filter.key"
          :model-value="filter.modelValue"
          :placeholder="filter.placeholder || filter.label"
          clearable
          class="filter-select"
          @update:model-value="(val: string | number | undefined) => handleFilterChange(filter.key, val)"
        >
          <el-option
            v-for="opt in filter.options"
            :key="opt.value"
            :label="opt.label"
            :value="opt.value"
          />
        </el-select>
      </slot>
    </div>

    <div
      v-if="showSearch"
      class="filter-right"
    >
      <el-input
        :model-value="searchValue"
        :placeholder="searchPlaceholder"
        :clearable="clearable"
        class="search-input"
        @update:model-value="handleSearchInput"
        @clear="handleClear"
      >
        <template #prefix>
          <el-icon><Search /></el-icon>
        </template>
      </el-input>
    </div>
  </div>
</template>

<style scoped>
.filter-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}

.filter-left {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.filter-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.filter-select {
  min-width: 150px;
}

.search-input {
  width: 280px;
}

@media (max-width: 768px) {
  .filter-bar {
    flex-direction: column;
    align-items: stretch;
  }

  .filter-left,
  .filter-right {
    width: 100%;
  }

  .search-input {
    width: 100%;
  }

  .filter-select {
    flex: 1;
    min-width: 0;
  }
}
</style>
