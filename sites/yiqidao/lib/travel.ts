import { env } from 'cloudflare:workers';
export type Obj = Record<string, any>;
const cfg=()=>env as unknown as Record<string,string>;
export function ready(){const c=cfg();return !!(c.BAILIAN_API_KEY&&c.BAILIAN_HOST&&c.BAILIAN_AGENT_ID&&c.BAILIAN_ENABLE_LIVE==='1');}
export function safeText(v:any){return String(v??'').replace(/sk-[\w.-]{8,}|Bearer\s+[\w.~-]+/gi,'[已隐藏]').slice(0,500);}
export async function upstream(path:string,method='GET',body?:Obj){
 const c=cfg();if(!/^[a-z0-9-]+\.cn-beijing\.maas\.aliyuncs\.com$/.test(c.BAILIAN_HOST||''))throw new Error('百炼接入域名未正确配置');
 const r=await fetch(`https://${c.BAILIAN_HOST}/api/v1/agentstudio${path}`,{method,headers:{Authorization:`Bearer ${c.BAILIAN_API_KEY}`,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined,signal:AbortSignal.timeout(20000)});
 if(!r.ok)throw new Error(r.status===401?'百炼鉴权失败，请联系网站维护者':r.status===429?'百炼限流，请稍后再试':`百炼服务返回 ${r.status}，本次没有使用演示替代`);
 if(path.endsWith('/content')){const text=await r.text();if(text.length>1500000)throw new Error('结果文件过大');return JSON.parse(text);}
 return r.json() as Promise<Obj>;
}
export function checkForm(f:Obj){
 if(!Array.isArray(f.members)||f.members.length<2||f.members.length>4)throw new Error('请填写 2–4 位成员');
 if(!Array.isArray(f.cities)||!f.cities.length||f.cities.length>5)throw new Error('请选择 1–5 个候选城市');
 const today=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
 const days=f.dates||[];if(!days[0]||!days[1]||!/^\d{4}-\d{2}-\d{2}$/.test(days[0])||!/^\d{4}-\d{2}-\d{2}$/.test(days[1])||days[0]<today||days[1]<=days[0])throw new Error('请选择有效的未来出发日期，返程需晚于出发');
 if(days.some((d:string)=>!Number.isFinite(Date.parse(d))||new Date(d+'T00:00:00Z').toISOString().slice(0,10)!==d))throw new Error('日期不存在');
 const duration=(Date.parse(days[1])-Date.parse(days[0]))/86400000;if(!Number.isFinite(duration)||duration>7)throw new Error('体验版支持 1–7 晚的短途规划');
 const hours=Number(f.hours);const maxMinutes=Number(f.maxMinutes||600);if(!Number.isFinite(hours)||hours<1||hours>168||!Number.isFinite(maxMinutes)||maxMinutes<1||maxMinutes>1440)throw new Error('共同停留或交通时长无效');
 if(f.cities.some((v:any)=>typeof v!=='string'||!v.trim()))throw new Error('请填写有效的候选城市');
 const members=f.members.map((m:Obj)=>{const budget=Number(String(m.budget||'').replace(/[^\d.]/g,''));if(typeof m.name!=='string'||!m.name.trim()||typeof m.city!=='string'||!m.city.trim()||!['flight','train'].includes(m.preference)||!Number.isFinite(budget)||budget<=0||budget>100000)throw new Error('请填写每人的称呼、出发城市和有效预算');return {name:String(m.name).slice(0,20),origin_city:String(m.city).slice(0,30),budget,dep_date_start:days[0],dep_date_end:days[0],ret_date_start:days[1],ret_date_end:days[1],modes:m.preference==='flight'?['flight']:['train'],max_oneway_minutes:maxMinutes};});
 const request={members,candidates:f.cities.map((x:any)=>String(x).slice(0,20)),min_common_hours:hours,nights:duration,rooming:{rooms:Math.ceil(members.length/2),nights:duration,note:'按两人一间估算，整房报价未确认，不能据此判断总预算满足'}};
 const estimated=request.candidates.length*(members.length*2+2)+Math.min(3,request.candidates.length);
 if(estimated>Number(cfg().MAX_CALLS_PER_JOB||'20'))throw new Error(`预计 ${estimated} 次旅行查询，超过体验版上限；请减少候选城市。`);
 return {request,estimated};
}
export async function createSession(){const c=cfg();return upstream('/sessions','POST',{agent:c.BAILIAN_AGENT_ID,...(c.BAILIAN_ENVIRONMENT_ID?{environment_id:c.BAILIAN_ENVIRONMENT_ID}:{}),title:'一起到网站：多人会合规划',...(c.FLYAI_API_KEY?{environment_variables:{FLYAI_API_KEY:c.FLYAI_API_KEY}}:{})});}
export async function startPlan(session:string,request:Obj){
 const c=cfg();const max=Math.min(20,Number(c.MAX_CALLS_PER_JOB||20));
 const prompt=`这是一起到网站的已确认请求。使用已挂载 yiqidao Skill 和现有脚本，禁止重写引擎或读取/打印任何凭证。若官方FlyAI CLI未安装，可从官方npm安装@fly-ai/flyai-cli@1.0.16。使用官方CLI当前默认配置或本会话已注入配置；不要检查或提取内置凭证，不读取其他会话或账号配置。禁止设置NODE_TLS_REJECT_UNAUTHORIZED=0或绕过证书校验。仅真实检索；不使用 fixture，不用历史输出冒充结果。请求：${JSON.stringify(request)}\n先验证输入，再用 --mode live --format json --max-calls ${max} --max-seconds 180 --timeout 30 --max-date-pairs 1 --max-plans 3，工作目录 /mnt/session/outputs/web-trip，缓存独立。遇到 401/403/429/451 停止，不绕过风控，不关闭TLS校验。使用plan子命令并显式传入--request /mnt/session/outputs/web-trip/request.json --output /mnt/session/outputs/web-trip/result.json --save-state /mnt/session/outputs/web-trip/state.json --cache-dir /mnt/session/web-trip-cache。输出完整 result.json、request.json、state.json 和 WORKLOG。result.json 必须为 yiqidao 引擎原生JSON，不是Markdown或模型自算JSON。将 /mnt/session/outputs/web-trip/result.json 用 mark_artifacts 登记。酒店缺整房价时 budget_state 保持unknown。不要下单、不要支付。条件已经给出，不重复问已有字段；如需补问在最终简短消息说明。最后只提供结果状态与产物位置，不输出内部推理。`;
 return upstream(`/sessions/${session}/events`,'POST',{input:[{type:'message',role:'user',content:[{type:'text',text:prompt}]}]});
}
function leg(v:Obj|undefined){if(!v)return null;return {mode:v.mode,price:v.price,price_is_missing:v.price_is_missing,total_duration_min:v.total_duration_min,booking_url:validUrl(v.jump_url||v.booking_url),segments:(v.segments||[]).map((s:Obj)=>({transport_no:s.transport_no,dep_city:s.dep_city,arr_city:s.arr_city,dep_station:s.dep_station,arr_station:s.arr_station,dep_time:s.dep_time,arr_time:s.arr_time,duration_min:s.duration_min,seat_class:s.seat_class}))};}
export function validUrl(url:any){try{const u=new URL(String(url));return ['https:','http:'].includes(u.protocol)?u.href:null;}catch{return null;}}
function hotel(h:Obj){return {name:safeText(h.name),address:safeText(h.address),rate:h.rate,display_price:h.display_price,quote_confirmed:!!h.quote_confirmed,booking_url:validUrl(h.jump_url||h.booking_url)};}
export function normalize(raw:Obj){
 if(!Array.isArray(raw.plans))throw new Error('云端结果尚未提供可展示的结构化方案');
 const provenance=(raw.provenance||[]).map((p:Obj)=>({endpoint:p.endpoint,source:p.source,queried_at:p.queried_at,ok:p.ok,n_items:p.n_items,failure_kind:p.failure_kind}));
 if(!provenance.length||provenance.some((p:Obj)=>p.source!=='live'&&!String(p.source).startsWith('cache:')))throw new Error('云端返回了演示来源，已拒绝作为真实结果显示');
 const plans=raw.plans.slice(0,3).map((p:Obj)=>({city:safeText(p.city),date_pair:p.date_pair,common_hours:p.common_hours,common_start:p.common_start,common_end:p.common_end,team_known_cost:p.team_known_cost,team_cost_incomplete:p.team_cost_incomplete,worst_burden_minutes:p.worst_burden_minutes,confirmation_required:(p.confirmation_required||[]).map(safeText),team_unverified:(p.team_unverified||[]).map(safeText),chosen_hotel:p.chosen_hotel?hotel(p.chosen_hotel):null,hotels:(p.hotels||[]).slice(0,6).map(hotel),pois:(p.pois||[]).slice(0,6).map((v:Obj)=>({name:safeText(v.name),address:safeText(v.address),booking_url:validUrl(v.jump_url||v.booking_url)})),members:(p.members||[]).map((m:Obj)=>({name:safeText(m.name),origin:safeText(m.origin),transport_known:m.transport_known,transport_missing:m.transport_missing,total_minutes:m.total_minutes,arrive_at:m.arrive_at,depart_at:m.depart_at,known_cost:m.known_cost,cost_incomplete:m.cost_incomplete,budget_state:m.budget_state,outbound:leg(m.outbound),return:leg(m.ret||m.return),connection_notes:(m.query_notes||m.connection_notes||[]).map(safeText)}))}));
 return {data_kind:'live',engine_status:raw.status,plans,provenance,budget_state:'unknown',needs_confirmation:true,generated_at:new Date().toISOString(),questions:(raw.questions||[]).map(safeText),stats:{live_calls:provenance.filter((p:Obj)=>p.source==='live').length,cached_calls:provenance.filter((p:Obj)=>String(p.source).startsWith('cache')).length},note:'已知费用不等于全程总价；可订性、酒店整房价及结算价需核实。'};
}
export async function getSessionResult(session:string){
 const state=await upstream(`/sessions/${session}`);const history=await upstream(`/sessions/${session}/events?order=desc&limit=50`);const events=history.data||[];
 const stages=events.filter((e:Obj)=>e.type==='tool_call'||e.type==='tool_call_output'||e.type==='message').slice(0,10).reverse().map((e:Obj)=>({id:e.id,message:e.type==='tool_call'?'智能体正在执行规划工具':e.type==='tool_call_output'?'规划工具已返回，正在整理结果':'智能体已更新任务状态',at:e.created_at}));
 const statusEvent=events.find((e:Obj)=>e.type==='session_status');const detail=statusEvent?.content?.[0]?.data||{};const reason=state.stop_reason?.type||detail.stop_reason?.type;
 const done=state.status==='terminated'||(state.status==='idle'&&(reason==='end_turn'||reason==='requires_action'||reason==='error'||reason==='max_iterations'));
 if(!done)return {status:'searching',steps:stages};
 const files=await upstream(`/files?scope_type=session&scope_id=${session}&limit=100`);
 const arr=files.data||files.items||[];const file=arr.find((f:Obj)=>/(^|\/)result\.json$/.test(f.name||f.filename||f.path||''));
 if(!file){const texts=events.filter((e:Obj)=>e.type==='message'&&e.role==='assistant').flatMap((e:Obj)=>(e.content||[]).filter((c:Obj)=>c.type==='text').map((c:Obj)=>safeText(c.text)));return {status:'failed',error:reason==='requires_action'?'云端工具需要维护者确认，当前网站不能自动批准':texts[0]||'智能体完成了本轮，但未登记 result.json，请维护者核对技能输出。',steps:stages};}
 const raw=await upstream(`/files/${encodeURIComponent(file.id)}/content`);
 if(raw.status==='error'){const blocked=(raw.provenance||[]).find((p:Obj)=>p.failure_kind==='risk'||p.failure_kind==='auth'||/429|451/.test(p.failure||''));return {status:'failed',error:blocked?'旅行数据接口限流或鉴权失败，本轮已停止；请维护者核对后再启用查询。':safeText(raw.error||'真实规划未成功，请核对查询条件和上游状态'),steps:stages};}
 const result=normalize(raw);return {status:result.plans.length?'partial':'completed',result,steps:stages};
}
export function demo(request:Obj){
 const dates=[request.members[0].dep_date_start,request.members[0].ret_date_start];const plans=request.candidates.slice(0,3).map((city:string,i:number)=>({city,date_pair:{dep_date:dates[0],ret_date:dates[1]},common_hours:(Date.parse(dates[1])-Date.parse(dates[0]))/3600000+6,team_known_cost:request.members.reduce((s:number,_m:Obj,j:number)=>s+600+j*100+i*40,0),team_cost_incomplete:true,worst_burden_minutes:240,confirmation_required:['演示数据，不可预订','住宿、餐饮、接驳与门票费用未确认'],hotels:[{name:city+'示例住宿',address:'示例地址，真实房型尚未核实',quote_confirmed:false,booking_url:null}],pois:[],members:request.members.map((m:Obj,j:number)=>{const cost=600+j*100+i*40;const make=(ret:boolean)=>({mode:m.modes[0],price:cost/2,price_is_missing:false,total_duration_min:120,booking_url:null,segments:[{transport_no:'DEMO-'+(ret?'R':'G')+(i+1)+(j+1),dep_station:(ret?city:m.origin_city)+'示例站',arr_station:(ret?m.origin_city:city)+'示例站',dep_time:(ret?dates[1]:dates[0])+'T'+(ret?'16':'08')+':00:00',arr_time:(ret?dates[1]:dates[0])+'T'+(ret?'18':'10')+':00:00',seat_class:'演示席位'}]});return {name:m.name,origin:m.origin_city,total_minutes:240,transport_known:cost,known_cost:cost,cost_incomplete:true,arrive_at:dates[0]+'T10:00:00',depart_at:dates[1]+'T16:00:00',outbound:make(false),return:make(true),connection_notes:[]};})}));
 return {data_kind:'mock',plans,budget_state:'unknown',needs_confirmation:true,generated_at:new Date().toISOString(),provenance:[{endpoint:'demo',source:'mock',queried_at:null}],stats:{live_calls:0,cached_calls:0},note:'独立演示通道，所有班次与金额都是虚构数据，没有调用百炼或 FlyAI。'};
}
