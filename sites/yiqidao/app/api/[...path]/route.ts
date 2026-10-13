import { env } from 'cloudflare:workers';
import { ready,checkForm,createSession,startPlan,getSessionResult,demo,safeText } from '@/lib/travel';
export const dynamic='force-dynamic';
const c=()=>env as unknown as Record<string,any>;
function response(value:any,status=200,cookie?:string){const h:Record<string,string>={'Content-Type':'application/json','Cache-Control':'no-store'};if(cookie)h['Set-Cookie']=`yqd_owner=${cookie}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=2592000`;return new Response(JSON.stringify(value),{status,headers:h});}
async function digest(value:string){return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(value)))).map(x=>x.toString(16).padStart(2,'0')).join('');}
function database(){if(!c().DB)throw new Error('网站存储尚未就绪，请稍后重试');return c().DB as D1Database;}
async function handle(request:Request){
 const url=new URL(request.url);const parts=url.pathname.split('/').filter(Boolean).slice(1);const method=request.method;
 const cookies=request.headers.get('cookie')||'';const old=cookies.match(/(?:^|;\s*)yqd_owner=([a-f0-9-]{36})/)?.[1];const token=old||crypto.randomUUID();const owner=await digest(request.headers.get('oai-authenticated-user-id')||token);const cookie=old?undefined:token;
 try{
 if(method==='POST'&&request.headers.get('origin')&&request.headers.get('origin')!==url.origin)return response({error:{message:'请求来源不匹配'}},403,cookie);
 if(parts[0]==='health')return response({ok:true,configured:ready(),provider:'百炼 Managed Agent',mode:ready()?'live':c().LIVE_PAUSE_REASON?'paused':'unconfigured',message:c().LIVE_PAUSE_REASON||null},200,cookie);
 const db=database();const now=Date.now();
 if(parts[0]==='jobs'&&parts.length===1&&method==='POST'){
  const str=await request.text();if(str.length>16000)return response({error:{message:'输入过长，请精简'}},413,cookie);const body=JSON.parse(str);const {request:input,estimated}=checkForm(body.form||{});const kind=body.kind==='demo'?'demo':'real';
  if(kind==='real'&&!ready())return response({error:{code:'not_configured',message:c().LIVE_PAUSE_REASON||'真实查询尚未启用：云端调用配置尚未完成。你可以单独查看演示，演示不会伪装成真实查询。'}},503,cookie);
  const key=String(body.idempotency_key||crypto.randomUUID()).slice(0,80);const id=await digest(owner+key);const existing=await db.prepare('SELECT * FROM trips WHERE id=? AND owner=?').bind(id,owner).first<any>();if(existing)return response({job_id:id,status:existing.status,kind:existing.kind},200,cookie);
  const row=kind==='demo'?{status:'partial',result:JSON.stringify(demo(input))}:{status:'starting',result:null};
  const gate=kind==='real'?`AND NOT EXISTS(SELECT 1 FROM trips WHERE kind='real' AND status IN ('starting','searching','stalled')) AND (SELECT COUNT(*) FROM trips WHERE kind='real' AND created>?)<6 AND (SELECT COUNT(*) FROM trips WHERE owner=? AND kind='real' AND created>?)<3`:'';
  const args:any[]=[id,owner,row.status,JSON.stringify(input),row.result,now,now,now+604800000,kind];if(kind==='real')args.push(now-86400000,owner,now-86400000);
  const inserted=await db.prepare(`INSERT INTO trips(id,owner,status,request,result,created,updated,expires,kind) SELECT ?,?,?,?,?,?,?,?,? WHERE 1=1 ${gate}`).bind(...args).run();
  if(!inserted.meta.changes)return response({error:{message:'已有任务正在执行或体验额度已用完，请稍后再试。'}},429,cookie);
  if(kind==='real'){
   try{const s=await createSession();if(!/^sesn_/.test(s.id))throw new Error('百炼未返回有效会话');await db.prepare('UPDATE trips SET session=?,updated=? WHERE id=?').bind(s.id,Date.now(),id).run();await startPlan(s.id,input);await db.prepare('UPDATE trips SET status=?,started=1,updated=? WHERE id=?').bind('searching',Date.now(),id).run();}
   catch(error){await db.prepare('UPDATE trips SET status=?,error=?,updated=? WHERE id=?').bind('failed',safeText((error as Error).message),Date.now(),id).run();return response({job_id:id,status:'failed',kind,error:{message:safeText((error as Error).message)}},200,cookie);}
  }
  return response({job_id:id,status:kind==='demo'?'partial':'searching',kind,estimated_calls:estimated},201,cookie);
 }
 if(parts[0]==='jobs'&&parts[1]&&method==='GET'){
  let row=await db.prepare('SELECT * FROM trips WHERE id=? AND owner=? AND expires>?').bind(parts[1],owner,now).first<any>();if(!row)return response({error:{message:'任务不存在、已过期或不属于当前浏览器'}},404,cookie);
  let steps:any[]=[];
  if(row.status==='searching'&&row.session){
   try{const run=await getSessionResult(row.session);steps=run.steps||[];
    if(run.status!=='searching'){await db.prepare('UPDATE trips SET status=?,result=?,error=?,updated=? WHERE id=?').bind(run.status,run.result?JSON.stringify(run.result):null,run.error||null,Date.now(),row.id).run();row={...row,status:run.status,result:run.result?JSON.stringify(run.result):null,error:run.error};}
    else if(now-row.created>360000){await db.prepare('UPDATE trips SET status=?,error=?,updated=? WHERE id=?').bind('stalled','云端任务等待超过6分钟；不会自动重新发起。维护者需在百炼核对状态。',Date.now(),row.id).run();row.status='stalled';row.error='云端任务等待超过6分钟，需维护者核对。';}
   }catch(error){return response({job_id:row.id,status:'searching',steps:[{message:'暂时无法读取云端状态，稍后继续恢复；没有发起新查询。'}],transient_error:safeText((error as Error).message)},200,cookie);}
  }
  return response({job_id:row.id,status:row.status,kind:row.kind,result:row.result?JSON.parse(row.result):null,error:row.error?{message:row.error}:null,steps},200,cookie);
 }
 if(parts[0]==='plans'&&parts.length===1&&method==='POST'){
  const body=await request.json() as any;const row=await db.prepare('SELECT * FROM trips WHERE id=? AND owner=? AND result IS NOT NULL AND expires>?').bind(body.job_id,owner,now).first<any>();if(!row)return response({error:{message:'请先完成一个属于你的任务'}},404,cookie);
  const result=JSON.parse(row.result);result.plans.forEach((p:any)=>p.members.forEach((m:any,i:number)=>{m.name='成员'+(i+1);delete m.budget;}));
  const share=row.share||crypto.randomUUID();await db.prepare('UPDATE trips SET saved=1,share=?,updated=? WHERE id=?').bind(share,now,row.id).run();return response({share_url:`/index.html?plan=${share}`,share_token:share},200,cookie);
 }
 if(parts[0]==='plans'&&parts.length===1&&method==='GET'){
  const rows=await db.prepare('SELECT id,kind,result,share,created,expires FROM trips WHERE owner=? AND saved=1 AND expires>? ORDER BY created DESC LIMIT 30').bind(owner,now).all<any>();return response({plans:rows.results.map(r=>({id:r.id,kind:r.kind,share:r.share,created_at:new Date(r.created).toISOString(),result:JSON.parse(r.result)}))},200,cookie);
 }
 if(parts[0]==='plans'&&parts[1]&&method==='GET'){
  const row=await db.prepare('SELECT result,kind,expires FROM trips WHERE share=? AND saved=1').bind(parts[1]).first<any>();if(!row)return response({error:{message:'分享方案不存在'}},404,cookie);if(row.expires<now)return response({error:{message:'方案快照已过期，请重新查询'}},410,cookie);
  const result=JSON.parse(row.result);result.plans.forEach((p:any)=>p.members.forEach((m:any,i:number)=>{m.name='成员'+(i+1);delete m.budget;}));return response({result,kind:row.kind,expires_at:new Date(row.expires).toISOString()},200,cookie);
 }
 return response({error:{message:'接口不存在'}},404,cookie);
 }catch(error){return response({error:{message:safeText((error as Error).message)||'服务暂不可用'}},400,cookie);}
}
export const GET=handle;export const POST=handle;
