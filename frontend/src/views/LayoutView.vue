<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useAuthStore } from '@/stores/auth'
import { useThemeStore } from '@/stores/theme'
import { setLocale } from '@/i18n'
import { Sunny, Moon } from '@element-plus/icons-vue'

const { t, locale } = useI18n()
const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()
const themeStore = useThemeStore()

const isCollapse = ref(false)
const activeMenu = computed(() => route.path)

const languageOptions = [
  { label: '简体中文', value: 'zh-CN' },
  { label: 'English', value: 'en-US' },
]

onMounted(() => {
  themeStore.initTheme()
})

const menuItems = computed(() => {
  const items = [
    { index: '/', name: t('nav.home'), icon: 'HomeFilled' },
    { index: '/data', name: t('nav.catalog'), icon: 'DataAnalysis' },
    { index: '/scripts', name: t('nav.scripts'), icon: 'Document' },
    { index: '/tasks', name: t('nav.tasks'), icon: 'Timer' },
    { index: '/executions', name: t('nav.executions'), icon: 'List' },
    { index: '/tables', name: t('nav.tables'), icon: 'Grid' },
  ]

  if (authStore.isAdmin) {
    items.push({ index: '/admin/interfaces', name: t('nav.interfaceManagement'), icon: 'Setting' })
    items.push({ index: '/users', name: t('nav.users'), icon: 'User' })
    items.push({ index: '/settings', name: t('nav.settings'), icon: 'Setting' })
  }

  return items
})

function handleLogout() {
  authStore.logout()
  void router.push('/login')
}

function toggleCollapse() {
  isCollapse.value = !isCollapse.value
}

function handleThemeToggle() {
  themeStore.toggleTheme()
}

function handleLanguageChange(lang: string) {
  setLocale(lang)
}
</script>

<template>
  <el-container class="layout-container">
    <el-aside
      :width="isCollapse ? '64px' : '200px'"
      class="sidebar"
    >
      <div class="logo">
        <span v-if="!isCollapse">opendata</span>
        <span v-else>ak</span>
      </div>
      <el-menu
        :default-active="activeMenu"
        :collapse="isCollapse"
        :collapse-transition="false"
        router
      >
        <el-menu-item
          v-for="item in menuItems"
          :key="item.index"
          :index="item.index"
        >
          <el-icon><component :is="item.icon" /></el-icon>
          <template #title>
            {{ item.name }}
          </template>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="header">
        <div class="header-left">
          <el-button
            :icon="isCollapse ? 'Expand' : 'Fold'"
            text
            @click="toggleCollapse"
          />
        </div>
        <div class="header-right">
          <el-dropdown @command="handleLanguageChange">
            <el-button text>
              {{ locale === 'zh-CN' ? '中文' : 'EN' }}
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item
                  v-for="lang in languageOptions"
                  :key="lang.value"
                  :command="lang.value"
                  :disabled="locale === lang.value"
                >
                  {{ lang.label }}
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
          <el-tooltip :content="themeStore.mode === 'dark' ? t('theme.toggleLight') : t('theme.toggleDark')">
            <el-button
              :icon="themeStore.mode === 'dark' ? Sunny : Moon"
              circle
              @click="handleThemeToggle"
            />
          </el-tooltip>
          <el-dropdown>
            <span class="user-dropdown">
              <el-avatar :size="32" icon="UserFilled" />
              <span class="username">{{ authStore.user?.email }}</span>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item disabled>
                  {{ t('common.role') }}: {{ authStore.user?.role }}
                </el-dropdown-item>
                <el-dropdown-item divided @click="handleLogout">
                  {{ t('common.logout') }}
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>
      <el-main class="main-content">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.layout-container {
  height: 100vh;
}

.sidebar {
  background-color: var(--sidebar-bg, #001529);
  transition: width 0.3s;
}

.logo {
  height: 60px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 18px;
  font-weight: bold;
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
}

.sidebar :deep(.el-menu) {
  border-right: none;
  background-color: transparent;
}

.sidebar :deep(.el-menu-item) {
  color: var(--sidebar-text, rgba(255, 255, 255, 0.65));
}

.sidebar :deep(.el-menu-item:hover) {
  background-color: var(--sidebar-active-bg, #1890ff) !important;
  color: #fff;
}

.sidebar :deep(.el-menu-item.is-active) {
  background-color: var(--sidebar-active-bg, #1890ff) !important;
  color: #fff;
}

.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid var(--border-color, #f0f0f0);
  padding: 0 20px;
  background-color: var(--card-bg, #fff);
}

.header-left {
  display: flex;
  align-items: center;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.user-dropdown {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
}

.username {
  font-size: 14px;
  color: var(--text-color, #303133);
}

.main-content {
  background-color: var(--content-bg, #f0f2f5);
  overflow-y: auto;
}

@media (max-width: 768px) {
  .sidebar {
    position: fixed;
    left: 0;
    top: 0;
    height: 100vh;
    z-index: 1000;
  }

  .username {
    display: none;
  }
}
</style>
