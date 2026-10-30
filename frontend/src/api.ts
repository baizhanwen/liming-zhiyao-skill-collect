export type Doc = Record<string, any>;
export class ApiError extends Error { code: string; detail: any; constructor(message:string,code:string,detail:any){super(message);this.code=code;this.detail=detail;} }
export async function api(path:string, data?:unknown, method?:string, options:{signal?:AbortSignal}={}):Promise<any>{
  const response=await fetch('/api'+path,{method:method||(data===undefined?'GET':'POST'),headers:data instanceof FormData?{}:data===undefined?{}:{'Content-Type':'application/json'},body:data===undefined?undefined:data instanceof FormData?data:JSON.stringify(data),signal:options.signal});
  const value=await response.json().catch(error=>{if(options.signal?.aborted)throw error;return {};});
  if(!response.ok){const e=value.error||value.detail||value;throw new ApiError(typeof e==='string'?e:e.message||`请求失败 (${response.status})`,e.code||String(response.status),value);}
  return value;
}
export const rows=(value:any):Doc[]=>Array.isArray(value)?value:value?.items||value?.records||value?.results||value?.skills||value?.tasks||value?.knowledge||value?.logs||value?.versions||value?.repositories||[];
export const display=(value:any):string=>typeof value==='string'?value:value==null?'':JSON.stringify(value,null,2);
export const formatDate=(value:any)=>value?new Date(value).toLocaleString('zh-CN',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}):'—';
export const statuses:Record<string,string>={queued:'等待中',pending:'等待中',running:'进行中',unknown:'状态待读取',waiting_config:'待配置',needs_config:'待配置',waiting_material:'待补充材料',needs_material:'待补充材料',awaiting_material:'待补充材料',succeeded:'已完成',success:'已完成',completed:'已完成',failed:'失败',cancelled:'已取消',canceled:'已取消',draft:'草稿',analyzed:'已分析',verified:'已验证',disabled:'已停用',archived:'已归档'};
export const stageNames:Record<string,string>={fetch:'获取内容',fetching:'获取内容',source:'获取内容',media:'处理视频',frames:'准备抽样画面',transcribe:'转写',transcription:'转写',vision:'画面分析',project_links:'关联GitHub项目',analyze:'内容分析',analysis:'内容分析',save:'保存结果',saving:'保存结果',queued:'等待开始',done:'完成',complete:'完成'};
