import {writeFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {chromium} from '../../frontend/node_modules/playwright-core/index.mjs';
let input='';for await(const chunk of process.stdin) input+=chunk;
const fixture=JSON.parse(input);input='';
const out=fileURLToPath(new URL('../../local/fe-know-02/',import.meta.url));
const checks=[];let browser,context,page,stage='launch';
const watchdog=setTimeout(()=>{void browser?.close();},100000);
function check(name,passed){checks.push({name,passed:!!passed});if(!passed)throw new Error('controlled_check_failed');}
async function open(token,path='/knowledge/upload',preferences=false){
  await context?.close();context=await browser.newContext({ignoreHTTPSErrors:true,serviceWorkers:'block'});
  await context.route('**/*',route=>new URL(route.request().url()).origin===fixture.origin?route.continue():route.abort());
  await context.addCookies([{name:'__Host-eai-session',value:token,url:fixture.origin,httpOnly:true,secure:true,sameSite:'Lax'}]);
  page=await context.newPage();page.setDefaultTimeout(9000);
  if(preferences){
    await page.goto(fixture.origin+'/appearance');await page.getByLabel('Bahasa antarmuka',{exact:true}).selectOption('en');await page.getByLabel('Theme',{exact:true}).selectOption('dark');
  }
  await page.goto(fixture.origin+path);
}
async function choose(text,scope='tenant',english=false){
  await page.getByRole('combobox',{name:english?'Document scope':'Cakupan dokumen',exact:true}).selectOption(scope);
  await page.getByLabel(english?'Knowledge file':'File pengetahuan',{exact:true}).setInputFiles({name:'Kebijakan perjalanan dengan nama lengkap yang tetap terbaca di layar kecil.md',mimeType:'text/markdown',buffer:Buffer.from(text)});
}
async function send(){await page.getByRole('button',{name:'Unggah file',exact:true}).click();}
async function processOne(){const result=await context.request.post(fixture.origin+'/__fixture/process',{headers:{'x-fixture-control':fixture.control}});check('owned fixture executes one real queued job '+checks.length,result.status()===200);}
try{
  browser=await chromium.launch({channel:'msedge',headless:true});
  stage='existing_version';await open(fixture.manager,'/knowledge/documents/'+fixture.document);
  await page.getByRole('button',{name:'Periksa pemrosesan versi',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.processing-status dl')?.textContent.includes('Menunggu'));
  check('existing version shows parse index and publication separately',await page.locator('.processing-status dt').allTextContents().then(x=>x.join('|')==='Ekstraksi teks|Indeks pengetahuan|Publikasi'));
  await processOne();await page.waitForFunction(()=>[...document.querySelectorAll('.processing-status dd')].map(e=>e.textContent).join('|')==='Siap|Siap|Draft');
  check('real controlled ingestion reaches ready draft without publishing',true);

  stage='markdown_upload';await open(fixture.manager);await choose('# Fictional policy\nText only.');
  check('full selected filename remains visible',await page.getByText(/Kebijakan perjalanan dengan nama lengkap yang tetap terbaca di layar kecil.md/).count()===1);
  const receiptPromise=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/v1/files'&&r.request().method()==='POST');
  await send();const receipt=await (await receiptPromise).json();
  await page.getByRole('heading',{name:'File dalam karantina',exact:true}).waitFor();
  check('actual upload 202 is quarantine and no completion percentage',receipt.mime==='text/markdown'&&receipt.scan_state==='quarantine'&&!await page.getByRole('progressbar').count());
  await processOne();await page.getByRole('heading',{name:'File siap untuk pembuatan draft',exact:true}).waitFor();
  check('actual file inspection ready is not admission or publication',await page.getByText('File telah lolos pemeriksaan. File ini belum menjadi dokumen dan belum diterbitkan.',{exact:true}).count()===1);
  for(const width of [1440,768,390]){
    await page.setViewportSize({width,height:1000});await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    check('upload result reflows without page overflow at '+width,await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:out+'upload-light-'+width+'.png'});
  }
  check('private upload data is absent from storage',await page.evaluate(()=>localStorage.length===0&&sessionStorage.length===0));
  check('keyboard can reach an enabled action',await page.getByRole('button',{name:'Pilih file lain',exact:true}).evaluate(e=>{e.focus();return document.activeElement===e;}));

  stage='rejected_file';await open(fixture.manager);await choose('invalid-fixture controlled parser rejection');await send();await page.getByRole('heading',{name:'File dalam karantina',exact:true}).waitFor();
  await processOne();await page.getByRole('heading',{name:'File ditolak',exact:true}).waitFor();
  check('real controlled parser rejection never becomes ready or offers parser rerun',!await page.getByRole('heading',{name:'File siap untuk pembuatan draft',exact:true}).count()&&!await page.getByRole('button',{name:/proses ulang/i}).count());

  stage='unknown_reentry';await open(fixture.manager);const sent=[];let dropped=false;
  await page.route('**/api/v1/files?*',async route=>{
    if(route.request().method()!=='POST'){await route.continue();return;}
    sent.push({key:route.request().headers()['idempotency-key'],bytes:route.request().postDataBuffer()});
    if(!dropped){dropped=true;await route.fetch();await route.abort('failed');}else await route.continue();
  });
  await choose('# Lost response after actual commit');await send();await page.getByRole('heading',{name:'Hasil unggahan belum diketahui',exact:true}).waitFor();
  await page.getByRole('link',{name:'Kembali ke dokumen',exact:true}).click();await page.getByRole('button',{name:'Buang perubahan dan lanjut',exact:true}).click();
  await page.getByRole('link',{name:'Unggah file',exact:true}).click();await page.getByRole('heading',{name:'Hasil unggahan belum diketahui',exact:true}).waitFor();
  check('reentry blocks new upload while retaining original unknown intent',sent.length===1&&await page.getByLabel('File pengetahuan',{exact:true}).isDisabled());
  await page.getByRole('button',{name:'Periksa hasil unggahan',exact:true}).click();await page.getByRole('heading',{name:'File dalam karantina',exact:true}).waitFor();
  check('explicit reconciliation reuses identical key and bytes after real committed lost reply',sent.length===2&&sent[0].key===sent[1].key&&sent[0].bytes.equals(sent[1].bytes));
  await processOne();await page.getByRole('heading',{name:'File siap untuk pembuatan draft',exact:true}).waitFor();

  stage='status_error';await open(fixture.manager);await page.route('**/api/v1/files/*/status',route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:{code:'unavailable',message:'controlled fault',request_id:'77777777-7777-4777-8777-777777777777',retryable:true}})}));
  await choose('# Controlled read fault');await send();await page.getByRole('heading',{name:'Status belum dapat dimuat',exact:true}).waitFor();
  check('labelled 503 status fault has no stale ready fallback',!await page.getByRole('heading',{name:'File siap untuk pembuatan draft',exact:true}).count());
  await page.unroute('**/api/v1/files/*/status');await page.getByRole('button',{name:'Periksa status lagi',exact:true}).click();await page.getByRole('heading',{name:'File dalam karantina',exact:true}).waitFor();
  await processOne();await page.getByRole('heading',{name:'File siap untuk pembuatan draft',exact:true}).waitFor();
  await context.setOffline(true);await page.getByRole('heading',{name:'File siap untuk pembuatan draft',exact:true}).waitFor({state:'hidden'});
  check('offline invalidation removes upload result and file metadata',!await page.getByRole('heading',{name:'File siap untuk pembuatan draft',exact:true}).count()&&!await page.getByText(receipt.id,{exact:true}).count());
  await context.setOffline(false);

  stage='scope_and_language';await open(fixture.scoped,'/knowledge/upload',true);await page.setViewportSize({width:390,height:1000});
  await page.getByRole('option',{name:fixture.scope,exact:true}).waitFor({state:'attached'});
  check('collection manager cannot choose company-wide scope',!await page.getByRole('option',{name:'Company-level documents',exact:true}).count());
  await choose('# Fictional scoped upload',fixture.scope,true);
  check('English dark mobile form keeps filename and controls usable',await page.getByRole('button',{name:'Upload file',exact:true}).isEnabled()&&await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  await page.screenshot({path:out+'upload-dark-390.png'});
  const scopedUpload=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/v1/files'&&r.request().method()==='POST');
  await page.getByRole('button',{name:'Upload file',exact:true}).click();const scopedResponse=await scopedUpload;
  check('scoped manager uploads through the actual authorized collection API',scopedResponse.status()===202&&new URL(scopedResponse.url()).searchParams.get('collection_id')===fixture.scope);
  await page.getByRole('heading',{name:'File in quarantine',exact:true}).waitFor();await processOne();await page.getByRole('heading',{name:'File ready for draft creation',exact:true}).waitFor();
  for(const [name,token] of [['employee',fixture.employee],['admin',fixture.admin]]){
    await open(token);await page.getByRole('heading',{name:'Halaman tidak tersedia untuk akses Anda',exact:true}).waitFor();check(name+' cannot open upload management route',!await page.getByLabel('File pengetahuan',{exact:true}).count());
  }
  stage='foreign';await open(fixture.foreign);await page.getByRole('heading',{name:'Unggah file pengetahuan',exact:true}).waitFor();
  // Probe through browser fetch under the real same-origin lock so bootstrap
  // has browser Fetch Metadata and does not race the application's CSRF cycle.
  const foreign=await page.evaluate(async fileId=>navigator.locks.request('eai-session-context-v1',{mode:'exclusive'},async()=>{
    const me=await fetch('/api/v1/me',{headers:{'X-EAI-CSRF-Bootstrap':'1'},cache:'no-store'});
    if(!me.ok)return {bootstrap:me.status,status:0};const identity=await me.json();
    const response=await fetch('/api/v1/files/'+fileId+'/status',{headers:{'X-EAI-Expected-Context':identity.context_binding},cache:'no-store'});
    return {bootstrap:me.status,status:response.status};
  }),receipt.id);
  check('foreign tenant cannot inspect a known uploaded file ID',foreign.bootstrap===200&&foreign.status===404);
  await writeFile(out+'browser-proof.json',JSON.stringify({passed:true,checks,browser_version:browser.version(),provider_login:'controlled_fixture_session_not_tested',physical_device:'not_tested',fixtures:'Real restricted PostgreSQL and application HTTP; storage/parser/embedding controlled. Lost upload reply and status 503 are explicitly injected browser faults. Five Markdown upload intents including actual collection-scoped upload.'},null,2));
  console.log(JSON.stringify({passed:true,checks:checks.length}));
}catch(error){await page?.screenshot({path:out+'failure.png'}).catch(()=>{});console.log(JSON.stringify({passed:false,stage,error_name:error instanceof Error?error.name:'unknown',checks}));process.exitCode=1;}
finally{clearTimeout(watchdog);await context?.close();await browser?.close();}
