import { http } from '../lib/http.ts'

/** 创建结账会话后回来的数据（后端 `api_payment.create_checkout_session`）。 */
export interface CheckoutSession {
  checkout_url: string
  order_no: string
  session_id: string
}

export async function createCheckoutSession(planType: string): Promise<CheckoutSession> {
  const res = await http.post('/api/payment/create-checkout', {
    plan_type: planType,
  })
  return res.data.data
}
