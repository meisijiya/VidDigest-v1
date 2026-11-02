import { clearToken, http, httpAnonymous, saveToken } from '../lib/http.ts'

const USER_KEY = 'auth_user'

/**
 * 登录用户。
 *
 * 字段是**开放**的：登录响应与 /api/auth/me 给回来的字段并不一致
 * （注册响应里就没有 is_admin，见 backend/api_auth.py），多出来的字段
 * 要能原样带过去，所以这里带索引签名而不是把字段逐个列死。
 */
export interface SessionUser {
  id?: number
  email?: string
  is_admin?: boolean
  is_vip?: boolean
  [key: string]: unknown
}

/**
 * 用户快照的读取出口。
 *
 * 这是**会话种子**，不是凭据：凭据（token）的落盘与读取在 `lib/http.ts`，
 * 那里是唯一真值。这里存的是「上次是谁」，只为第一帧不用空一拍。
 */
export function getSavedUser(): SessionUser | null {
  const data = localStorage.getItem(USER_KEY)
  return data ? (JSON.parse(data) as SessionUser) : null
}

function saveUser(user: SessionUser): void {
  localStorage.setItem(USER_KEY, JSON.stringify(user))
}

/**
 * 注册。走**匿名**出口：注册不需要凭据，也不该把旧 token 发去用不上的地方。
 */
export async function register(email: string, password: string): Promise<SessionUser> {
  const res = await httpAnonymous.post('/api/auth/register', { email, password })
  const { token, user } = res.data.data
  saveToken(token)
  saveUser(user)
  return user
}

/**
 * 登录。同样走匿名出口——登录要的是「换一份新凭据」，不是「带上旧的」。
 */
export async function login(email: string, password: string): Promise<SessionUser> {
  const res = await httpAnonymous.post('/api/auth/login', { email, password })
  const { token, user } = res.data.data
  saveToken(token)
  saveUser(user)
  return user
}

/** 回查本人信息。需要凭据，所以走带 token 的出口。 */
export async function fetchMe(): Promise<SessionUser> {
  const res = await http.get('/api/auth/me')
  const user = res.data.data as SessionUser
  saveUser(user)
  return user
}

/** 登出：token 与用户快照一起清。 */
export function logout(): void {
  clearToken()
  localStorage.removeItem(USER_KEY)
}
