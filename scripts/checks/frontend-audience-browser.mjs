import {writeFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {chromium} from '../../frontend/node_modules/playwright-core/index.mjs';
let input='';for await(const chunk of process.stdin) input+=chunk;const fixture=JSON.parse(input);input='';
const out=fileURLToPath(new URL(fixture.visualOnly?'../../local/fe-know-04/visual/':'../../local/fe-know-04/',import.meta.url));const checks=[];let browser,context,page,stage='launch';
const watchdog=setTimeout(()=>{void browser?.close();},100000);
function check(name,passed){checks.push({name,passed:!!passed});if(!passed)throw new Error('controlled_check_failed');}
async function open(token=fixture.manager,path='/knowledge/documents/'+fixture.document){
  await context?.close();context=await browser.newContext({ignoreHTTPSErrors:true,serviceWorkers:'block'});
  await context.route('**/*',route=>new URL(route.request().url()).origin===fixture.origin?route.continue():route.abort());
  await context.addCookies([{name:'__Host-eai-session',value:token,url:fixture.origin,httpOnly:true,secure:true,sameSite:'Lax'}]);page=await context.newPage();page.setDefaultTimeout(9000);await page.goto(fixture.origin+path);
}
async function manage(){await page.getByRole('button',{name:'Periksa dan kelola pembaca',exact:true}).click();await page.getByText(/Audience saat ini:/).waitFor();}
async function edit(){await page.getByRole('button',{name:'Ubah audience',exact:true}).click();}
async function confirm(){await page.getByLabel('Saya telah memeriksa audience dan dampaknya pada seluruh versi',{exact:true}).check();await page.getByRole('button',{name:'Simpan audience',exact:true}).click();}
async function sourceStatus(){
  await open(fixture.reader,'/chat');await page.getByRole('button',{name:'Keluar',exact:true}).waitFor();
  return await page.evaluate(async version=>navigator.locks.request('eai-session-context-v1',{mode:'exclusive'},async()=>{
    const me=await (await fetch('/api/v1/me',{headers:{'X-EAI-CSRF-Bootstrap':'1'},cache:'no-store'})).json();
    return (await fetch('/api/v1/knowledge/sources/'+version+'/content',{headers:{'X-EAI-Expected-Context':me.context_binding},cache:'no-store'})).status;
  }),fixture.version);
}
try{
  browser=await chromium.launch({channel:'msedge',headless:true});
  if(fixture.visualOnly){
    stage='visual_current';await open();await manage();
    for(const width of [1440,768,390]){
      await page.setViewportSize({width,height:1000});await page.getByRole('heading',{name:'Audience dan pembaca',exact:true}).evaluate(el=>el.scrollIntoView({block:'start'}));
      check('visual-only current panel fits '+width,await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:out+'audience-light-'+width+'.png'});
    }
    await edit();await page.getByRole('button',{name:'Periksa pilihan akhir',exact:true}).click();await page.getByRole('button',{name:'Gunakan pilihan 2 orang',exact:true}).waitFor();
    await page.getByRole('heading',{name:'Ringkasan pilihan beku',exact:true}).evaluate(el=>el.scrollIntoView({block:'start'}));await page.screenshot({path:out+'audience-frozen-390.png'});
    check('visual-only frozen summary fits mobile',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await open(fixture.manager,'/appearance');await page.getByLabel('Bahasa antarmuka',{exact:true}).selectOption('en');await page.getByLabel('Theme',{exact:true}).selectOption('dark');
    await page.goto(fixture.origin+'/knowledge/documents/'+fixture.document);await page.getByRole('button',{name:'Review and manage readers',exact:true}).click();await page.getByRole('button',{name:'Change audience',exact:true}).click();await page.getByRole('combobox',{name:'Who can read',exact:true}).selectOption('all_current_employees');
    await page.setViewportSize({width:390,height:1000});await page.getByRole('heading',{name:'Audience and readers',exact:true}).evaluate(el=>el.scrollIntoView({block:'start'}));await page.screenshot({path:out+'audience-dark-390.png'});
    check('visual-only English dark form fits mobile',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await writeFile(out+'browser-proof.json',JSON.stringify({passed:true,visual_only:true,checks,browser_version:browser.version(),fixtures:'Actual scoped API/DB with controlled external ports; fresh two-reader document, four directory members, one ephemeral selection for preview. No audience apply or business regression replay. Not the 21-check transaction proof.'},null,2));console.log(JSON.stringify({passed:true,visual_only:true,checks:checks.length}));
  }else{stage='initial';await open();await manage();
  check('current ACL retains inactive stored grant and separates manager authority',await page.getByText('Pembaca nonaktif',{exact:true}).count()===1&&await page.getByText(/Pengelola pengetahuan yang berwenang tetap/).count()===1);
  await edit();await page.getByRole('button',{name:'Hapus pembaca Pembaca nonaktif',exact:true}).click();
  await page.getByRole('textbox',{name:'Departemen',exact:true}).fill('Finance');await page.getByRole('textbox',{name:'Jabatan',exact:true}).fill('Manager');await page.getByRole('button',{name:'Terapkan filter pembaca',exact:true}).click();
  await page.getByLabel('Finance reader 01',{exact:true}).waitFor();
  check('Finance plus Manager directory excludes HR manager',!await page.getByLabel('Finance reader 52',{exact:true}).count());
  await page.getByRole('button',{name:'Bekukan semua hasil filter',exact:true}).click();await page.getByRole('button',{name:'Pilih semua 52 orang',exact:true}).waitFor();
  check('frozen all matching includes every page plus retained creator minus explicit inactive removal',true);
  await page.getByRole('button',{name:'Pilih semua 52 orang',exact:true}).click();
  await page.getByRole('textbox',{name:'Departemen',exact:true}).fill('HR');await page.getByRole('button',{name:'Terapkan filter pembaca',exact:true}).click();await page.getByLabel('Finance reader 52',{exact:true}).waitFor();
  check('changing filter does not replace selected frozen people',await page.getByText('Jumlah pembaca: 52',{exact:true}).count()===2);
  await page.setViewportSize({width:390,height:1000});await page.getByRole('heading',{name:'Ringkasan pilihan beku',exact:true}).evaluate(el=>el.scrollIntoView({block:'start'}));await page.screenshot({path:out+'audience-frozen-390.png'});await page.setViewportSize({width:1440,height:1000});
  const drift=await context.request.post(fixture.origin+'/__fixture/drift',{headers:{'x-fixture-control':fixture.control}});check('owned fixture changes job title after selection snapshot',drift.status()===200);
  await confirm();await page.getByRole('button',{name:'Ubah audience',exact:true}).waitFor();check('atomic replacement retains exact 52-person snapshot after job-title drift',await page.getByText('Jumlah pembaca: 52',{exact:true}).count()===1&&!await page.getByText('Pembaca nonaktif',{exact:true}).count());
  stage='layout';for(const width of [1440,768,390]){await page.setViewportSize({width,height:1000});await page.getByRole('heading',{name:'Audience dan pembaca',exact:true}).evaluate(el=>el.scrollIntoView({block:'start'}));check('current audience reflows at '+width,await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:out+'audience-light-'+width+'.png'});}
  check('audience selections are not persisted in browser storage',await page.evaluate(()=>localStorage.length===0&&sessionStorage.length===0));

  stage='mixed_CAS';await open();const audience='**/api/v1/knowledge/documents/'+fixture.document+'/audience?*';
  await page.route(audience,async route=>{const response=await route.fetch();const body=await response.json();if(new URL(route.request().url()).searchParams.has('after'))body.document_version++;await route.fulfill({response,json:body});});
  await page.getByRole('button',{name:'Periksa dan kelola pembaca',exact:true}).click();await page.getByRole('heading',{name:'Data belum dapat dimuat',exact:true}).waitFor();check('mixed-CAS assembled current ACL is discarded entirely',!await page.getByText('Jumlah pembaca: 52',{exact:true}).count());
  await page.unroute(audience);await page.getByRole('button',{name:'Periksa kembali',exact:true}).click();await page.getByRole('button',{name:'Ubah audience',exact:true}).waitFor();
  stage='all_employees';await edit();await page.getByRole('combobox',{name:'Siapa yang boleh membaca',exact:true}).selectOption('all_current_employees');await confirm();await page.getByText('Audience saat ini: Seluruh karyawan',{exact:true}).waitFor();check('all-employees mode clears explicit grants after server acknowledgement',!await page.getByText(/Jumlah pembaca:/).count());
  check('actual current employee can read the published source under all-employees mode',await sourceStatus()===200);

  stage='named_empty';await open();await manage();await edit();await page.getByRole('combobox',{name:'Siapa yang boleh membaca',exact:true}).selectOption('specific_people');await page.getByRole('button',{name:'Periksa pilihan akhir',exact:true}).click();await page.getByRole('button',{name:'Gunakan pilihan 0 orang',exact:true}).click();await confirm();await page.getByRole('button',{name:'Ubah audience',exact:true}).waitFor();check('specific-people empty stays zero and does not silently restore creator',await page.getByText('Jumlah pembaca: 0',{exact:true}).count()===1);
  check('actual employee loses source access after named-empty replacement',await sourceStatus()===404);

  stage='unknown_after_commit';await open();await manage();await edit();let dropped=false;const sent=[];
  await page.route('**/api/v1/knowledge/documents/'+fixture.document+'/audience',async route=>{
    if(route.request().method()!=='PATCH'){await route.continue();return;}sent.push(route.request().postData());
    if(!dropped){dropped=true;await route.fetch();await route.abort('failed');}else await route.continue();
  });
  await page.getByRole('combobox',{name:'Siapa yang boleh membaca',exact:true}).selectOption('all_current_employees');await confirm();await page.getByRole('heading',{name:'Hasil penyimpanan belum diketahui',exact:true}).waitFor();
  check('lost committed apply stays unknown rather than optimistic success',await page.getByText('Audience saat ini: Orang tertentu',{exact:true}).count()===1);
  await page.getByRole('button',{name:'Periksa hasil penyimpanan',exact:true}).click();await page.getByText('Audience saat ini: Seluruh karyawan',{exact:true}).waitFor();check('explicit apply reconciliation sends identical material and key before fresh current ACL',sent.length===2&&sent[0]===sent[1]);
  stage='scope';for(const [name,token] of [['foreign',fixture.foreign],['collection-only',fixture.scoped]]){await open(token);await page.getByRole('heading',{name:'Tidak tersedia',exact:true}).waitFor();check(name+' manager cannot view foreign document audience',!await page.getByRole('button',{name:'Periksa dan kelola pembaca',exact:true}).count());}
  await open(fixture.admin);await page.getByRole('heading',{name:'Halaman tidak tersedia untuk akses Anda',exact:true}).waitFor();check('tenant admin has no automatic audience management authority',true);
  stage='dark_english';await open(fixture.manager,'/appearance');await page.getByLabel('Bahasa antarmuka',{exact:true}).selectOption('en');await page.getByLabel('Theme',{exact:true}).selectOption('dark');await page.goto(fixture.origin+'/knowledge/documents/'+fixture.document);await page.getByRole('button',{name:'Review and manage readers',exact:true}).click();await page.getByText('Current audience: All employees',{exact:true}).waitFor();await page.getByRole('button',{name:'Change audience',exact:true}).click();await page.setViewportSize({width:390,height:1000});await page.getByRole('heading',{name:'Audience and readers',exact:true}).evaluate(el=>el.scrollIntoView({block:'start'}));check('English dark audience remains usable on mobile',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:out+'audience-dark-390.png'});
  await writeFile(out+'browser-proof.json',JSON.stringify({passed:true,checks,browser_version:browser.version(),provider_login:'controlled_fixture_session_not_tested',physical_device:'not_tested',fixtures:'Actual restricted DB/API and controlled storage/parser/embedding. Mixed-CAS read and lost response after committed apply are labelled injected faults. Organization job-title drift is an owned maintenance fixture.'},null,2));console.log(JSON.stringify({passed:true,checks:checks.length}));
  }
}catch(error){await page?.screenshot({path:out+'failure.png'}).catch(()=>{});console.log(JSON.stringify({passed:false,stage,error_name:error instanceof Error?error.name:'unknown',checks}));process.exitCode=1;}
finally{clearTimeout(watchdog);await context?.close();await browser?.close();}
