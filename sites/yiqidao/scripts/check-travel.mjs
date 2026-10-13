import fs from 'node:fs/promises';
import assert from 'node:assert/strict';
import ts from 'typescript';
const source=(await fs.readFile(new URL('../lib/travel.ts',import.meta.url),'utf8')).replace("import { env } from 'cloudflare:workers';","const env={BAILIAN_HOST:'test.cn-beijing.maas.aliyuncs.com',BAILIAN_API_KEY:'test-placeholder',BAILIAN_AGENT_ID:'agent_test',BAILIAN_ENABLE_LIVE:'1'};");
const js=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText;
const travel=await import('data:text/javascript;base64,'+Buffer.from(js).toString('base64'));
const future=new Date();future.setUTCDate(future.getUTCDate()+10);const dep=future.toISOString().slice(0,10);future.setUTCDate(future.getUTCDate()+2);const ret=future.toISOString().slice(0,10);
const form={members:[{name:'A',city:'北京',budget:'¥3,000',preference:'flight'},{name:'B',city:'上海',budget:3000,preference:'train'}],dates:[dep,ret],cities:['杭州','南京'],hours:30};
const {request,estimated}=travel.checkForm(form);assert.equal(estimated,14);assert.deepEqual(request.members.map(m=>m.modes),[['flight'],['train']]);
assert.throws(()=>travel.checkForm({...form,dates:['2027-02-30','2027-03-02']}));
assert.throws(()=>travel.checkForm({...form,hours:'abc'}));
assert.throws(()=>travel.checkForm({...form,members:form.members.map(m=>({...m,preference:'fake'}))}));
assert.throws(()=>travel.checkForm({...form,cities:['杭州','南京','苏州','武汉','黄山']}),/超过体验版上限/);
const demo=travel.demo(request);assert.equal(demo.data_kind,'mock');assert.equal(demo.stats.live_calls,0);
for(const plan of demo.plans){const start=Math.max(...plan.members.map(m=>Date.parse(m.arrive_at)));const end=Math.min(...plan.members.map(m=>Date.parse(m.depart_at)));assert.equal(plan.common_hours,(end-start)/3600000);assert.equal(plan.worst_burden_minutes,Math.max(...plan.members.map(m=>m.total_minutes)));}
assert.throws(()=>travel.normalize({plans:[],provenance:[]}));
assert.throws(()=>travel.normalize({plans:[],provenance:[{source:'fixture:test'}]}));
const raw={status:'needs_confirmation',plans:[{city:'杭州',members:[{name:'A',transport_known:null,transport_missing:true,ret:{price:null,segments:[],raw:{secret:'never expose'}}}],hotels:[]}],provenance:[{endpoint:'search-train',source:'live',queried_at:'2026-10-13',raw:{private:'hidden'}}]};
const result=travel.normalize(raw);assert.equal(result.plans[0].members[0].transport_known,null);assert.equal(result.plans[0].members[0].return.price,null);assert.equal(result.provenance[0].raw,undefined);assert.equal(result.plans[0].members[0].return.raw,undefined);assert.equal(result.budget_state,'unknown');
assert.equal(travel.validUrl('javascript:alert(1)'),null);
console.log('Travel adapter checks passed: input, query budget, demo arithmetic, provenance, missing prices and redaction.');

assert.equal(travel.ready(),true);
const originalFetch=globalThis.fetch;let sessionBody;
globalThis.fetch=async(url,options)=>{sessionBody=JSON.parse(options.body);return new Response(JSON.stringify({id:'sesn_test'}));};
await travel.createSession();assert.equal(sessionBody.environment_id,undefined);assert.equal(sessionBody.environment_variables,undefined);
globalThis.fetch=async(url)=>{let value;if(url.endsWith('/sesn_test'))value={status:'idle',stop_reason:{type:'end_turn'}};else if(url.includes('/events?'))value={data:[]};else if(url.includes('/files?'))value={data:[{id:'file_test',name:'result.json'}]};else value={status:'error',plans:[],provenance:[{source:'live',failure_kind:'risk',failure:'HTTP 429'},{source:'not-called',ok:false}]};return new Response(JSON.stringify(value));};
const limited=await travel.getSessionResult('sesn_test');assert.equal(limited.status,'failed');assert.match(limited.error,/限流/);
globalThis.fetch=originalFetch;console.log('Default environment and upstream rate-limit handling checks passed.');
