import { createI18n } from 'vue-i18n'
import zhCN from './locales/zh-CN'
import enUS from './locales/en-US'

type MessageSchema = typeof zhCN

const messages: Record<string, MessageSchema> = {
  'zh-CN': zhCN,
  'en-US': enUS,
}

const i18n = createI18n<[MessageSchema], 'zh-CN' | 'en-US'>({
  legacy: false,
  locale: (localStorage.getItem('locale') as 'zh-CN' | 'en-US') || 'zh-CN',
  fallbackLocale: 'zh-CN',
  messages,
})

export default i18n

export function setLocale(locale: 'zh-CN' | 'en-US') {
  ;(i18n.global.locale as unknown as string) = locale
  localStorage.setItem('locale', locale)
  document.querySelector('html')?.setAttribute('lang', locale)
}

export function getLocale(): 'zh-CN' | 'en-US' {
  return i18n.global.locale.value as 'zh-CN' | 'en-US'
}
