import type { Job, Status, IndexedSource } from '../types/api'
const base = import.meta.env.VITE_API_URL || 'http://127.0.0.1:8000'
async function request<T>(path:string, init?:RequestInit):Promise<T> {
  const res = await fetch(`${base}${path}`, init)
  if (!res.ok) { let detail = `Request failed (${res.status})`; try { const body=await res.json(); detail=body.detail || detail } catch {} throw new Error(detail) }
  return res.json()
}
export const api = {
  status:()=>request<Status>('/api/status'),
  sources:async()=> (await request<{sources:IndexedSource[]}>('/api/sources')).sources,
  chat:(question:string)=>request<{answer:string;sources:import('../types/api').Source[];evidence_sufficient:boolean}>('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question})}),
  addUrl:(url:string)=>request<Job>('/api/sources/url',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})}),
  upload:(file:File)=>{const data=new FormData();data.append('file',file);return request<Job>('/api/sources/upload',{method:'POST',body:data})},
  remove:(id:string)=>request(`/api/sources/${id}`,{method:'DELETE'}),
  start:(kind:'crawl'|'incremental'|'reindex'|'retry')=>request<Job>(`/api/ingestion/${kind}`,{method:'POST'}),
  job:(id:string)=>request<Job>(`/api/ingestion/jobs/${id}`),
}
