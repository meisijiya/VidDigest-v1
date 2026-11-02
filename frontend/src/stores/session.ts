import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { fetchMe, getSavedUser, logout as logoutApi, type SessionUser } from '../api/auth.ts'
import { getToken, isLoggedIn, isUnauthorized } from '../lib/http.ts'

/**
 * 会话的唯一持有者：登录用户、token、管理员标记。
 *
 * 为什么单独一个 store：这三样东西**在多个地方被读**，以前散在
 * App.vue 的局部 ref 里（currentUser / isAdmin / token 的 localStorage
 * 读取），每个读点各自去摸一遍 localStorage，谁改了谁不知道。
 * 现在读点统一走这里，写点也只有这里。
 *
 * 与 api/auth.ts 的分工：api/auth.ts 拥有**落盘**（localStorage 是唯一
 * 落盘处），本 store 拥有**内存里的这份状态**（给界面渲染用）。
 * 两者在登录 / 登出 / 回查三处同步，不存在第二个落盘处。
 */
export type AdminVerdict = 'admin' | 'user' | 'anonymous'

export const useSessionStore = defineStore('session', () => {
  /** 当前登录用户（未登录为 null）。字段与后端 /api/auth/me 的返回一致。 */
  const user = ref<SessionUser | null>(getSavedUser())
  /**
   * token 的内存镜像。**真值在 localStorage**（api/auth.ts 落盘），
   * 这里存一份给界面和路由守卫读；每次登录 / 登出 / 回查都会同步。
   */
  const token = ref<string | null>(getToken())
  /** 是不是管理员。注意：**登录后必须回查 /api/auth/me**，不能直接读 user.is_admin。 */
  const isAdmin = ref(!!user.value?.is_admin)

  /**
   * 路由守卫判定「这条要管理员的路径，但当前没登录」时置位。
   * 由 App.vue 消费：弹登录框。守卫自己不碰界面。
   */
  const loginRequested = ref(false)

  /**
   * 在途回查的代次。每发一次回查 +1；登出也 +1。
   * 只有代次仍然是最新的那次回查才允许改 isAdmin——
   * 否则「登出后在途回查回来」会把管理员入口重新点亮。
   */
  let adminFlagGen = 0

  const isAuthenticated = computed(() => !!token.value)

  function currentVerdict(): AdminVerdict {
    if (isAdmin.value) return 'admin'
    return isLoggedIn() ? 'user' : 'anonymous'
  }

  /**
   * 登录 / 注册之后写入新用户状态。token 重新从落盘处读，避免两份记忆不一致。
   *
   * **不**在这里改 isAdmin：登录响应不是安全边界（注册响应里根本没有
   * is_admin 这个字段），管理员判据只由 refresh() 回查 /api/auth/me 决定
   * （ADR 0010）。写在这里会让「回查在途」的那一小段时间里提前亮出入口。
   */
  function applyUser(next: SessionUser | null): void {
    user.value = next
    token.value = getToken()
  }

  /** 守卫发现当前访问需要登录，而用户没登录。 */
  function requestLogin(): void {
    loginRequested.value = true
  }

  function clearLoginRequest(): void {
    loginRequested.value = false
  }

  /** 登出：代次 +1 作废在途回查，然后清空本地状态。 */
  function signOut(): void {
    adminFlagGen++
    logoutApi()
    user.value = null
    token.value = null
    isAdmin.value = false
  }

  /**
   * 回查 /api/auth/me，判定管理员身份。
   *
   * @param syncUser true = 顺带把服务端返回的用户写回本地（登出态复核用）
   * @returns 'admin' | 'user' | 'anonymous'
   */
  async function refresh(syncUser: boolean): Promise<AdminVerdict> {
    const gen = ++adminFlagGen
    if (!isLoggedIn()) {
      isAdmin.value = false
      return 'anonymous'
    }
    try {
      const fresh = await fetchMe()
      if (gen !== adminFlagGen) return currentVerdict()
      isAdmin.value = !!fresh?.is_admin
      if (syncUser && fresh) {
        user.value = fresh
        token.value = getToken()
      }
      return isAdmin.value ? 'admin' : 'user'
    } catch (err) {
      if (gen === adminFlagGen) {
        isAdmin.value = false
        // 401 = 服务端不认这个 token（过期或无效）。本地那份会话就此作废：
        // 不清的话，顶栏会一直按 localStorage 的种子渲染成一个「已登录」的
        // 旧账号，而每个请求都在 401 —— 顶栏与实际状态对不上。
        // 只清 401：网络抖动与 5xx 不该把人踢出登录态。
        if (isUnauthorized(err)) signOut()
      }
      return 'anonymous'
    }
  }

  /**
   * 路由守卫用的鉴权：**等待**异步回查结果再决定放不放行。
   *
   * 回查失败（网络问题 / token 失效）时返回 'user'：不在前端猜，
   * 让 AdminPage 自己那一次请求去落地 403 与错误态——
   * 管理后台的判定权始终在服务端（ADR 0010）。
   */
  async function resolveAdminAccess(): Promise<AdminVerdict> {
    if (!isLoggedIn()) return 'anonymous'
    try {
      const fresh = await fetchMe()
      return fresh?.is_admin ? 'admin' : 'user'
    } catch {
      return 'user'
    }
  }

  return {
    user,
    token,
    isAdmin,
    isAuthenticated,
    loginRequested,
    applyUser,
    requestLogin,
    clearLoginRequest,
    signOut,
    refresh,
    resolveAdminAccess,
  }
})
