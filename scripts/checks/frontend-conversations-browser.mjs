import {writeFile} from 'node:fs/promises';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {join} from 'node:path';
import {homedir} from 'node:os';
import {chromium} from '../../frontend/node_modules/playwright-core/index.mjs';
let input='';for await(const chunk of process.stdin)input+=chunk;
const f=JSON.parse(input);input='';
const out=fileURLToPath(new URL('../../local/fe-chat-01/',import.meta.url));
const checks=[],replays={},inspections=[];let browser,context,page,stage='launch';
const watchdog=setTimeout(()=>{void browser?.close();},215000);
function check(name,passed){checks.push({name,passed:!!passed});if(!passed)throw new Error('controlled_check_failed');}
async function save(name,passed){await writeFile(out+name+'.json',JSON.stringify({passed,stage,checks,replays,browser_version:browser?.version(),mode:'Actual built UI/HTTPS/API/restricted PostgreSQL; fixture session bootstrap only; no model execution'},null,2));}
async function open(token=f.owner,path='/chat',capture){
  await context?.close();context=await browser.newContext({ignoreHTTPSErrors:true,serviceWorkers:'block'});
  await context.route('**/*',route=>new URL(route.request().url()).origin===f.origin?route.continue():route.abort());
  if(capture)context.on('request',r=>capture.push(new URL(r.url()).pathname));
  await context.addCookies([{name:'__Host-eai-session',value:token,url:f.origin,httpOnly:true,secure:true,sameSite:'Lax'}]);page=await context.newPage();page.setDefaultTimeout(10000);await page.goto(f.origin+path);
}
async function control(action){const r=await context.request.post(f.origin+'/__fixture/control',{headers:{'x-fixture-control':f.control,'x-fixture-action':action}});if(!r.ok())throw new Error('fixture_action_failed');if(action==='session')f.owner=(await context.cookies(f.origin)).find(c=>c.name==='__Host-eai-session')?.value;return r;}
async function discardIfAsked(){const branch=await Promise.race([page.getByRole('dialog').waitFor({state:'visible'}).then(()=>true),page.waitForURL(url=>url.pathname==='/chat').then(()=>false)]);if(branch)await page.getByRole('button',{name:/^(Buang perubahan dan lanjut|Discard changes and continue)$/}).click();}
async function lostOnce(pattern,method){const sent=[];let dropped=false;await page.route(pattern,async route=>{
  if(route.request().method()!==method){await route.continue();return;}sent.push(route.request().postData());
  if(!dropped){
    dropped=true;const response=await route.fetch();const expected=method==='POST'?201:200;
    const committed=response.status()===expected;checks.push({name:'injected '+method+' lost reply follows actual successful '+expected+' receipt',passed:committed});
    if(!committed){await route.fulfill({response});return;}
    const body=await response.json(),material=JSON.parse(sent[0]);
    const matches=method==='POST'?body.language===material.language:body.label===material.name&&body.version===material.expected_version+1;
    checks.push({name:'actual '+method+' commit receipt matches submitted material',passed:matches});
    if(!matches){await route.fulfill({response});return;}
    await route.abort('failed');
  }else await route.continue();
});return sent;}
async function read(path){return page.evaluate(async path=>navigator.locks.request('eai-session-context-v1',{mode:'exclusive'},async()=>{const me=await(await fetch('/api/v1/me',{headers:{'X-EAI-CSRF-Bootstrap':'1'},cache:'no-store'})).json();const r=await fetch(path,{headers:{'X-EAI-Expected-Context':me.context_binding},cache:'no-store'});return {status:r.status,body:r.ok?await r.json():null};}),path);}
async function inspect(width){
  const base=pathToFileURL(join(homedir(),'.codex/skills/agency-taste/scripts/lib/'));
  const {probe,layout,app}=await import(new URL('probe.mjs',base));const {judge,judgeLayout,judgeApp,APP_SKIP}=await import(new URL('rules.mjs',base));
  const metrics=await page.evaluate(probe),lay=await page.evaluate(layout),appMetrics=await page.evaluate(app);
  const findings=[...judge(metrics,{width,allow:[]}).filter(item=>!APP_SKIP.has(item.id)&&!['no-title','small-title'].includes(item.id)),...judgeLayout(lay,{width,allow:[]}),...judgeApp(appMetrics,metrics,{width,allow:[]})];
  inspections.push({width,findings});await writeFile(out+'visual-inspection.json',JSON.stringify({mode:'same authenticated browser, serial; approved product palette/type retained',inspections},null,2));
}
try{
  browser=await chromium.launch({channel:'msedge',headless:true});
  stage='paged-owner-list';const requests=[];await open(f.owner,'/chat',requests);await page.getByRole('heading',{name:'Percakapan',exact:true}).waitFor();await page.locator('.conversation-list > li').first().waitFor();
  check('first owner page is bounded20 and does not display oldest known label',await page.locator('.conversation-list > li').count()===20&&!await page.getByRole('link',{name:f.question,exact:true}).count());
  await page.getByRole('button',{name:'Berikutnya',exact:true}).click();await page.getByRole('link',{name:f.question,exact:true}).waitFor();
  check('next page discovers full first question without per-conversation history fanout',await page.locator('.conversation-list > li').count()===2&&requests.every(p=>!p.includes('/messages')));
  check('first question markup is rendered as text and not abbreviated',await page.getByRole('link',{name:f.question,exact:true}).textContent()===f.question&&!await page.locator('.conversation-list b').count());
  await page.getByRole('link',{name:f.question,exact:true}).click();await page.locator('.conversation-text').waitFor();
  check('opening exact owner conversation preserves the stored question',await page.locator('.conversation-text').textContent()===f.question);
  await open(f.owner,'/chat/'+f.conversation);await page.locator('.conversation-text').waitFor();check('deep-link outside page1 reads bounded metadata/history directly',await page.locator('.conversation-text').textContent()===f.question);

  stage='rename-unknown';await page.getByRole('button',{name:'Ganti nama percakapan',exact:true}).click();await page.getByLabel('Nama percakapan',{exact:true}).fill('  Nama lengkap perjalanan  ');
  const renames=await lostOnce('**/api/v1/conversations/'+f.conversation+'/name','PATCH');await page.getByRole('button',{name:'Simpan nama',exact:true}).click();await page.getByRole('heading',{name:'Hasil penyimpanan belum diketahui',exact:true}).waitFor();
  check('lost committed rename stays unknown with immutable input',await page.getByLabel('Nama percakapan',{exact:true}).isDisabled());await control('rename-other');
  stage='rename-back-list';await page.getByRole('link',{name:'Kembali ke percakapan',exact:true}).click();await discardIfAsked();await page.waitForURL(url=>url.pathname==='/chat');
  // Reenter through the SPA. A hard page.goto intentionally destroys the
  // memory-only unresolved intent and cannot be used as its recovery oracle.
  await page.getByRole('button',{name:'Berikutnya',exact:true}).click();await page.getByRole('link',{name:'Nama dari sesi lain',exact:true}).click();await page.getByRole('heading',{name:'Nama dari sesi lain',exact:true,level:1}).waitFor();
  stage='rename-reconcile';await page.getByRole('button',{name:'Periksa hasil penyimpanan',exact:true}).click();await page.getByRole('button',{name:'Ganti nama percakapan',exact:true}).waitFor();
  replays.rename={requests:renames.length,identical:renames.length===2&&renames[0]===renames[1]};check('old exact receipt is reconciled but current later label is refetched',replays.rename.identical&&await page.getByRole('heading',{name:'Nama dari sesi lain',exact:true,level:1}).count()===1);
  check('rename leaves original history and language unchanged',await page.locator('.conversation-text').textContent()===f.question&&await page.getByText('Bahasa percakapan: Indonesia',{exact:true}).count()===1);await page.unroute('**/api/v1/conversations/'+f.conversation+'/name');

  stage='stale-cas';await page.getByRole('button',{name:'Ganti nama percakapan',exact:true}).click();await page.getByLabel('Nama percakapan',{exact:true}).fill('Percobaan versi lama');await control('rename-other');let patches=0;
  page.on('request',r=>{if(new URL(r.url()).pathname==='/api/v1/conversations/'+f.conversation+'/name'&&r.method()==='PATCH')patches++;});
  await page.getByRole('button',{name:'Simpan nama',exact:true}).click();await page.getByRole('heading',{name:'Data telah berubah',exact:true}).waitFor();check('other actual session wins CAS and stale UI does not auto-rebase or resend',patches===1);
  await page.getByRole('button',{name:'Muat ulang percakapan',exact:true}).click();await page.getByRole('button',{name:'Ganti nama percakapan',exact:true}).waitFor();
  await page.getByRole('button',{name:'Ganti nama percakapan',exact:true}).click();await page.getByLabel('Nama percakapan',{exact:true}).fill(' \n\t');await page.getByRole('button',{name:'Simpan nama',exact:true}).click();await page.waitForFunction(()=>document.activeElement?.getAttribute('role')==='alert');check('blank rename focuses linked error without server write',patches===1&&!!await page.getByLabel('Nama percakapan',{exact:true}).getAttribute('aria-describedby'));
  await page.getByRole('button',{name:'Batal',exact:true}).click();await control('checkpoint');await save('positive-browser',true);

  stage='create-unknown-language';await page.getByRole('link',{name:'Kembali ke percakapan',exact:true}).click();await page.getByRole('combobox',{name:'Bahasa percakapan baru',exact:true}).selectOption('en');
  const creates=await lostOnce('**/api/v1/conversations','POST');await page.getByRole('button',{name:'Buat percakapan',exact:true}).click();await page.getByRole('heading',{name:'Hasil penyimpanan belum diketahui',exact:true}).waitFor();
  await page.getByRole('link',{name:'Tampilan',exact:true}).click();await page.getByRole('button',{name:'Buang perubahan dan lanjut',exact:true}).click();await page.getByLabel('Bahasa antarmuka',{exact:true}).selectOption('en');await page.getByLabel('Theme',{exact:true}).selectOption('dark');await page.getByRole('link',{name:'Back to workspace',exact:true}).click();
  await page.getByRole('combobox',{name:'New conversation language',exact:true}).waitFor();check('unknown create retains explicit language after locale switch and reentry',await page.getByRole('combobox',{name:'New conversation language',exact:true}).inputValue()==='en'&&await page.getByRole('combobox',{name:'New conversation language',exact:true}).isDisabled());
  await page.getByRole('button',{name:'Check save result',exact:true}).click();await page.getByRole('heading',{name:'New conversation',exact:true,level:1}).waitFor();
  replays.create={requests:creates.length,identical:creates.length===2&&creates[0]===creates[1]};check('explicit nondefault create reconciliation opens detail without discard prompt or duplicate',replays.create.identical&&!await page.getByRole('dialog').count()&&await page.getByText('Conversation language: English',{exact:true}).count()===1);
  await page.unroute('**/api/v1/conversations');

  stage='locale-and-visual';await page.goto(f.origin+'/chat/'+f.conversation);await page.getByRole('heading',{name:'Nama dari sesi lain',exact:true,level:1}).waitFor();check('changing interface locale never translates stored label or existing language',await page.getByText('Conversation language: Indonesia',{exact:true}).count()===1&&await page.locator('.conversation-text').textContent()===f.question);
  for(const width of [1440,768,390]){
    await page.setViewportSize({width,height:1000});await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))));
    check('full stored text wraps without horizontal overflow at '+width,await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await page.screenshot({path:out+'conversation-dark-'+width+'.png'});await inspect(width);
  }
  await page.setViewportSize({width:1440,height:1000});await page.getByRole('link',{name:'Appearance',exact:true}).click();await page.getByLabel('Interface language',{exact:true}).selectOption('id');await page.getByLabel('Tema',{exact:true}).selectOption('light');await page.goto(f.origin+'/chat/'+f.conversation);await page.getByRole('heading',{name:'Nama dari sesi lain',exact:true,level:1}).waitFor();await page.setViewportSize({width:390,height:1000});await page.screenshot({path:out+'conversation-light-390.png'});
  await page.getByRole('link',{name:'Kembali ke percakapan',exact:true}).focus();await page.keyboard.press('Tab');
  check('actual Tab reaches the rename control on mobile',await page.getByRole('button',{name:'Ganti nama percakapan',exact:true}).evaluate(el=>document.activeElement===el));

  stage='fresh-session';await page.getByRole('button',{name:'Keluar',exact:true}).click();await page.getByRole('link',{name:'Masuk melalui akun perusahaan',exact:true}).waitFor();await control('session');await page.goto(f.origin+'/chat/'+f.conversation);await page.getByRole('heading',{name:'Nama dari sesi lain',exact:true,level:1}).waitFor();check('fresh controlled session rediscovers persisted rename and original history',await page.locator('.conversation-text').textContent()===f.question);

  stage='known-id-denials';for(const [label,token,target] of [['colleague',f.colleague,f.conversation],['foreign',f.foreign,f.conversation],['owner-foreign-object',f.owner,f.colleague_conversation]]){
    await open(token,'/chat/'+target);await page.getByRole('heading',{name:'Tidak tersedia',exact:true}).waitFor();check(label+' cannot read known conversation metadata or history',!await page.locator('.conversation-text').count()&&!await page.getByRole('button',{name:'Ganti nama percakapan',exact:true}).count());
  }
  await open(f.admin,'/chat/'+f.conversation);await page.getByRole('heading',{name:'Halaman tidak tersedia untuk akses Anda',exact:true}).waitFor();check('admin role does not imply employee chat access',!await page.locator('.conversation-text').count());

  stage='pending-switch-two-tabs';await open(f.owner,'/chat/'+f.conversation);await page.getByRole('button',{name:'Ganti nama percakapan',exact:true}).click();await page.getByLabel('Nama percakapan',{exact:true}).fill('Nama menunggu rekonsiliasi');await lostOnce('**/api/v1/conversations/'+f.conversation+'/name','PATCH');await page.getByRole('button',{name:'Simpan nama',exact:true}).click();await page.getByRole('heading',{name:'Hasil penyimpanan belum diketahui',exact:true}).waitFor();
  const other=await context.newPage();await other.goto(f.origin+'/chat/'+f.conversation);await other.getByRole('heading',{name:'Nama menunggu rekonsiliasi',exact:true,level:1}).waitFor();
  await page.getByRole('button',{name:'Ganti perusahaan',exact:true}).click();await page.getByRole('button',{name:'Tetap di sini',exact:true}).click();check('Stay preserves unknown intent before tenant switch',await page.getByRole('heading',{name:'Hasil penyimpanan belum diketahui',exact:true}).count()===1);
  await page.getByRole('button',{name:'Ganti perusahaan',exact:true}).click();await page.getByRole('button',{name:'Buang perubahan dan lanjut',exact:true}).click();await page.getByLabel('Perusahaan',{exact:true}).selectOption(f.member_b);await page.getByRole('button',{name:'Gunakan perusahaan',exact:true}).click();await page.getByText('Fiktif Beta',{exact:true}).first().waitFor();await other.evaluate(()=>window.dispatchEvent(new Event('focus')));await other.getByText('Fiktif Beta',{exact:true}).first().waitFor();
  check('actual tenant switch purges unknown input and prior history from both tabs',!await page.locator('.conversation-text').count()&&!await other.locator('.conversation-text').count()&&!await page.getByRole('heading',{name:'Hasil penyimpanan belum diketahui',exact:true}).count());await other.close();
  check('browser stores only allowed locale/theme enums, never conversations or intents',await page.evaluate(()=>Object.keys(localStorage).every(k=>['eai.ui.locale','eai.ui.theme'].includes(k))&&sessionStorage.length===0));
  if(checks.some(c=>!c.passed))throw new Error('recorded_check_failed');
  await save('browser-proof',true);console.log(JSON.stringify({passed:true,checks:checks.length}));
}catch(error){await page?.screenshot({path:out+'failure.png'}).catch(()=>{});await save('browser-partial',false);console.log(JSON.stringify({passed:false,stage,error_name:error instanceof Error?error.name:'unknown',error_summary:error instanceof Error?error.message.split('\n')[0].slice(0,180):'unknown',checks}));process.exitCode=1;}
finally{clearTimeout(watchdog);await context?.close();await browser?.close();}
