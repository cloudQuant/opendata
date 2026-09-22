import { defineStore } from 'pinia'
import { ref, watch } from 'vue'

export type ThemeMode = 'light' | 'dark'

export const useThemeStore = defineStore(
  'theme',
  () => {
    const mode = ref<ThemeMode>('light')

    function toggleTheme() {
      mode.value = mode.value === 'light' ? 'dark' : 'light'
    }

    function setTheme(newMode: ThemeMode) {
      mode.value = newMode
    }

    function initTheme() {
      applyTheme(mode.value)
    }

    function applyTheme(theme: ThemeMode) {
      const html = document.documentElement
      if (theme === 'dark') {
        html.classList.add('dark')
      } else {
        html.classList.remove('dark')
      }
    }

    watch(mode, (newMode) => {
      applyTheme(newMode)
    })

    return {
      mode,
      toggleTheme,
      setTheme,
      initTheme,
    }
  },
  {
    persist: {
      paths: ['mode'],
    },
  }
)
