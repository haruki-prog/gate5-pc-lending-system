'use strict';
const $ = id => document.getElementById(id);
let data, csrf, messages = {}, pending = null, busy = false, currentView = 'lend', storageKey;
const fieldIds = {device_id: 'device-error', due_date: 'date-error', purpose: 'purpose-error'};
const unknownMessage = '処理結果を確認できません。再申請せず、「結果を確認する」を押してください。';
const dateText = value => value ? value.replaceAll('-', '/') : '—';
const timeText = value => new Intl.DateTimeFormat('ja-JP', {timeZone:'Asia/Tokyo', month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}).format(new Date(value));
function el(tag, text, cls) { const node=document.createElement(tag); if(text!==undefined) node.textContent=text; if(cls) node.className=cls; return node; }
function savePending(value) { pending=value; if(value) sessionStorage.setItem(storageKey,JSON.stringify(value)); else sessionStorage.removeItem(storageKey); }
function saveDraft() { if(storageKey) sessionStorage.setItem(storageKey+'-draft',JSON.stringify(draft())); }
function draft() { return {device_id:$('device_id').value,due_date:$('due_date').value,purpose:$('purpose').value}; }
function clearErrors() { $('notice').hidden=true; $('notice').className='notice'; $('recovery').hidden=true; $('check-result').textContent='結果を確認する'; delete $('check-result').dataset.retry; for(const [id,errorId] of Object.entries(fieldIds)){ $(errorId).hidden=true; $(id).removeAttribute('aria-invalid'); } }
function notice(message, info=false) { $('notice').textContent=message; $('notice').className='notice'+(info?' info':''); $('notice').hidden=false; }
function showError(error) {
  clearErrors(); notice(error.message || unknownMessage);
  const entries=Object.entries(error.fields||{});
  for(const [name,value] of entries) if(fieldIds[name]) { $(fieldIds[name]).textContent=value.message; $(fieldIds[name]).hidden=false; $(name).setAttribute('aria-invalid','true'); }
  if(entries.length && fieldIds[entries[0][0]]) $(entries[0][0]).focus(); else $('notice').focus();
  if(error.code==='E-08'||error.code==='E-20') {
    $('recovery').hidden=false; $('relogin').hidden=error.code!=='E-08'; $('check-result').hidden=!pending;
  }
}
async function api(path, body) {
  const controller=new AbortController(), timeout=setTimeout(()=>controller.abort(),12000);
  try {
    const response=await fetch(path,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:body===undefined?undefined:JSON.stringify(body),signal:controller.signal});
    const result=await response.json(); if(!response.ok||!result.ok) throw result; return result;
  } catch(error) { if(error.code) throw error; throw {code:'E-20',message:unknownMessage}; }
  finally { clearTimeout(timeout); }
}
function details(container, rows) { const nodes=rows.map(([label,value])=>{const row=el('div');row.append(el('dt',label),el('dd',value));return row;}); $(container).replaceChildren(...nodes); }
function step(stage) { ['input','confirm','complete'].forEach(x=>$('step-'+x).classList.toggle('current',x===stage)); }
function view(name) {
  currentView=name;
  $('lend-view').hidden=name!=='lend'; $('return-view').hidden=name!=='return'; $('complete-view').hidden=name!=='complete';
  $('nav-lend').classList.toggle('active',name==='lend'); $('nav-return').classList.toggle('active',name==='return');
  $('page-title').textContent=name==='return'?'PCを返す':name==='complete'?'手続きが完了しました':'PCを借りる';
  $('page-description').textContent=name==='return'?'返却するPCを選んで、返却処理を行ってください。':name==='complete'?'処理結果と貸出状況をご確認ください。':'利用したいPCを選んで、貸出を申請してください。';
}
function inputPanel() { view('lend');$('input-panel').hidden=false;$('confirm-panel').hidden=true;step('input'); }
function renderDashboard(next) {
  data=next; const old=$('device_id').value;
  $('user-name').textContent=data.user.name;$('user-department').textContent=data.user.department;$('avatar').textContent=data.user.name[0];
  $('today').textContent=dateText(data.today);$('available-count').textContent=data.counts.available;$('mine-count').textContent=data.counts.mine;$('overdue-count').textContent=data.counts.overdue;
  const options=[new Option(data.devices.length?'PCを選択してください':'現在貸出可能な端末はありません',''),...data.devices.map(d=>new Option(`${d.model_name}（${d.asset_no}）`,String(d.id)))];
  $('device_id').replaceChildren(...options);$('device_id').value=old;
  $('device_id').disabled=!data.devices.length;
  $('due_date').min=data.min_date;$('due_date').max=data.max_date;
  $('prepare-button').disabled=busy||!!data.lend_notice||!data.devices.length||!!pending;
  renderLoans();renderHistory();
}
function renderLoans() {
  if(!data.loans.length){$('loans').replaceChildren(el('p',messages['I-01'],'empty'));return;}
  $('loans').replaceChildren(...data.loans.map(loan=>{
    const box=el('article',undefined,'loan-card'),head=el('div',undefined,'loan-header'),title=el('h3',loan.asset_no);
    title.append(el('small',loan.model_name));head.append(title,el('span',loan.blocked?'確認が必要':loan.overdue?'延滞':'貸出中','badge'+(loan.overdue||loan.blocked?' warning':'')));box.append(head);
    const dl=el('dl',undefined,'details');for(const [label,value] of [['貸出日時',timeText(loan.lent_at)],['返却予定日',dateText(loan.due_date)],['利用目的',loan.purpose]]){const row=el('div');row.append(el('dt',label),el('dd',value));dl.append(row);}box.append(dl);
    const foot=el('div',undefined,'loan-footer'),text=loan.blocked?messages['E-06']:loan.overdue?`延滞（返却予定日：${dateText(loan.due_date)}）`:'お手元のPCの資産番号を確認してください。';foot.append(el('p',text));
    const button=el('button','返却する','button primary');button.disabled=busy||!!pending||loan.blocked;button.addEventListener('click',()=>returnLoan(loan));foot.append(button);box.append(foot);return box;
  }));
}
function renderHistory() {
  if(!data.history.length){$('history').replaceChildren(el('p','完了した操作はまだありません。','empty'));return;}
  $('history').replaceChildren(...data.history.map(h=>{
    const row=el('div',undefined,'history-row');row.append(el('span',h.operation==='LEND'?'貸出':'返却','badge'));
    const button=el('button',`${h.result.asset_no} · ${h.result.model_name}`,'text-button');button.addEventListener('click',()=>run(button,async()=>{if(pending){notice('処理中の操作を先に確認してください。');return;}const r=await api('/api/results/'+h.request_key);await complete({...r.result,request_key:h.request_key});}));
    row.append(button,el('time',timeText(h.completed_at)));return row;
  }));
}
async function refresh() { renderDashboard(await api('/api/dashboard')); }
async function run(button, action) {
  if(busy)return;busy=true;const label=button.textContent;button.disabled=true;button.textContent='処理中…';
  try { await action(); } catch(error){ showError(error); }
  finally { busy=false;button.textContent=button.id==='check-result'&&button.dataset.retry==='true'?'同じ操作を再試行':label;button.disabled=false;if(data){$('prepare-button').disabled=!!data.lend_notice||!data.devices.length||!!pending;renderLoans();} }
}
function confirmation() {
  view('lend');$('input-panel').hidden=true;$('confirm-panel').hidden=false;step('confirm');
  const p=pending.display;details('confirm-details',[['機種',`${p.model_name}（${p.asset_no}）`],['借用者',data.user.name],['返却予定日',dateText(p.due_date)],['利用目的',p.purpose]]);
}
async function complete(result) {
  savePending(null);clearErrors();view('complete');
  $('complete-title').textContent=result.operation==='LEND'?'貸出が完了しました':result.code==='S-03'?'返却状況を確認しました':'返却が完了しました';
  $('complete-message').textContent=result.message;
  const rows=[['機種',`${result.model_name}（${result.asset_no}）`],['借用者',data.user.name],['返却予定日',dateText(result.due_date)]];
  if(result.returned_at) rows.push(['返却日時',timeText(result.returned_at)]);else rows.push(['利用目的',result.purpose]);
  details('complete-details',rows);
  // Keep the receipt route for safe refresh/back navigation, without resending a POST.
  const receipt=result.request_key;
  if(receipt) location.hash='result/'+receipt;
  try { await refresh(); } catch(error){notice('手続きは完了しました。最新の一覧を取得できないため、再読み込みしてください。',true);}
}
async function submitPending() {
  const key=pending.request_key,operation=pending.operation;
  try { const response=await api(`/api/${operation==='LEND'?'lend':'return'}/commit`,{request_key:key});await complete({...response.result,request_key:key}); }
  catch(error) {
    if(['E-19','E-20','E-08'].includes(error.code)) { showError(error);$('recovery').hidden=false;$('check-result').hidden=false;return; }
    // Definitive rejection: invalidate the old confirmation before allowing edits.
    try { const cancel=await api('/api/cancel',{request_key:key}); if(cancel.state==='SUCCEEDED'){await complete({...cancel.result,request_key:key});return;} }
    catch(cancelError){if(!['E-21','E-23'].includes(cancelError.code)){showError(cancelError);return;}}
    savePending(null); if(operation==='LEND')inputPanel();else view('return');
    try{await refresh();}catch(_){}
    showError(error);
  }
}
async function returnLoan(loan) {
  const button=Array.from($('loans').querySelectorAll('button')).find(b=>b.closest('.loan-card').textContent.includes(loan.asset_no));
  await run(button,async()=>{clearErrors();const result=await api('/api/return/prepare',{lending_id:loan.id});savePending(result);await submitPending();});
}
async function checkResult() {
  if(!pending)return;
  const key=pending.request_key;
  try {
    const result=await api('/api/results/'+key);
    if(result.state==='SUCCEEDED'){await complete({...result.result,request_key:key});return;}
    clearErrors();notice('この操作はまだ完了していません。同じ内容で再試行できます。',true);
    if(pending.operation==='LEND')confirmation();else{view('return');$('recovery').hidden=false;$('check-result').hidden=false;}
    $('check-result').textContent='同じ操作を再試行';$('check-result').dataset.retry='true';
    $('recovery').hidden=false;$('check-result').hidden=false;
  } catch(error){if(error.code==='E-21'){savePending(null);inputPanel();try{await refresh();}catch(_){}}showError(error);}
}
$('lend-form').addEventListener('submit',event=>{event.preventDefault();run($('prepare-button'),async()=>{clearErrors();saveDraft();const result=await api('/api/lend/prepare',draft());savePending(result);confirmation();});});
$('commit-button').addEventListener('click',()=>run($('commit-button'),async()=>{clearErrors();await submitPending();}));
$('edit-button').addEventListener('click',()=>run($('edit-button'),async()=>{const response=await api('/api/cancel',{request_key:pending.request_key});if(response.state==='SUCCEEDED'){await complete(response.result);return;}savePending(null);clearErrors();inputPanel();await refresh();}));
$('check-result').addEventListener('click',()=>run($('check-result'),async()=>{if($('check-result').dataset.retry==='true'){delete $('check-result').dataset.retry;await submitPending();}else await checkResult();}));
$('relogin').addEventListener('click',()=>run($('relogin'),async()=>{const result=await api('/api/bootstrap');csrf=result.csrf;renderDashboard(result);clearErrors();if(pending)await checkResult();else notice('再ログインしました。',true);}));
async function navigate(name) {if(busy)return;if(pending){notice('確認中・処理中の操作があります。内容の確認または結果の確認を完了してください。',true);return;}clearErrors();location.hash='';view(name);if(name==='lend')inputPanel();try{await refresh();if(name==='lend'&&data.lend_notice)notice(messages[data.lend_notice]);else if(name==='lend'&&!data.devices.length)notice(messages['E-05'],true);}catch(error){showError(error);}}
$('nav-lend').addEventListener('click',()=>navigate('lend'));$('nav-return').addEventListener('click',()=>navigate('return'));$('complete-next').addEventListener('click',()=>navigate('return'));
function countPurpose(){ $('purpose-count').textContent=`${Array.from($('purpose').value.trim()).length} / 100`;saveDraft(); }
$('purpose').addEventListener('input',countPurpose);$('due_date').addEventListener('input',saveDraft);$('device_id').addEventListener('change',saveDraft);
async function boot() {
  try {
    const result=await api('/api/bootstrap');csrf=result.csrf;messages=result.messages;storageKey='gate5-'+result.user.id;renderDashboard(result);
    const saved=JSON.parse(sessionStorage.getItem(storageKey+'-draft')||'null');if(saved){for(const [key,value] of Object.entries(saved))if(fieldIds[key])$(key).value=value;countPurpose();}
    pending=JSON.parse(sessionStorage.getItem(storageKey)||'null');
    if(pending){if(pending.operation==='LEND')confirmation();await checkResult();}
    else if(location.hash.startsWith('#result/')){const key=location.hash.slice(8);const receipt=await api('/api/results/'+key);if(receipt.state==='SUCCEEDED')await complete({...receipt.result,request_key:key});}
    else if(data.lend_notice)notice(messages[data.lend_notice]);else if(!data.devices.length)notice(messages['E-05'],true);
  } catch(error){showError(error);}
}
boot();
