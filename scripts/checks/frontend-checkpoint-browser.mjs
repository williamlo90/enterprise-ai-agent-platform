import {writeFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {chromium} from '../../frontend/node_modules/playwright-core/index.mjs';

let input='';for await (const chunk of process.stdin) input+=chunk;
const fixture=JSON.parse(input);input='';
const out=fileURLToPath(new URL('../../local/fe-check-01/',import.meta.url));
const checks=[];const replay={};let browser,context,page,stage='launch';
const title='Kebijakan perjalanan gabungan lokal';
const original='Perjalanan checkpointword pertama. <b>Fictional policy</b>.';
const replacement='Perjalanan checkpointword terbaru. Fictional replacement.';
const watchdog=setTimeout(()=>{void browser?.close();},190000);
function check(name,passed){checks.push({name,passed:!!passed});if(!passed)throw new Error('controlled_check_failed');}
async function save(name,passed){await writeFile(out+name+'.json',JSON.stringify({passed,stage,checks,replay,browser_version:browser?.version(),external_boundary:'controlled sessions/storage/scanner/extractor/embedding; no AWS',application:'actual built React, HTTPS API, PostgreSQL and jobs'},null,2));}
async function open(token=fixture.manager,path='/knowledge'){
  await context?.close();context=await browser.newContext({ignoreHTTPSErrors:true,serviceWorkers:'block'});
  await context.route('**/*',route=>new URL(route.request().url()).origin===fixture.origin?route.continue():route.abort());
  if(token)await context.addCookies([{name:'__Host-eai-session',value:token,url:fixture.origin,httpOnly:true,secure:true,sameSite:'Lax'}]);
  page=await context.newPage();page.setDefaultTimeout(9000);await page.goto(fixture.origin+path);
}
async function control(action){
  const response=await context.request.post(fixture.origin+'/__fixture/control',{headers:{'x-fixture-control':fixture.control,'x-fixture-action':action}});
  if(response.status()!==200)throw new Error('owned_fixture_action_failed');
  // Logout really revokes the previous token. Later journeys must use the newly
  // minted fixture session, never silently reuse that revoked credential.
  if(action==='session')fixture.manager=(await context.cookies(fixture.origin)).find(c=>c.name==='__Host-eai-session')?.value;
  return response;
}
async function read(path){return page.evaluate(async path=>navigator.locks.request('eai-session-context-v1',{mode:'exclusive'},async()=>{
  const me=await(await fetch('/api/v1/me',{headers:{'X-EAI-CSRF-Bootstrap':'1'},cache:'no-store'})).json();
  const response=await fetch(path,{headers:{'X-EAI-Expected-Context':me.context_binding},cache:'no-store'});
  return {status:response.status,body:response.ok?await response.json():null};
}),path);}
async function upload(text,isReplacement=false){
  if(!isReplacement)await page.getByRole('combobox',{name:'Cakupan dokumen',exact:true}).selectOption('tenant');
  await page.getByLabel('File pengetahuan',{exact:true}).setInputFiles({name:'Perjalanan fiktif.md',mimeType:'text/markdown',buffer:Buffer.from(text)});
  await page.getByRole('button',{name:'Unggah file',exact:true}).click();await page.getByRole('heading',{name:'File dalam karantina',exact:true}).waitFor();
  await control('process');await page.getByRole('heading',{name:'File siap untuk pembuatan draft',exact:true}).waitFor();
}
async function preview(){await page.getByRole('button',{name:'Muat ulang versi dan status',exact:true}).click();await page.waitForFunction(()=>[...document.querySelectorAll('button')].some(b=>b.textContent==='Buka preview ekstraksi'&&!b.disabled));await page.getByRole('button',{name:'Buka preview ekstraksi',exact:true}).click();await page.locator('.extraction-preview').waitFor();}
async function publish(){await page.getByLabel('Saya telah memeriksa ekstraksi, periode dan audience versi ini',{exact:true}).check();await page.getByRole('button',{name:'Terbitkan versi ini',exact:true}).click();}
async function published(number){await page.waitForFunction(n=>{
  const active=document.querySelector('.workspace-detail-grid > section');const action=[...document.querySelectorAll('button')].find(b=>b.textContent==='Unggah versi pengganti');
  return !!active&&active.textContent.includes('Versi '+n)&&active.textContent.includes('Diterbitkan')&&action&&!action.disabled;
},number);}
async function leaveCatalog(){
  await page.getByRole('link',{name:'Kembali ke dokumen',exact:true}).click();
  const branch=await Promise.race([page.waitForURL(url=>url.pathname==='/knowledge').then(()=> 'navigated'),page.getByRole('dialog').waitFor({state:'visible'}).then(()=> 'confirmation')]);
  if(branch==='confirmation')await page.getByRole('button',{name:'Buang perubahan dan lanjut',exact:true}).click();
  await page.waitForURL(url=>url.pathname==='/knowledge');
}
async function search(){await page.getByRole('textbox',{name:'Kata pencarian',exact:true}).fill('checkpointword');await page.getByRole('button',{name:'Cari',exact:true}).click();await page.getByRole('button',{name:title,exact:true}).waitFor();}
async function manage(){
  const show=page.getByRole('button',{name:'Periksa dan kelola pembaca',exact:true});
  const edit=page.getByRole('button',{name:'Ubah audience',exact:true});
  const state=await Promise.race([show.waitFor({state:'visible'}).then(()=> 'closed'),edit.waitFor({state:'visible'}).then(()=> 'open')]);
  if(state==='closed')await show.click();
  await edit.waitFor();
}
async function audience(mode){await manage();await page.getByRole('button',{name:'Ubah audience',exact:true}).click();await page.getByRole('combobox',{name:'Siapa yang boleh membaca',exact:true}).selectOption(mode);
  if(mode==='specific_people'){await page.getByRole('button',{name:'Periksa pilihan akhir',exact:true}).click();await page.getByRole('button',{name:'Gunakan pilihan 0 orang',exact:true}).click();}
  await page.getByLabel('Saya telah memeriksa audience dan dampaknya pada seluruh versi',{exact:true}).check();await page.getByRole('button',{name:'Simpan audience',exact:true}).click();await page.getByRole('button',{name:'Ubah audience',exact:true}).waitFor();}
async function externalSwitch(member){
  const me=await(await context.request.get(fixture.origin+'/api/v1/me',{headers:{'X-EAI-CSRF-Bootstrap':'1','sec-fetch-site':'same-origin'}})).json();
  const response=await context.request.post(fixture.origin+'/api/v1/session/tenant',{headers:{origin:fixture.origin,'sec-fetch-site':'same-origin','x-csrf-token':me.csrf_token},data:{membership_id:member}});
  if(!response.ok())throw new Error('actual_external_session_switch_failed');
}
try{
  browser=await chromium.launch({channel:'msedge',headless:true});
  stage='organization-to-audience';await open(fixture.admin,'/organization/members/'+fixture.membership);
  await page.getByRole('textbox',{name:'Departemen',exact:true}).fill('Finance');await page.getByRole('textbox',{name:'Jabatan',exact:true}).fill('Manager');
  await page.getByRole('link',{name:'Tampilan',exact:true}).click();await page.getByRole('button',{name:'Tetap di sini',exact:true}).click();
  check('unsaved Stay preserves organization form and location',new URL(page.url()).pathname.startsWith('/organization/members/')&&await page.getByRole('textbox',{name:'Departemen',exact:true}).inputValue()==='Finance');
  await page.getByRole('link',{name:'Tampilan',exact:true}).click();await page.getByRole('button',{name:'Buang perubahan dan lanjut',exact:true}).click();await page.waitForURL('**/appearance');
  await page.goto(fixture.origin+'/organization/members/'+fixture.membership);await page.getByRole('textbox',{name:'Departemen',exact:true}).waitFor();
  check('unsaved Discard discards only local fields',await page.getByRole('textbox',{name:'Departemen',exact:true}).inputValue()==='');
  await page.getByRole('textbox',{name:'Departemen',exact:true}).fill('Finance');await page.getByRole('textbox',{name:'Jabatan',exact:true}).fill('Manager');
  await page.getByRole('button',{name:'Simpan atribut',exact:true}).click();await page.getByRole('heading',{name:'Atribut berhasil disimpan',exact:true}).waitFor();await page.reload();
  await page.getByRole('textbox',{name:'Departemen',exact:true}).waitFor();check('admin form commits organization metadata used by KM filter',await page.getByRole('textbox',{name:'Departemen',exact:true}).inputValue()==='Finance');

  stage='upload-admit';await open(fixture.manager,'/knowledge/upload');await upload(original);
  await page.getByRole('textbox',{name:'Judul dokumen',exact:true}).fill(title);
  await page.getByRole('textbox',{name:'Departemen',exact:true}).fill('Finance');await page.getByRole('textbox',{name:'Jabatan',exact:true}).fill('Manager');await page.getByRole('button',{name:'Terapkan filter pembaca',exact:true}).click();
  await page.getByLabel('Budi Santoso',{exact:true}).check();await page.getByRole('button',{name:'Periksa pilihan akhir',exact:true}).click();await page.getByRole('button',{name:'Gunakan pilihan 2 orang',exact:true}).click();
  check('admin metadata drives actual KM named-reader selection',await page.getByText('Pembuat dokumen ditambahkan server sebagai pembaca awal. Grant ini dapat dicabut kemudian.',{exact:true}).count()===1);
  await page.getByLabel('Saya telah memeriksa judul, periode dan audience draft ini',{exact:true}).check();await page.getByRole('button',{name:'Buat draft',exact:true}).click();await page.getByRole('link',{name:'Periksa draft dokumen',exact:true}).click();await page.getByRole('heading',{name:'Riwayat versi',exact:true}).waitFor();
  const did=new URL(page.url()).pathname.split('/').at(-1);const draft=(await read('/api/v1/knowledge/documents/'+did)).body;const first=draft.latest_version.version_id;
  check('unpublished draft cannot be read by known source ID',(await read('/api/v1/knowledge/sources/'+first+'/content')).status===404);
  await control('process');await preview();check('actual worker preview shows exact uploaded text safely',await page.locator('.extraction-preview').getByText(original,{exact:true}).count()===1&&!await page.locator('.extraction-preview b').count());

  stage='lost-publish';const sends=[];let dropped=false;
  await page.route('**/api/v1/knowledge/versions/'+first+'/publish',async route=>{sends.push(route.request().postData());if(!dropped){dropped=true;await route.fetch();await route.abort('failed');}else await route.continue();});
  await publish();await page.getByRole('heading',{name:'Hasil tindakan dokumen belum diketahui',exact:true}).waitFor();
  check('lost actual publish reply masks extraction instead of claiming success',!await page.locator('.extraction-preview').count());
  await leaveCatalog();await page.getByRole('heading',{name:'Hasil tindakan dokumen belum diketahui',exact:true}).waitFor();await page.getByRole('button',{name:'Rekonsiliasi tindakan dokumen',exact:true}).click();await published(1);
  replay.publish={requests:sends.length,identical:sends.length===2&&sends[0]===sends[1]};check('navigation and reentry reconcile exact publish material/key',replay.publish.identical);
  check('publish does not require reader approval',(await read('/api/v1/knowledge/documents/'+did)).body.active_version_id===first);

  stage='reload-and-new-session';await page.reload();await page.getByRole('heading',{name:'Riwayat versi',exact:true}).waitFor();await manage();check('current ACL survives reload',await page.getByText('Jumlah pembaca: 2',{exact:true}).count()===1);
  await page.getByRole('button',{name:'Keluar',exact:true}).click();await page.getByRole('link',{name:'Masuk melalui akun perusahaan',exact:true}).waitFor();check('actual logout removes the application session',(await context.request.get(fixture.origin+'/api/v1/me')).status()===401);
  await control('session');await page.goto(fixture.origin+'/knowledge');await page.getByRole('link',{name:title,exact:true}).click();await page.getByRole('heading',{name:'Riwayat versi',exact:true}).waitFor();await manage();check('fresh controlled login rediscovers server catalog/version/current ACL',await page.getByText('Jumlah pembaca: 2',{exact:true}).count()===1);

  stage='reader-source-mobile';await open(fixture.employee,'/search');await search();await page.getByRole('button',{name:title,exact:true}).click();await page.locator('mark').waitFor();check('published audience member reads actual uploaded source',await page.locator('mark').textContent()===original);
  await page.setViewportSize({width:390,height:900});check('mobile handoff displays source without horizontal overflow',await page.locator('.source-workspace-panel').isVisible()&&await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:out+'journey-mobile-source.png'});
  await page.getByRole('button',{name:'Kembali ke posisi sebelumnya',exact:true}).click();await page.waitForFunction(t=>document.activeElement?.textContent===t,title);check('mobile return restores focus to exact source result',!await page.locator('mark').count());
  await control('checkpoint');await save('journey-positive',true);

  stage='replacement';await open(fixture.manager,'/knowledge/documents/'+did);await page.getByRole('button',{name:'Unggah versi pengganti',exact:true}).click();await upload(replacement,true);await page.getByLabel('Saya telah memeriksa judul, periode dan audience draft ini',{exact:true}).check();await page.getByRole('button',{name:'Buat versi draft',exact:true}).click();await page.getByText('Versi aktif tetap digunakan sampai versi pengganti diterbitkan.',{exact:true}).waitFor();
  const replacing=(await read('/api/v1/knowledge/documents/'+did)).body;const second=replacing.latest_version.version_id;
  check('replacement retains old active pointer and rejects unpublished source',replacing.active_version_id===first&&(await read('/api/v1/knowledge/sources/'+second+'/content')).status===404);
  await control('process');await preview();await publish();await published(2);check('published replacement supersedes the first exact source',(await read('/api/v1/knowledge/sources/'+first+'/content')).body.publication_state==='superseded');await control('replacement-checkpoint');

  stage='revoke-current-and-old';await audience('all_current_employees');await audience('specific_people');check('named-empty ACL is authoritative zero',await page.getByText('Jumlah pembaca: 0',{exact:true}).count()===1);
  await open(fixture.employee,'/sources/'+second);await page.getByRole('heading',{name:'Sumber tidak tersedia',exact:true}).waitFor();check('reader revoke removes current and superseded source access',(await read('/api/v1/knowledge/sources/'+first+'/content')).status===404&&(await read('/api/v1/knowledge/sources/'+second+'/content')).status===404&&!await page.locator('.source-text').count());
  await open(fixture.manager,'/knowledge/documents/'+did);await audience('all_current_employees');const newcomer=await(await control('new-employee')).json();
  await open(newcomer.token,'/sources/'+second);await page.locator('.source-text').waitFor();check('new current employee gets all-employees access without ACL enumeration',await page.locator('.source-text').textContent()===replacement);

  stage='negative-authority';for(const [label,token] of [['employee',fixture.employee],['admin',fixture.admin],['foreign',fixture.foreign],['collection-only',fixture.scoped]]){
    await open(token,'/access');await page.getByRole('button',{name:'Keluar',exact:true}).waitFor();
    const statuses=[];for(const suffix of ['', '/versions', '/versions/'+second+'/preview', '/audience?limit=50'])statuses.push((await read('/api/v1/knowledge/documents/'+did+suffix)).status);
    check(label+' cannot use known IDs to read management metadata preview or ACL',statuses.every(s=>s===403||s===404));
    if(label!=='employee')check(label+' has no implicit all-employees source access',(await read('/api/v1/knowledge/sources/'+second+'/content')).status===404);
  }

  stage='two-tab-switch';await open(fixture.manager,'/knowledge/documents/'+did);await page.getByRole('heading',{name:'Riwayat versi',exact:true}).waitFor();
  const other=await context.newPage();await other.goto(fixture.origin+'/knowledge');await other.getByRole('link',{name:title,exact:true}).waitFor();
  await page.getByRole('button',{name:'Ganti perusahaan',exact:true}).click();await page.getByLabel('Perusahaan',{exact:true}).selectOption(fixture.member_b);await page.getByRole('button',{name:'Gunakan perusahaan',exact:true}).click();await page.getByText('Fiktif Beta',{exact:true}).first().waitFor();
  await other.evaluate(()=>window.dispatchEvent(new Event('focus')));await other.getByText('Fiktif Beta',{exact:true}).first().waitFor();check('native Web Locks and actual switch purge previous tenant catalog in both tabs',await page.evaluate(()=>isSecureContext&&!!navigator.locks)&&!await other.getByRole('link',{name:title,exact:true}).count());await other.close();

  stage='aba-read';await externalSwitch(fixture.member_a);await page.goto(fixture.origin+'/knowledge');await page.getByRole('link',{name:title,exact:true}).waitFor();
  let aba=false;let abaStatus;let beforeReleaseRows;
  await page.evaluate(()=>{globalThis.checkpointSawHistory=false;globalThis.checkpointObserver=new MutationObserver(()=>{if(document.querySelector('.version-history-row'))globalThis.checkpointSawHistory=true;});globalThis.checkpointObserver.observe(document.body,{childList:true,subtree:true});});
  const versionRoute=url=>url.origin===fixture.origin&&url.pathname==='/api/v1/knowledge/documents/'+did+'/versions';
  await page.route(versionRoute,async route=>{
    if(aba){await route.continue();return;}aba=true;
    // Change the actual session outside Web Locks, then send the actual protected
    // read using B's cookie and the UI's original expected A binding. Restore A
    // before releasing the rejection; no BroadcastChannel signal is emitted.
    await externalSwitch(fixture.member_b);
    const cookies=await context.cookies(fixture.origin);
    const response=await route.fetch({headers:{...route.request().headers(),cookie:cookies.map(c=>c.name+'='+c.value).join('; ')}});
    abaStatus=response.status();await externalSwitch(fixture.member_a);
    beforeReleaseRows=await page.locator('.version-history-row').count();await route.fulfill({response});
  });
  await page.getByRole('link',{name:title,exact:true}).click();await page.getByRole('heading',{name:'Konteks akses perlu diperiksa ulang',exact:true}).waitFor();
  const sawHistory=await page.evaluate(()=>{globalThis.checkpointObserver.disconnect();return globalThis.checkpointSawHistory;});
  check('actual B read within external A-B-A session rotation is server-rejected and never renders history',aba&&abaStatus===409&&beforeReleaseRows===0&&!sawHistory&&!await page.locator('.version-history-row').count());
  await page.unroute(versionRoute);

  stage='withdraw';await control('session');await page.goto(fixture.origin+'/knowledge/documents/'+did);await page.getByRole('heading',{name:'Riwayat versi',exact:true}).waitFor();await preview();await page.getByLabel('Saya memahami penarikan dokumen bersifat terminal untuk seluruh versi',{exact:true}).check();await page.getByRole('button',{name:'Tarik seluruh dokumen',exact:true}).click();await page.getByRole('heading',{name:'Dokumen telah ditarik',exact:true}).waitFor();
  check('terminal withdrawal denies current and old source after real commit',(await read('/api/v1/knowledge/sources/'+first+'/content')).status===404&&(await read('/api/v1/knowledge/sources/'+second+'/content')).status===404);
  await page.goto(fixture.origin+'/knowledge/documents/'+did);await page.getByRole('heading',{name:'Tidak tersedia',exact:true}).waitFor();check('withdrawn document has no replacement or publish controls',!await page.getByRole('button',{name:'Unggah versi pengganti',exact:true}).count());
  check('private journey state never persists in local or session storage',await page.evaluate(()=>localStorage.length===0&&sessionStorage.length===0));
  await save('browser-proof',true);console.log(JSON.stringify({passed:true,checks:checks.length}));
}catch(error){await page?.screenshot({path:out+'failure.png'}).catch(()=>{});await save('browser-partial',false);console.log(JSON.stringify({passed:false,stage,error_name:error instanceof Error?error.name:'unknown',checks}));process.exitCode=1;}
finally{clearTimeout(watchdog);await context?.close();await browser?.close();}
