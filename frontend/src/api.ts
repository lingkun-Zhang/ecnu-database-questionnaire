import type { FieldError } from './types'
const API = import.meta.env.VITE_API_BASE || '/api'
export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public fieldErrors: FieldError[] = [], public requestId?: string) { super(message) }
}
export function getToken() { return localStorage.getItem('wenchuan_token') || '' }
export function setToken(token: string) { token ? localStorage.setItem('wenchuan_token', token) : localStorage.removeItem('wenchuan_token') }
export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers)
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  if (getToken()) headers.set('Authorization', `Bearer ${getToken()}`)
  let response: Response
  try { response = await fetch(`${API}${path}`, {...options, headers}) }
  catch { throw new ApiError(0, 'NETWORK_ERROR', '暂时无法连接服务，请检查网络后重试。结果未确认前，请保留原提交标识。') }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    const value = typeof body.detail === 'object' && !Array.isArray(body.detail) ? body.detail : body
    const errors = value.fieldErrors || (Array.isArray(body.detail) ? body.detail.map((item: {msg: string;loc?:unknown[]}) => ({message:item.msg,field:item.loc?.join('.')})) : [])
    throw new ApiError(response.status, value.code || `HTTP_${response.status}`, value.message || (typeof body.detail === 'string' ? body.detail : '请求未完成，请稍后重试。'), errors, value.requestId)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}
export const post = <T>(path: string, body: unknown, headers?: HeadersInit) => request<T>(path, {method:'POST',body:JSON.stringify(body),headers})
export async function download(path: string, filename: string, isCurrent:()=>boolean = ()=>true) {
  if (!isCurrent()) return
  const response = await fetch(`${API}${path}`, {headers: {Authorization:`Bearer ${getToken()}`}})
  if (!isCurrent()) return
  if (!response.ok) { const body = await response.json().catch(()=>({})); if (!isCurrent()) return; throw new ApiError(response.status,body.code || 'EXPORT_ERROR',body.message || '导出失败，请重新获取统计范围后重试。') }
  const blob = await response.blob()
  if (!isCurrent()) return
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a'); link.href=url;link.download=filename;link.click();URL.revokeObjectURL(url)
}
