import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('@/views/LoginView.vue'),
    meta: { requiresAuth: false, title: '登录' },
  },
  {
    path: '/register',
    name: 'Register',
    component: () => import('@/views/RegisterView.vue'),
    meta: { requiresAuth: false, title: '注册' },
  },
  {
    path: '/',
    component: () => import('@/views/LayoutView.vue'),
    meta: { requiresAuth: true },
    children: [
      {
        path: '',
        name: 'Home',
        component: () => import('@/views/HomeView.vue'),
        meta: { title: '首页' },
      },
      {
        path: 'scripts',
        name: 'Scripts',
        component: () => import('@/views/ScriptsView.vue'),
        meta: { title: '数据接口' },
      },
      {
        path: 'scripts/:id',
        name: 'ScriptDetail',
        component: () => import('@/views/ScriptDetailView.vue'),
        meta: { title: '接口详情' },
      },
      {
        path: 'tasks',
        name: 'Tasks',
        component: () => import('@/views/TasksView.vue'),
        meta: { title: '定时任务' },
      },
      {
        path: 'executions',
        name: 'Executions',
        component: () => import('@/views/ExecutionsView.vue'),
        meta: { title: '执行记录' },
      },
      {
        path: 'tables',
        name: 'Tables',
        component: () => import('@/views/TablesView.vue'),
        meta: { title: '数据表' },
      },
      {
        path: 'tables/:id',
        name: 'TableDetail',
        component: () => import('@/views/TableDetailView.vue'),
        meta: { title: '表详情' },
      },
      {
        path: 'users',
        name: 'Users',
        component: () => import('@/views/UsersView.vue'),
        meta: { title: '用户管理', requiresAdmin: true },
      },
      {
        path: 'admin/interfaces',
        name: 'InterfaceManagement',
        component: () => import('@/views/admin/InterfaceManagement.vue'),
        meta: { title: '接口管理', requiresAdmin: true },
      },
      {
        path: 'settings',
        name: 'Settings',
        component: () => import('@/views/SettingsView.vue'),
        meta: { title: '设置', requiresAdmin: true },
      },
    ],
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'NotFound',
    component: () => import('@/views/NotFoundView.vue'),
    meta: { title: '404' },
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

// Navigation guard
router.beforeEach((to, _from, next) => {
  const authStore = useAuthStore()

  // Update page title
  if (to.meta.title) {
    document.title = `${to.meta.title} - opendata`
  }

  // Check if route requires authentication
  if (to.meta.requiresAuth !== false) {
    if (!authStore.isAuthenticated) {
      // Save redirect path
      next({
        name: 'Login',
        query: { redirect: to.fullPath },
      })
      return
    }

    // Check admin requirement
    if (to.meta.requiresAdmin && !authStore.isAdmin) {
      next({ name: 'Home' })
      return
    }
  }

  // Redirect authenticated users away from login/register
  if (authStore.isAuthenticated && (to.name === 'Login' || to.name === 'Register')) {
    next({ name: 'Home' })
    return
  }

  next()
})

export default router
