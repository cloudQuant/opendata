import { ElMessage, ElNotification, type MessageParams, type NotificationParams } from 'element-plus'

export type NotificationType = 'success' | 'warning' | 'info' | 'error'

export function useNotification() {
  function success(message: string) {
    ElMessage.success(message)
  }

  function error(message: string) {
    ElMessage.error(message)
  }

  function warning(message: string) {
    ElMessage.warning(message)
  }

  function info(message: string) {
    ElMessage.info(message)
  }

  function notify(title: string, message: string, type: NotificationType = 'info') {
    ElNotification({
      title,
      message,
      type,
      duration: 3000,
    })
  }

  function notifySuccess(title: string, message: string) {
    notify(title, message, 'success')
  }

  function notifyError(title: string, message: string) {
    notify(title, message, 'error')
  }

  function notifyWarning(title: string, message: string) {
    notify(title, message, 'warning')
  }

  return {
    success,
    error,
    warning,
    info,
    notify,
    notifySuccess,
    notifyError,
    notifyWarning,
  }
}
