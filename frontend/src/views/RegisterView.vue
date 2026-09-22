<script setup lang="ts">
import { ref, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import type { RegisterRequest } from '@/types'
import { getErrorMessage } from '@/utils/error'
import type { FormItemRule } from 'element-plus'

const router = useRouter()
const authStore = useAuthStore()

const formRef = ref()
const loading = ref(false)

const form = reactive<RegisterRequest & { password_confirm: string }>({
  email: '',
  password: '',
  password_confirm: '',
})

const validatePasswordConfirm = (
  _rule: FormItemRule,
  value: string,
  callback: (err?: Error) => void
) => {
  if (value !== form.password) {
    callback(new Error('两次输入的密码不一致'))
  } else {
    callback()
  }
}

const rules = {
  email: [
    { required: true, message: '请输入邮箱', trigger: 'blur' },
    { type: 'email', message: '请输入正确的邮箱格式', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 8, message: '密码至少8位', trigger: 'blur' },
  ],
  password_confirm: [
    { required: true, message: '请确认密码', trigger: 'blur' },
    { validator: validatePasswordConfirm, trigger: 'blur' },
  ],
}

async function handleRegister() {
  try {
    await formRef.value.validate()
    loading.value = true

    await authStore.register({
      email: form.email,
      password: form.password,
    })

    ElMessage.success('注册成功')

    // Redirect to login page
    await router.push('/login')
  } catch (error: unknown) {
    const msg = getErrorMessage(error)
    if (msg) {
      ElMessage.error(msg)
    }
  } finally {
    loading.value = false
  }
}

function goToLogin() {
  void router.push('/login')
}
</script>

<template>
  <div class="register-container">
    <el-card class="register-card">
      <template #header>
        <div class="card-header">
          <h2>注册账户</h2>
          <p>opendata 金融数据管理平台</p>
        </div>
      </template>

      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-width="100px"
        @submit.prevent="handleRegister"
      >
        <el-form-item
          label="邮箱"
          prop="email"
        >
          <el-input
            v-model="form.email"
            type="email"
            placeholder="请输入邮箱"
            :disabled="loading"
          />
        </el-form-item>

        <el-form-item
          label="密码"
          prop="password"
        >
          <el-input
            v-model="form.password"
            type="password"
            placeholder="请输入密码（至少8位）"
            show-password
            :disabled="loading"
          />
        </el-form-item>

        <el-form-item
          label="确认密码"
          prop="password_confirm"
        >
          <el-input
            v-model="form.password_confirm"
            type="password"
            placeholder="请再次输入密码"
            show-password
            :disabled="loading"
            @keyup.enter="handleRegister"
          />
        </el-form-item>

        <el-form-item>
          <el-button
            type="primary"
            :loading="loading"
            style="width: 100%"
            @click="handleRegister"
          >
            注册
          </el-button>
        </el-form-item>

        <el-form-item>
          <el-button
            text
            style="width: 100%"
            @click="goToLogin"
          >
            已有账户？立即登录
          </el-button>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<style scoped>
.register-container {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
}

.register-card {
  width: 420px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
}

.card-header {
  text-align: center;
}

.card-header h2 {
  margin: 0;
  color: #303133;
}

.card-header p {
  margin: 8px 0 0;
  color: #909399;
  font-size: 14px;
}
</style>
