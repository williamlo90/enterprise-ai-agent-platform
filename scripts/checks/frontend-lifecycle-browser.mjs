import {writeFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {chromium} from '../../frontend/node_modules/playwright-core/index.mjs';
let input='';for await(const chunk of process.stdin)input+=chunk;const fixture=JSON.parse(input);input='';
const out=fileURLToPath(new URL('../../local/fe-know-03/',import.meta.url));const checks=[];const replayStats={};let browser,context,page,stage='launch';
const watchdog=setTimeout(()=>{void browser?.close();},100000);
function check(name,passed){checks.push({name,passed:!!passed});if(!passed)throw new Error('controlled_check_failed');}
async function open(token=fixture.manager,path='/knowledge/upload'){
  await context?.close();context=await browser.newContext({ignoreHTTPSErrors:true,serviceWorkers:'block'});await context.route('**/*',route=>new URL(route.request().url()).origin===fixture.origin?route.continue():route.abort());
  await context.addCookies([{name:'__Host-eai-session',value:token,url:fixture.origin,httpOnly:true,secure:true,sameSite:'Lax'}]);page=await context.newPage();page.setDefaultTimeout(9000);await page.goto(fixture.origin+path);
}
async function processOne(fail=false){const response=await context.request.post(fixture.origin+'/__fixture/process',{headers:{'x-fixture-control':fixture.control,...(fail?{'x-fixture-fail':'parse'}:{})}});if(response.status()!==200)throw new Error('owned_job_process_failed');if(fail&&(await response.json()).expected_failure!==true)throw new Error('controlled_extractor_failure_not_recorded');}
async function read(path){return await page.evaluate(async path=>navigator.locks.request('eai-session-context-v1',{mode:'exclusive'},async()=>{
  const me=await (await fetch('/api/v1/me',{headers:{'X-EAI-CSRF-Bootstrap':'1'},cache:'no-store'})).json();const response=await fetch(path,{headers:{'X-EAI-Expected-Context':me.context_binding},cache:'no-store'});return {status:response.status,body:response.ok?await response.json():null};
}),path);}
async function upload(text,replacement=false){
  if(!replacement)await page.getByRole('combobox',{name:'Cakupan dokumen',exact:true}).selectOption('tenant');
  await page.getByLabel('File pengetahuan',{exact:true}).setInputFiles({name:'Kebijakan perjalanan dengan judul file lengkap.md',mimeType:'text/markdown',buffer:Buffer.from(text)});
  const response=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/v1/files'&&r.request().method()==='POST');await page.getByRole('button',{name:'Unggah file',exact:true}).click();const actual=await response;
  const uploadKey=actual.request().headers()['idempotency-key'];await page.getByRole('heading',{name:'File dalam karantina',exact:true}).waitFor();await processOne();await page.getByRole('heading',{name:'File siap untuk pembuatan draft',exact:true}).waitFor();return uploadKey;
}
async function initialFields(title,named=true,period=false){
  await page.getByRole('textbox',{name:'Judul dokumen',exact:true}).fill(title);
  if(period){await page.getByLabel('Mulai berlaku, inklusif',{exact:true}).fill('2020-01-01');await page.getByLabel('Akhir berlaku, eksklusif',{exact:true}).fill('2030-01-01');}
  if(named){await page.getByRole('button',{name:'Periksa pilihan akhir',exact:true}).click();await page.getByText('Pembuat dokumen ditambahkan server sebagai pembaca awal. Grant ini dapat dicabut kemudian.',{exact:true}).waitFor();await page.getByRole('button',{name:'Gunakan pilihan 1 orang',exact:true}).click();}
  else await page.getByRole('combobox',{name:'Audience awal dokumen',exact:true}).selectOption('all_current_employees');
  await page.getByLabel('Saya telah memeriksa judul, periode dan audience draft ini',{exact:true}).check();
}
async function reloadReady(){await page.getByRole('button',{name:'Muat ulang versi dan status',exact:true}).click();await page.getByRole('button',{name:'Buka preview ekstraksi',exact:true}).waitFor({state:'visible'});await page.waitForFunction(()=>[...document.querySelectorAll('button')].some(e=>e.textContent==='Buka preview ekstraksi'&&!e.disabled));}
async function inspect(){await page.getByRole('button',{name:'Buka preview ekstraksi',exact:true}).click();await page.locator('.extraction-preview').waitFor();}
async function publish(){await page.getByLabel('Saya telah memeriksa ekstraksi, periode dan audience versi ini',{exact:true}).check();await page.getByRole('button',{name:'Terbitkan versi ini',exact:true}).click();}
async function leaveCatalog(){
  await page.getByRole('link',{name:'Kembali ke dokumen',exact:true}).click();
  const branch=await Promise.race([page.waitForURL(url=>url.pathname==='/knowledge').then(()=> 'navigated'),page.getByRole('dialog').waitFor({state:'visible'}).then(()=> 'confirmation')]);
  if(branch==='confirmation')await page.getByRole('button',{name:'Buang perubahan dan lanjut',exact:true}).click();
  await page.waitForURL(url=>url.pathname==='/knowledge');
}
async function waitPublished(number,documentId,versionId){
  await page.waitForFunction(number=>{
    const active=document.querySelector('.workspace-detail-grid > section');
    const replacement=[...document.querySelectorAll('button')].find(button=>button.textContent==='Unggah versi pengganti');
    return !!active&&active.textContent.includes('Versi '+number)&&active.textContent.includes('Diterbitkan')&&!!replacement&&!replacement.disabled;
  },number);
  const current=await read('/api/v1/knowledge/documents/'+documentId);
  if(current.status!==200||current.body.active_version_id!==versionId)throw new Error('actual_published_pointer_not_confirmed');return current;
}
async function lostOnce(pattern,kind){const bodies=[];let dropped=false;await page.route(pattern,async route=>{if(route.request().method()!=='POST'){await route.continue();return;}bodies.push(route.request().postData());replayStats[kind]={request_count:bodies.length,key_equal:bodies.length>1?JSON.parse(bodies[0]).key===JSON.parse(bodies.at(-1)).key:null,material_equal:bodies.length>1?bodies[0]===bodies.at(-1):null};if(!dropped){dropped=true;await route.fetch();await route.abort('failed');}else await route.continue();});return bodies;}
try{
  browser=await chromium.launch({channel:'msedge',headless:true});stage='initial_upload';await open();const original='Original policy. <b>Quoted source remains plain text.</b>';
  const uploadKey=await upload(original);const admissions=await lostOnce('**/api/v1/knowledge/documents','admission');await initialFields('Kebijakan perjalanan perusahaan dengan judul lengkap',true,true);
  check('named admission requires visible server creator grant review',true);
  await page.getByRole('button',{name:'Buat draft',exact:true}).click();await page.getByRole('heading',{name:'Hasil penyimpanan belum diketahui',exact:true}).waitFor();check('admission lost reply after actual commit stays unknown',admissions.length===1);
  await leaveCatalog();await page.getByRole('heading',{name:'Hasil tindakan dokumen belum diketahui',exact:true}).waitFor();await page.getByRole('button',{name:'Rekonsiliasi tindakan dokumen',exact:true}).click();await page.getByRole('heading',{name:'Riwayat versi',exact:true}).waitFor();
  const did=new URL(page.url()).pathname.split('/').at(-1);const doc1=(await read('/api/v1/knowledge/documents/'+did)).body;const first=doc1.latest_version.version_id;
  check('admission reentry replays identical request with a key distinct from raw upload',admissions.length===2&&admissions[0]===admissions[1]&&JSON.parse(admissions[0]).key!==uploadKey);
  check('admission is a draft without an active published version',doc1.active_version_id===null&&doc1.latest_version.publication_state==='draft');
  stage='preview_publish';await processOne();await reloadReady();await inspect();
  check('actual extracted preview binds exact version period and audience before publish',await page.locator('.extraction-preview').getByText(original,{exact:true}).count()===1&&await page.locator('.extraction-preview').getByText('2020-01-01',{exact:true}).count()===1&&await page.locator('.extraction-preview').getByText('2030-01-01',{exact:true}).count()===1);
  check('source markup is escaped and publish requires explicit manager review',!await page.locator('.extraction-preview b').count()&&await page.getByRole('button',{name:'Terbitkan versi ini',exact:true}).isDisabled());
  for(const width of [1440,768,390]){await page.setViewportSize({width,height:1000});await page.locator('.extraction-preview').evaluate(e=>e.scrollIntoView({block:'start'}));check('draft preview reflows at '+width,await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:out+'preview-light-'+width+'.png'});}
  const publishes=await lostOnce('**/api/v1/knowledge/versions/'+first+'/publish','publish');await publish();await page.getByRole('heading',{name:'Hasil tindakan dokumen belum diketahui',exact:true}).waitFor();check('publish lost reply does not leave the old extraction preview displayed',!await page.locator('.extraction-preview').count());
  await page.getByRole('button',{name:'Rekonsiliasi tindakan dokumen',exact:true}).click();await page.getByRole('heading',{name:'Hasil tindakan dokumen belum diketahui',exact:true}).waitFor({state:'hidden'});await page.getByRole('button',{name:'Unggah versi pengganti',exact:true}).waitFor();
  const publishedFirst=await waitPublished(1,did,first);check('publish replay preserves exact original key and material',publishes.length===2&&publishes[0]===publishes[1]);check('published active pointer comes from actual current state',publishedFirst.body.active_version_id===first);
  stage='replacement';await page.getByRole('button',{name:'Unggah versi pengganti',exact:true}).click();const replacementScopeFixed=await page.getByRole('combobox',{name:'Cakupan dokumen',exact:true}).isDisabled();await upload('Replacement policy. Updated text.',true);
  check('replacement scope is fixed and audience is inherited',replacementScopeFixed&&!await page.getByRole('combobox',{name:'Audience awal dokumen',exact:true}).count());
  await page.getByLabel('Saya telah memeriksa judul, periode dan audience draft ini',{exact:true}).check();await page.getByRole('button',{name:'Buat versi draft',exact:true}).click();await page.getByText('Versi aktif tetap digunakan sampai versi pengganti diterbitkan.',{exact:true}).waitFor();
  const doc2=(await read('/api/v1/knowledge/documents/'+did)).body;const second=doc2.latest_version.version_id;
  check('old published remains active until replacement publish and new dates do not inherit',doc2.active_version_id===first&&doc2.latest_version.publication_state==='draft'&&doc2.latest_version.valid_from===null&&doc2.latest_version.valid_until===null);
  check('reader source rejects unpublished replacement even for known version ID',(await read('/api/v1/knowledge/sources/'+second+'/content')).status===404);
  await processOne();await reloadReady();await inspect();await publish();await waitPublished(2,did,second);
  const oldSource=await read('/api/v1/knowledge/sources/'+first+'/content');const newSource=await read('/api/v1/knowledge/sources/'+second+'/content');check('replacement publish atomically supersedes old source and publishes the new exact version',oldSource.status===200&&oldSource.body.publication_state==='superseded'&&newSource.status===200&&newSource.body.publication_state==='published');
  stage='withdraw_unknown';await inspect();const withdrawals=await lostOnce('**/api/v1/knowledge/documents/'+did+'/withdraw','withdraw');await page.getByLabel('Saya memahami penarikan dokumen bersifat terminal untuk seluruh versi',{exact:true}).check();await page.getByRole('button',{name:'Tarik seluruh dokumen',exact:true}).click();await page.getByRole('heading',{name:'Hasil tindakan dokumen belum diketahui',exact:true}).waitFor();
  check('withdraw unknown masks all private detail and does not infer success from404',!await page.locator('.extraction-preview').count()&&!await page.getByRole('heading',{name:'Dokumen telah ditarik',exact:true}).count());
  await leaveCatalog();await page.getByRole('button',{name:'Rekonsiliasi tindakan dokumen',exact:true}).click();await page.getByRole('heading',{name:'Dokumen telah ditarik',exact:true}).waitFor();
  check('withdraw reentry reconciles the same original intent to terminal receipt',withdrawals.length===2&&withdrawals[0]===withdrawals[1]);
  check('terminal withdrawal denies source for both original and replacement versions',(await read('/api/v1/knowledge/sources/'+first+'/content')).status===404&&(await read('/api/v1/knowledge/sources/'+second+'/content')).status===404);
  await page.goto(fixture.origin+'/knowledge/documents/'+did);await page.getByRole('heading',{name:'Tidak tersedia',exact:true}).waitFor();check('fresh withdrawn detail is unavailable with no publish or new-version controls',!await page.getByRole('button',{name:'Unggah versi pengganti',exact:true}).count());
  stage='all_employees';await open();await upload('All employee policy. Fictional text.');await initialFields('Kebijakan seluruh karyawan',false);await page.getByRole('button',{name:'Buat draft',exact:true}).click();await page.getByRole('link',{name:'Periksa draft dokumen',exact:true}).click();await page.getByRole('heading',{name:'Riwayat versi',exact:true}).waitFor();const allId=new URL(page.url()).pathname.split('/').at(-1);await processOne();await reloadReady();await inspect();const allVersion=(await read('/api/v1/knowledge/documents/'+allId)).body.latest_version.version_id;await publish();await waitPublished(1,allId,allVersion);
  check('all-employees admission is explicit and publishes without reader approval',(await read('/api/v1/knowledge/documents/'+allId+'/audience?limit=50')).body.audience_mode==='all_current_employees');
  stage='negative_scope';await open(fixture.employee,'/chat');await page.getByRole('button',{name:'Keluar',exact:true}).waitFor();check('actual employee reads published all-employees source',(await read('/api/v1/knowledge/sources/'+allVersion+'/content')).status===200);
  for(const [name,token] of [['foreign',fixture.foreign],['collection-only',fixture.scoped]]){await open(token,'/knowledge/documents/'+allId);await page.getByRole('heading',{name:'Tidak tersedia',exact:true}).waitFor();check(name+' manager cannot preview publish or replace this document',!await page.getByRole('button',{name:'Buka preview ekstraksi',exact:true}).count());}
  await open(fixture.admin,'/knowledge/documents/'+allId);await page.getByRole('heading',{name:'Halaman tidak tersedia untuk akses Anda',exact:true}).waitFor();check('tenant admin has no automatic knowledge lifecycle authority',true);
  stage='english_dark';await open(fixture.manager,'/appearance');await page.getByLabel('Bahasa antarmuka',{exact:true}).selectOption('en');await page.getByLabel('Theme',{exact:true}).selectOption('dark');await page.goto(fixture.origin+'/knowledge/documents/'+allId);await page.getByRole('button',{name:'Open extraction preview',exact:true}).click();await page.locator('.extraction-preview').waitFor();await page.setViewportSize({width:390,height:1000});await page.locator('.extraction-preview').evaluate(e=>e.scrollIntoView({block:'start'}));check('English dark extraction preview remains usable on mobile',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:out+'preview-dark-390.png'});
  stage='failed_ingestion';await open();await upload('A deliberate controlled extraction failure.');await initialFields('Dokumen gagal ekstraksi',false);await page.getByRole('button',{name:'Buat draft',exact:true}).click();await page.getByRole('link',{name:'Periksa draft dokumen',exact:true}).click();await page.getByRole('heading',{name:'Riwayat versi',exact:true}).waitFor();await processOne(true);await page.getByRole('button',{name:'Muat ulang versi dan status',exact:true}).click();await page.locator('.version-history-error').waitFor();await page.getByRole('button',{name:'Buka preview ekstraksi',exact:true}).waitFor();
  check('actual controlled ingestion failure cannot be previewed or published and offers no parser rerun',await page.getByRole('button',{name:'Buka preview ekstraksi',exact:true}).isDisabled()&&!await page.getByRole('button',{name:'Terbitkan versi ini',exact:true}).count());
  check('private knowledge state is not persisted in browser storage',await page.evaluate(()=>localStorage.length===0&&sessionStorage.length===0));
  await context.setOffline(true);await page.getByRole('heading',{name:'Riwayat versi',exact:true}).waitFor({state:'hidden'});check('context invalidation purges private document and lifecycle controls',!await page.getByRole('button',{name:'Buka preview ekstraksi',exact:true}).count());
  await writeFile(out+'browser-proof.json',JSON.stringify({passed:true,checks,replay_stats:replayStats,browser_version:browser.version(),provider_login:'controlled_fixture_session_not_tested',physical_device:'not_tested',fixtures:'Actual restricted DB/API and jobs; storage/parser/embedding controlled. Admission/publish/withdraw lost replies occur after real commits. Extractor failure is deliberate. Three documents/four versions; no real provider inference.'},null,2));console.log(JSON.stringify({passed:true,checks:checks.length}));
}catch(error){await page?.screenshot({path:out+'failure.png'}).catch(()=>{});console.log(JSON.stringify({passed:false,stage,replay_stats:replayStats,error_name:error instanceof Error?error.name:'unknown',checks}));process.exitCode=1;}
finally{clearTimeout(watchdog);await context?.close();await browser?.close();}
