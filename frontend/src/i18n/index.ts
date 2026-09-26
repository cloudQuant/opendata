import { createI18n } from 'vue-i18n'
import zhCN from './locales/zh-CN'
import enUS from './locales/en-US'

export type Locale = 'zh-CN' | 'en-US'

type MessageSchema = typeof zhCN

const messages: Record<Locale, MessageSchema> = {
  'zh-CN': zhCN,
  'en-US': enUS,
}

const i18n = createI18n({
  legacy: false,
  locale: (localStorage.getItem('locale') as Locale | null) ?? 'zh-CN',
  fallbackLocale: 'zh-CN',
  messages,
})

export default i18n

export function setLocale(locale: Locale) {
  i18n.global.locale.value = locale
  localStorage.setItem('locale', locale)
  document.querySelector('html')?.setAttribute('lang', locale)
}

export function getLocale(): Locale {
  return i18n.global.locale.value as Locale
}
