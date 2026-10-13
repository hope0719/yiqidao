'use strict';
const $ = (s, root=document) => root.querySelector(s);
const $$ = (s, root=document) => [...root.querySelectorAll(s)];
let flowTimer;
let activeView = 1;
const memberGrid = $('#view-1 .grid');
memberGrid.id = 'members';
const memberTemplate = memberGrid.children[1].cloneNode(true);
const memberHeading = $('#view-1 h2');
const addButton = $$('#view-1 button').find(b => b.textContent.includes('添加成员'));
const dates = $$('#view-1 input').filter(i=>/^2025-/.test(i.value));
dates.forEach(i => { i.type = 'date'; i.setAttribute('aria-label', i === dates[0] ? '出发日期' : '返程日期'); });
const range = $('#view-1 input[type=range]');
const rangeLabel = range.parentElement.previousElementSibling.lastElementChild;
const cityButtons = $$('#view-1 button').filter(b => ['杭州','南京','苏州','武汉','黄山'].includes(b.textContent.trim().replace('check','').trim()));
const customCity = $$('#view-1 button').find(b => b.textContent.includes('自定义添加'));
const storage = {
  read(key, fallback) { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } },
  write(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); return true; } catch { toast('浏览器存储不可用，请导出行程备份'); return false; } }
};
function toast(message) {
  $('#toast')?.remove();
  const el = document.createElement('div'); el.id='toast'; el.role='status'; el.textContent=message;
  document.body.append(el); setTimeout(()=>el.remove(),3200);
}
function showDialog(title, text) {
  const dialog = $('#info-dialog'); $('h2',dialog).textContent=title; $('p',dialog).textContent=text; dialog.showModal();
}
function switchView(index) {
  clearTimeout(flowTimer);
  activeView = Math.min(5,Math.max(1,Number(index)||1));
  for(let i=1;i<=5;i++) {
    $('#view-'+i).classList.toggle('hidden',i!==activeView);
    const tab=$('#tab-btn-'+i); tab.classList.toggle('bg-primary',i===activeView); tab.classList.toggle('text-on-primary',i===activeView); tab.classList.toggle('text-on-surface-variant',i!==activeView);
    tab.setAttribute('aria-selected', String(i===activeView));
  }
  $$('.mobile-nav button').forEach((b,i)=> b.classList.toggle('selected',[1,3,5][i]===activeView));
  if(activeView===5) renderSaved();
  updateSummary();
  history.replaceState(null,'','#step-'+activeView);
  window.scrollTo({top:0,behavior:'instant'});
}
function updateMembers() {
  memberHeading.textContent=`出行成员 (${memberGrid.children.length}/4)`;
  $('span:last-child',addButton).textContent=`添加成员 (当前${memberGrid.children.length}人，上限4人)`;
  addButton.disabled = memberGrid.children.length >= 4;
  [...memberGrid.children].forEach((card,i)=> {
    $('span',card).textContent=i===0?'成员 1 · 发起人':`成员 ${i+1}`;
    const remove=$('button',card);
    if(remove) remove.onclick=()=> { if(memberGrid.children.length<=2) return toast('至少需要两位出行成员'); card.remove(); updateMembers(); saveDraft(); };
    $$('input',card).forEach((input,j)=>input.setAttribute('aria-label',`成员${i+1}${['称呼','出发城市','预算'][j]}`));
  });
}
function addMember(name='',city='',budget='¥3,000') {
  if(memberGrid.children.length>=4) return;
  const card=memberTemplate.cloneNode(true); const inputs=$$('input',card);
  [name,city,budget].forEach((v,i)=>inputs[i].value=v);
  memberGrid.append(card); updateMembers();
}
addButton.onclick=()=> {addMember(`好友${memberGrid.children.length+1}`); saveDraft();};
function getRequest() {
  return {members:[...memberGrid.children].map(c=>({name:$$('input',c)[0].value,city:$$('input',c)[1].value,budget:$$('input',c)[2].value,preference:$('select',c).value})), dates:dates.map(i=>i.value),hours:range.value,cities:cityButtons.filter(b=>b.getAttribute('aria-pressed')==='true').map(b=>$('span',b).textContent), preferences:$$('#view-1 details select').map(s=>s.value)};
}
function saveDraft() { storage.write('yiqidao-draft',getRequest()); }
function applyRequest(r) {
  while(memberGrid.children.length>2) memberGrid.lastElementChild.remove();
  r.members?.slice(0,4).forEach((m,i)=> {
    if(i>=2) addMember();
    const card=memberGrid.children[i]; [m.name,m.city,m.budget].forEach((v,j)=>$$('input',card)[j].value=v);
    if(m.preference) $('select',card).value=m.preference;
  });
  r.dates?.forEach((v,i)=>dates[i].value=v); range.value=r.hours||42; rangeLabel.textContent=`≥ ${range.value} 小时`;
  if(r.cities) cityButtons.forEach(b=>setCity(b,r.cities.includes($('span',b).textContent)));
  r.preferences?.forEach((v,i)=>$$('#view-1 details select')[i].value=v);
  updateMembers();
}
function quickFillPrompt() {
  const text=$('#nlp-input').value;
  const known=['北京','上海','广州','深圳','成都','重庆','武汉','南京','杭州','西安','长沙','苏州'];
  const cities=known.filter(c=>text.includes(c)).sort((a,b)=>text.indexOf(a)-text.indexOf(b));
  if(cities.length<2) return toast('请描述至少两个出发城市，例如：我在北京，朋友在上海');
  const budget=text.match(/预算\s*(\d+)/)?.[1]||'3000';
  applyRequest({members:cities.slice(0,4).map((city,i)=>({name:i===0?'小林 (我)':`好友${i+1}`,city,budget:`¥${Number(budget).toLocaleString()}`}))});
  saveDraft(); toast(`已识别 ${Math.min(cities.length,4)} 个城市和每人预算；请确认日期与交通偏好`);
}
function setPreset(name) {
  const presets={'京沪周末聚':'我在北京，朋友在上海，想一起出去玩两晚，每人预算3000元。','四城毕业游':'我们分别在广州、武汉、南京和成都，想一起出去玩两晚，每人预算3000元。','广深成渝会合':'我们分别在广州、深圳、成都和重庆，想一起出去玩两晚，每人预算3000元。'};
  $('#nlp-input').value=presets[name]; quickFillPrompt();
}
function setCity(b,selected) { b.setAttribute('aria-pressed',String(selected)); b.classList.toggle('bg-primary',selected); b.classList.toggle('text-on-primary',selected); b.classList.toggle('bg-surface-container-low',!selected); const icon=$('.material-symbols-outlined',b); if(icon) icon.style.display=selected?'':'none'; }
cityButtons.forEach(b=> {setCity(b,b.classList.contains('bg-primary')); b.onclick=()=>{setCity(b,b.getAttribute('aria-pressed')!=='true');saveDraft();};});
customCity.onclick=()=>showDialog('示例城市范围','当前展示杭州与南京的设计示例。添加其他目的地和真实查询需要接入旅行数据服务。');
range.oninput=()=>{rangeLabel.textContent=`≥ ${range.value} 小时`;saveDraft();};
$('#view-1').addEventListener('change',saveDraft);
function startCalculatingFlow() {
  const r=getRequest();
  if(r.members.some(m=>!m.name.trim()||!m.city.trim()||Number(m.budget.replace(/[^\d.]/g,''))<=0)) return toast('请填写每位成员的称呼、城市和有效预算');
  if(!r.dates[0]||!r.dates[1]||r.dates[1]<=r.dates[0]) return toast('返程日期需要晚于出发日期');
  if(!r.cities.length) return toast('请至少选择一个候选城市');
  saveDraft();
  $('#view-2 .font-title-sm').textContent=r.members.map(m=>`${m.city} (${m.name})`).join(' ⇄ ');
  $('#view-2 .font-label-md').textContent=`${r.dates.join(' 至 ')} · 示例流程 · 候选：${r.cities.join('、')}`;
  switchView(2); flowTimer=setTimeout(()=>switchView(3),1800);
}
function saveCurrentPlan() {
  const plans=storage.read('yiqidao-saved',[]);
  const plan={id:Date.now(),created:new Date().toISOString(),request:getRequest(),title:'杭州会合方案 · 设计示例'};
  plans.unshift(plan); if(storage.write('yiqidao-saved',plans)) {toast('已保存到当前浏览器');switchView(5);}
}
function renderSaved() {
  const grid=$('#view-5 .grid'); grid.replaceChildren();
  const plans=storage.read('yiqidao-saved',[]);
  const count=$('header [data-path="my-plans"] span:last-child'); count.textContent=plans.length;
  if(!plans.length) { const empty=document.createElement('p'); empty.className='empty-state';empty.textContent='还没有保存的方案。查看杭州示例详情，点击「保存方案」即可在这里找到。';grid.append(empty);return; }
  plans.forEach(plan=> {
    const card=document.createElement('article');card.className='saved-card';
    const title=document.createElement('h3');title.textContent=plan.title;
    const desc=document.createElement('p');desc.textContent=plan.request.members.map(m=>`${m.city} (${m.name})`).join(' ＋ ');
    const date=document.createElement('p');date.textContent=plan.request.dates.join(' 至 ');
    const note=document.createElement('small');note.textContent='已保存输入条件与杭州示例；交通和价格未实时查询。';
    const actions=document.createElement('div');actions.className='saved-actions';
    [['查看示例',()=>switchView(4)],['再编辑',()=>{applyRequest(plan.request);saveDraft();switchView(1);}],['删除',()=>{storage.write('yiqidao-saved',plans.filter(p=>p.id!==plan.id));renderSaved();}]].forEach(([label,fn])=>{const b=document.createElement('button');b.textContent=label;b.onclick=fn;actions.append(b);});
    card.append(title,desc,date,note,actions);grid.append(card);
  });
}
async function copyShareLink() {
  try { await navigator.clipboard.writeText(location.href.split('#')[0]+'#step-4');toast('已复制杭州设计示例链接；本地保存的条件不会随链接传递'); } catch { showDialog('分享示例链接',location.href.split('#')[0]+'#step-4'); }
}
function exportPlanImage() {
  const c=document.createElement('canvas');c.width=1080;c.height=1260;const ctx=c.getContext('2d');
  ctx.fillStyle='#f0f3ff';ctx.fillRect(0,0,c.width,c.height);ctx.fillStyle='#00685f';ctx.fillRect(0,0,1080,200);
  ctx.fillStyle='#fff';ctx.font='bold 54px sans-serif';ctx.fillText('一起到 · 杭州会合行程',60,115);
  ctx.fillStyle='#111c2d';ctx.font='32px sans-serif';
  ['设计示例 · 非实时票价或可预订方案','北京（小林）＋ 上海（阿泽）','示例日期：2025年5月16日 — 5月18日','周五：各自出发，在杭州东站会合','示例到达：阿泽 20:43 / 小林 21:08','周六：西湖漫步，湖滨商圈相聚','周日：自由活动，各自返程','所有车次、价格与酒店均需出行前核实'].forEach((line,i)=>ctx.fillText(line,60,290+i*100));
  c.toBlob(blob=>{const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='一起到-杭州设计示例.png';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('行程示例图片已导出');});
}
const dialog=document.createElement('dialog');dialog.id='info-dialog';dialog.innerHTML='<h2></h2><p></p><form method="dialog"><button>知道了</button></form>';document.body.append(dialog);
const mobile=document.createElement('nav');mobile.className='mobile-nav';mobile.setAttribute('aria-label','主导航');
[['near_me','会合规划',1],['balance','城市比选',3],['bookmark_border','我的方案',5]].forEach(([icon,label,index])=>{const b=document.createElement('button');b.innerHTML=`<span class="material-symbols-outlined">${icon}</span><span>${label}</span>`;b.onclick=()=>switchView(index);mobile.append(b);});document.body.append(mobile);
document.addEventListener('click',e=>{const a=e.target.closest('[data-path]');if(!a)return;e.preventDefault();const path=a.dataset.path;if(path==='help')showDialog('使用帮助与隐私','填写2–4位成员的城市、日期和预算，分别选择真实查询或演示。方案保存7天；分享称呼匿名化，出发城市与班次仍会展示。价格、库存和住宿需在预订页核实。');else switchView(path==='my-plans'?5:1);});
$$('button[onclick^="alert"]').forEach(b=>{b.removeAttribute('onclick');b.onclick=()=>showDialog('其他城市示例','这份设计仅提供杭州的完整方案详情。其他城市尚未提供完整行程数据。');});
const home=$('header button');if(home)home.onclick=()=>showDialog('常居地','出发城市以规划表单中每位成员填写的城市为准。');
const draft=storage.read('yiqidao-draft',null);if(draft)applyRequest(draft);updateMembers();
renderSaved();switchView(Number(location.hash.match(/(?:view|step)-(\d)/)?.[1])||1);
window.addEventListener('hashchange',()=>switchView(Number(location.hash.match(/(?:view|step)-(\d)/)?.[1])||1));

// Planning workspace: retain existing form controls and storage behavior.
function enhancePlanningViews() {
  const section=$('#view-1'); section.classList.add('planning-workspace');
  const hero=section.children[0],form=section.children[1];
  hero.className='planning-intro';
  hero.children[0].remove();
  $('h1',hero).innerHTML='让下一次相聚，<span>对每个人都轻松。</span>';
  $('p',hero).textContent='告诉我们朋友从哪里出发，一起挑一个好会合的城市。';
  const nlp=$('#nlp-input').closest('.rounded-2xl'); nlp.className='quick-plan';
  const quickHeader=document.createElement('div');quickHeader.className='quick-heading';quickHeader.innerHTML='<span class="material-symbols-outlined">auto_awesome</span><strong>先说说你们的计划</strong><span>也可以直接填写下方表单</span>';nlp.prepend(quickHeader);
  const input=$('#nlp-input'),textarea=document.createElement('textarea');textarea.id=input.id;textarea.value=input.value;textarea.placeholder=input.placeholder;textarea.rows=2;textarea.setAttribute('aria-label','用一句话描述出行计划');input.replaceWith(textarea);
  const nlpRow=textarea.parentElement.parentElement;nlpRow.classList.add('quick-entry');
  $('button',nlpRow).innerHTML='<span class="material-symbols-outlined">auto_awesome</span><span>解析并填入</span>';
  nlpRow.nextElementSibling.classList.add('quick-presets');
  hero.after(nlp);
  form.className='planning-form';
  const members=form.children[0],common=form.children[1],submit=form.children[2];
  members.classList.add('form-section');common.className='form-section common-section';
  $('h2',common).textContent='共同行程';
  $$('.w-2\\.5',form).forEach(el=>el.remove());
  members.firstElementChild.classList.add('section-heading');common.firstElementChild.classList.add('section-heading');
  const timeGrid=common.children[1];timeGrid.className='time-settings';
  const dateCard=timeGrid.children[0],hoursCard=timeGrid.children[1];dateCard.classList.add('time-card');hoursCard.classList.add('time-card');
  dateCard.firstElementChild.lastElementChild.textContent='选择出发与返程日期';
  dateCard.lastElementChild.classList.add('date-inputs');
  dates.forEach((date,i)=> {const wrap=document.createElement('label');wrap.className='date-field';const caption=document.createElement('span');caption.textContent=i?'返程':'出发';date.before(wrap);wrap.append(caption,date);});
  const destination=common.children[2];destination.className='destination-section';destination.firstElementChild.textContent='想在哪里会合？';
  const hint=document.createElement('p');hint.className='field-hint';hint.textContent='可多选，先比较大家感兴趣的城市。';destination.firstElementChild.after(hint);
  const layout=document.createElement('div');layout.className='planning-layout';form.before(layout);layout.append(form);
  const summary=document.createElement('aside');summary.className='plan-summary';summary.innerHTML='<div class="summary-heading"><span class="material-symbols-outlined">travel_explore</span><h2>这次相聚</h2></div><p class="summary-caption">确认条件，就可以开始比较</p><div id="summary-members"></div><dl><div><dt>行程时间</dt><dd id="summary-dates"></dd></div><div><dt>共同停留</dt><dd id="summary-hours"></dd></div><div><dt>候选城市</dt><dd id="summary-cities"></dd></div></dl><div class="summary-note"><span class="material-symbols-outlined">info</span><p>当前为示例体验，展示杭州与南京的固定方案。</p></div>';
  layout.append(summary);submit.className='plan-submit';summary.append(submit);
  const submitButton=$('button',submit);submitButton.innerHTML='<span>帮我们找会合城市</span><span class="material-symbols-outlined">arrow_forward</span>';submitButton.setAttribute('aria-label','帮我们找会合城市 (开始比较)');$('p',submit).textContent='查看示例比较结果';
  addButton.classList.add('add-member');
  const stepNames=['填写计划','准备方案','比较城市','行程详情','我的方案'];stepNames.forEach((name,i)=>$('#tab-btn-'+(i+1)).innerHTML=`<span class="step-number">${i+1}</span><span>${name}</span>`);
  const bar=$('main>div>aside');bar.classList.add('workflow-nav');bar.firstElementChild.firstElementChild.innerHTML='<span>聚会规划</span>';bar.firstElementChild.lastElementChild.textContent='';
  $('#view-2').innerHTML=`<div class="waiting-layout"><div class="waiting-header"><span class="waiting-badge">示例体验</span><h1>好久不见，<br>这次在哪儿见？</h1><p>我们把出发条件整理在一起，<br>带你看看一份会合方案如何比较。</p></div><div class="waiting-card"><div class="meeting-visual" aria-hidden="true"><div class="origin-node"><span class="material-symbols-outlined">train</span></div><div class="meeting-path"></div><div class="meeting-center"><span class="material-symbols-outlined">handshake</span></div><div class="meeting-path"></div><div class="origin-node"><span class="material-symbols-outlined">train</span></div></div><h2>正在准备会合方案</h2><p class="waiting-subtitle">示例流程约需 2 秒，完成后自动进入城市比较</p><div class="waiting-request"><span class="material-symbols-outlined">group</span><div><strong class="font-title-sm" id="waiting-members">北京 (小林) ⇄ 上海 (阿泽)</strong><p class="font-label-md" id="waiting-dates">请选择行程条件</p></div></div><ol class="waiting-steps"><li class="done"><span class="step-icon material-symbols-outlined">check</span><div><strong>整理出行条件</strong><p>出发地、预算与共同停留时间</p></div><span class="step-state">已就绪</span></li><li class="current"><span class="step-icon material-symbols-outlined">progress_activity</span><div><strong>准备城市比较</strong><p>展示交通耗时与到达时间差示例</p></div><span class="step-state">准备中</span></li><li><span class="step-icon">3</span><div><strong>汇总相聚行程</strong><p>查看会合地点与行程安排</p></div><span class="step-state">下一步</span></li></ol><div class="waiting-actions"><button onclick="switchView(1)"><span class="material-symbols-outlined">arrow_back</span>返回修改条件</button><button onclick="switchView(3)">直接查看示例<span class="material-symbols-outlined">arrow_forward</span></button></div><p class="waiting-disclaimer">本次未调用实时交通、酒店或价格查询接口</p></div></div>`;
  const mobileStart=document.createElement('div');mobileStart.className='mobile-plan-start';mobileStart.innerHTML='<span id="mobile-plan-count"></span><button onclick="startCalculatingFlow()">开始比较<span class="material-symbols-outlined">arrow_forward</span></button>';section.append(mobileStart);
  updateSummary();
}
function updateSummary() {
  if(!$('#summary-members')) return;
  const r=getRequest();$('#mobile-plan-count').textContent=`${r.members.length} 位好友 · ${r.cities.length} 个候选城市`;$('#summary-members').replaceChildren();
  r.members.forEach((m,i)=>{const row=document.createElement('div');row.className='summary-member';const icon=document.createElement('span');icon.className='summary-avatar';icon.textContent=String(i+1);const info=document.createElement('div');const name=document.createElement('strong');name.textContent=m.city||'待填写出发城市';const sub=document.createElement('span');sub.textContent=m.name||'待填写称呼';info.append(name,sub);const budget=document.createElement('span');budget.className='summary-budget';budget.textContent=m.budget||'待填写预算';row.append(icon,info,budget);$('#summary-members').append(row);});
  $('#summary-dates').textContent=r.dates.every(Boolean)?r.dates.map(d=>d.slice(5).replace('-','月')+'日').join(' — '):'待选择';
  $('#summary-hours').textContent=`至少 ${r.hours} 小时`;$('#summary-cities').textContent=r.cities.join('、')||'待选择';
  if($('#waiting-members')) $('#waiting-members').textContent=r.members.map(m=>`${m.city||'待填写'} (${m.name||'好友'})`).join(' ⇄ ');
  if($('#waiting-dates')) $('#waiting-dates').textContent=`${r.dates.join(' 至 ')} · 候选：${r.cities.join('、')}`;
}
enhancePlanningViews();
$('#view-1').addEventListener('input',updateSummary);
$('#view-1').addEventListener('click',()=>queueMicrotask(updateSummary));
$('#view-1').addEventListener('change',updateSummary);
