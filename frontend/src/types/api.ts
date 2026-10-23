export type Source = { id:string; title:string; url:string; passage:string; page_number?:number|null; published_date?:string|null; academic_session?:string|null }
export type Message = { id:string; role:'user'|'assistant'; text:string; sources?:Source[]; evidence?:boolean }
export type Status = { backend:string; ollama:{available:boolean;model_available:boolean;error?:string|null}; model:string; embedding_model:string; embedding_model_available:boolean; documents:number; chunks:number; last_ingestion:string|null; knowledge_base_ready:boolean }
export type IndexedSource = { id:string; url:string; title:string; category:string; type:string; status:string; chunks:number; last_ingested:string; published_date?:string|null }
export type Job = { id:string; status:string; kind:string; started_at:string; finished_at?:string|null; progress:Record<string,unknown>; error?:string|null; result?:Record<string,unknown> }
