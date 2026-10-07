/* Real-browser acceptance tests and submission screenshots. Requires Playwright. */
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root=__dirname, python=process.env.GATE5_PYTHON||'python';
const work=fs.mkdtempSync(path.join(require('node:os').tmpdir(),'gate5-browser-'));
const mocks=path.join(root,'mocks');fs.mkdirSync(mocks,{recursive:true});
const servers=[], checks=[], pageErrors=[];
function launch(db,user=1,extra=[]) {
  return new Promise((resolve,reject)=>{
    const process=spawn(python,['-X','utf8','app.py','--port','0','--db',db,'--user-id',String(user),...extra],{cwd:root,windowsHide:true});servers.push(process);
    let output='';const timer=setTimeout(()=>reject(new Error('Server startup timeout: '+output)),20000);
    process.stdout.on('data',chunk=>{output+=chunk;const match=output.match(/http:\/\/127\.0\.0\.1:\d+/);if(match){clearTimeout(timer);resolve(match[0]);}});
    process.stderr.on('data',chunk=>output+=chunk);process.on('error',reject);process.on('exit',code=>{if(code)reject(new Error(output));});
  });
}
async function ready(page,url) {await page.goto(url);await page.locator('#user-name').filter({hasNotText:'読み込み中'}).waitFor();await page.waitForFunction(()=>document.querySelector('#available-count').textContent!=='—');}
async function prepare(page,device='1',purpose='出張先での資料作成・オンライン会議') {
  await page.locator('#device_id').selectOption(device);
  const min=await page.locator('#due_date').getAttribute('min');await page.locator('#due_date').fill(min);
  await page.locator('#purpose').fill(purpose);await page.locator('#prepare-button').click();await page.locator('#confirm-panel').waitFor({state:'visible'});
}
async function screenshot(page,name) {await page.screenshot({path:path.join(mocks,name),fullPage:true});}
async function visibleText(page,selector,text){await page.waitForFunction(({selector,text})=>{const e=document.querySelector(selector);return e&&!e.hidden&&e.textContent.includes(text);},{selector,text});}
async function json(page,url,body) {
  return page.evaluate(async({url,body})=>{
    const boot=await (await fetch('/api/bootstrap')).json();
    return (await fetch(url,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':boot.csrf},body:body===undefined?undefined:JSON.stringify(body)})).json();
  },{url,body});
}
async function main(){
  let browser;
  try {
    const db=path.join(work,'shared.db'),faultDb=path.join(work,'fault.db');
    const url1=await launch(db,1),url2=await launch(db,2),urlFault=await launch(faultDb,1,['--fault-after-first-update','LEND']),urlRetired=await launch(db,4);
    const launchOptions={headless:true};
    if(process.env.PLAYWRIGHT_EXECUTABLE) launchOptions.executablePath=process.env.PLAYWRIGHT_EXECUTABLE;
    browser=await chromium.launch(launchOptions);
    const context=await browser.newContext({viewport:{width:1440,height:1080},deviceScaleFactor:1});
    const newPage=async()=>{const p=await context.newPage();p.on('pageerror',e=>pageErrors.push(e.message));return p;};
    const a=await newPage(),b=await newPage();await ready(a,url1);await ready(b,url2);
    await screenshot(a,'mock_00_貸出申請.png');
    await a.locator('#device_id').selectOption('1');await a.locator('#due_date').fill('2020-01-01');await a.locator('#purpose').fill('出張');await a.locator('#prepare-button').click();
    await visibleText(a,'#date-error','明日から90日以内');assert.equal(await a.locator('#purpose').inputValue(),'出張');
    await screenshot(a,'mock_03_エラー_過去日.png');checks.push('過去日のエラー表示・入力保持');
    await prepare(a);await prepare(b);
    await a.locator('#commit-button').click();await a.locator('#complete-view').waitFor({state:'visible'});await visibleText(a,'#complete-message','PC-0001 を貸し出しました。');
    await screenshot(a,'mock_01_正常時_貸出完了.png');checks.push('正常な貸出・完了表示');
    await a.reload();await a.locator('#complete-view').waitFor({state:'visible'});checks.push('完了画面リロードで再登録なし');
    await b.locator('#commit-button').click();await visibleText(b,'#notice','この端末は現在貸出中です。');await screenshot(b,'mock_02_エラー_貸出中.png');checks.push('別社員の確認後競合・E-02');
    await a.locator('#complete-next').click();await a.locator('.loan-card button').click();await visibleText(a,'#complete-message','PC-0001 を返却しました。');await screenshot(a,'mock_05_正常時_返却完了.png');checks.push('正常な返却');
    const fault=await newPage();await ready(fault,urlFault);await prepare(fault);await fault.locator('#commit-button').click();await visibleText(fault,'#notice','処理を完了できませんでした。');await screenshot(fault,'mock_04_エラー_更新失敗.png');
    const faultState=await json(fault,'/api/dashboard');assert.equal(faultState.loans.length,0);assert.ok(faultState.devices.some(d=>d.id===1));checks.push('更新途中の失敗・貸出と端末更新を全取消');
    await fault.locator('#check-result').click();await visibleText(fault,'#notice','この操作はまだ完了していません');assert.equal(await fault.locator('#check-result').textContent(),'同じ操作を再試行');checks.push('未完了キーの照会と同じ操作の再試行案内');
    const retired=await newPage();await ready(retired,urlRetired);await visibleText(retired,'#notice','在籍中の社員のみ');assert.equal(await retired.locator('#prepare-button').isDisabled(),true);await retired.locator('#nav-return').click();await retired.locator('.loan-card button').click();await visibleText(retired,'#complete-message','返却しました');checks.push('退職者の新規貸出拒否・本人の返却許可');
    // Lose only the commit response AFTER the server has committed, then recover by GET.
    await b.locator('#nav-lend').click();await prepare(b,'2','<img src=x onerror="window.XSS=1">');
    assert.equal(await b.evaluate(()=>window.XSS),undefined);
    await b.route('**/api/lend/commit',async route=>{await route.fetch();await route.abort('failed');});
    await b.locator('#commit-button').click();await visibleText(b,'#notice','処理結果を確認できません');
    await b.unroute('**/api/lend/commit');await b.locator('#check-result').click();await visibleText(b,'#complete-message','PC-0002 を貸し出しました。');
    assert.equal(await b.evaluate(()=>window.XSS),undefined);assert.equal(await b.locator('#complete-details img').count(),0);checks.push('コミット応答消失から成功結果を復元・HTMLは文字表示');
    // Reload an unsubmitted confirmation, then edit it: the old key must expire.
    await a.locator('#nav-lend').click();await prepare(a,'6');
    const key=await a.evaluate(()=>JSON.parse(sessionStorage.getItem('gate5-1')).request_key);
    await a.reload();await a.locator('#confirm-panel').waitFor({state:'visible'});await visibleText(a,'#notice','まだ完了していません');await a.locator('#edit-button').click();await a.locator('#input-panel').waitFor({state:'visible'});
    assert.equal((await json(a,'/api/results/'+key)).code,'E-21');checks.push('確認画面の再読込・修正による旧キー無効化');
    // No horizontal overflow on mobile.
    await a.setViewportSize({width:390,height:844});await screenshot(a,'mock_06_モバイル.png');
    assert.equal(await a.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);checks.push('390pxのモバイル表示・横はみ出しなし');
    assert.deepEqual(pageErrors,[]);checks.push('ブラウザの未処理JavaScriptエラーなし');
    const report={date:new Date().toISOString(),result:'PASS',checks,screenshots:fs.readdirSync(mocks).filter(x=>x.endsWith('.png'))};
    fs.writeFileSync(path.join(root,'browser-test-results.json'),JSON.stringify(report,null,2),'utf8');console.log(JSON.stringify(report,null,2));
  } finally {if(browser)await browser.close();for(const server of servers)server.kill();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
