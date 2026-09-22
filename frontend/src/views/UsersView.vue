<script setup lang="ts">
import { computed, ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { usersApi } from '@/api/users'
import { useAuthStore } from '@/stores/auth'
import { getApiErrorMessage } from '@/utils/error'
import type { User } from '@/types'

const router = useRouter()
const authStore = useAuthStore()

const users = ref<User[]>([])
const loading = ref(false)
const searchKeyword = ref('')
const currentPage = ref(1)
const pageSize = ref(20)
const total = ref(0)

const isAdmin = authStore.isAdmin

const filteredUsers = computed(() => {
  if (!searchKeyword.value) return users.value
  const keyword = searchKeyword.value.toLowerCase()
  return users.value.filter((u) => u.email.toLowerCase().includes(keyword))
})

async function loadUsers() {
  loading.value = true
  try {
    const data = await usersApi.list({
      page: currentPage.value,
      page_size: pageSize.value,
    })
    users.value = data.items ?? []
    total.value = data.total ?? 0
  } catch (error) {
    ElMessage.error(getApiErrorMessage(error))
  } finally {
    loading.value = false
  }
}

async function handleUpdateRole(user: User) {
  try {
    const result = await ElMessageBox.prompt('请选择用户角色', '修改角色', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      inputPattern: /^(admin|user)$/,
      inputErrorMessage: '请输入 admin 或 user',
      inputValue: user.role,
    })
    const role = (result as { value: string }).value as 'admin' | 'user'
    await usersApi.updateRole(user.id, role)
    ElMessage.success('角色更新成功')
    loadUsers()
  } catch (error) {
    if (error !== 'cancel') {
      ElMessage.error(getApiErrorMessage(error))
    }
  }
}

async function handleDelete(user: User) {
  try {
    await ElMessageBox.confirm(`确定要删除用户 ${user.email} 吗？`, '确认删除', {
      type: 'warning',
    })
    await usersApi.delete(user.id)
    ElMessage.success('删除成功')
    loadUsers()
  } catch (error: unknown) {
    if (error !== 'cancel') {
      ElMessage.error(getApiErrorMessage(error))
    }
  }
}

function handlePageChange(page: number) {
  currentPage.value = page
  loadUsers()
}

onMounted(() => {
  if (!isAdmin) {
    ElMessage.error('权限不足')
    router.push('/')
    return
  }
  loadUsers()
})
</script>

<template>
  <div class="users-view">
    <el-card>
      <template #header>
        <div class="header">
          <span>用户管理</span>
        </div>
      </template>

      <div
        v-if="!isAdmin"
        class="no-permission"
      >
        <el-empty description="您没有权限访问此页面" />
      </div>

      <div
        v-else
        class="content"
      >
        <div class="search-bar">
          <el-input
            v-model="searchKeyword"
            placeholder="搜索邮箱"
            clearable
            style="max-width: 400px"
          >
            <template #prefix>
              <el-icon><Search /></el-icon>
            </template>
          </el-input>
        </div>

        <el-table
          v-loading="loading"
          :data="filteredUsers"
          style="width: 100%"
          stripe
        >
          <el-table-column
            prop="id"
            label="ID"
            width="80"
          />
          <el-table-column
            prop="email"
            label="邮箱"
            min-width="200"
          />
          <el-table-column
            label="角色"
            width="120"
          >
            <template #default="{ row }">
              <el-tag
                :type="row.role === 'admin' ? 'danger' : 'primary'"
                size="small"
              >
                {{ row.role }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column
            prop="created_at"
            label="创建时间"
            width="180"
          >
            <template #default="{ row }">
              {{ new Date(row.created_at).toLocaleString() }}
            </template>
          </el-table-column>
          <el-table-column
            label="操作"
            width="150"
            fixed="right"
          >
            <template #default="{ row }">
              <el-button
                type="primary"
                link
                size="small"
                @click="handleUpdateRole(row)"
              >
                修改角色
              </el-button>
              <el-button
                v-if="row.email !== authStore.user?.email"
                type="danger"
                link
                size="small"
                @click="handleDelete(row)"
              >
                删除
              </el-button>
            </template>
          </el-table-column>
        </el-table>

        <div class="pagination">
          <el-pagination
            :current-page="currentPage"
            :page-size="pageSize"
            :total="total"
            layout="total, prev, pager, next"
            @current-change="handlePageChange"
          />
        </div>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.users-view {
  padding: 0;
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.content {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.search-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.pagination {
  display: flex;
  justify-content: center;
}

.no-permission {
  padding: 60px 0;
}
</style>
