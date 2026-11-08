import {writeFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {chromium} from '../../frontend/node_modules/playwright-core/index.mjs';
let input='';for await (const chunk of process.stdin) input+=chunk;
const fixture=JSON.parse(input);input='';
const out=fileURLToPath(new URL('../../local/fe-know-01/',import.meta.url));
const checks=[];let browser,context,page,stage='launch';
function check(name,passed){checks.push({name,passed:!!passed});if(!passed)throw new Error('controlled_check_failed');}
const watchdog=setTimeout(()=>{void browser?.close();},100000);
async function open(token,path='/knowledge'){
  await context?.close();context=await browser.newContext({ignoreHTTPSErrors:true,serviceWorkers:'block'});
  await context.route('**/*',route=>new URL(route.request().url()).origin===fixture.origin?route.continue():route.abort());
  await context.addCookies([{name:'__Host-eai-session',value:token,url:fixture.origin,httpOnly:true,secure:true,sameSite:'Lax'}]);
  page=await context.newPage();page.setDefaultTimeout(9000);await page.goto(fixture.origin+path);
}
async function filter(value){await page.getByRole('textbox',{name:'Cari judul dokumen',exact:true}).fill(value);await page.getByRole('button',{name:'Terapkan filter',exact:true}).click();}
try{
  browser=await chromium.launch({channel:'msedge',headless:true});
  stage='catalog';await open(fixture.manager);await page.getByRole('link',{name:'Kebijakan perjalanan perusahaan',exact:true}).waitFor();
  check('catalog finds drafts and the published document without history or lexical-search fan-out',await page.locator('.workspace-list > li').count()===20);
  await page.getByRole('button',{name:'Berikutnya',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('.workspace-list > li').length===3);
  check('document cursor reaches the second page beyond twenty records',await page.locator('.workspace-list > li').count()===3&&await page.getByRole('button',{name:'Berikutnya',exact:true}).isDisabled());
  await page.getByRole('button',{name:'Sebelumnya',exact:true}).click();await page.getByRole('link',{name:'Kebijakan perjalanan perusahaan',exact:true}).waitFor();
  await page.getByRole('button',{name:'Koleksi berikutnya',exact:true}).click();await page.waitForFunction(()=>document.querySelector('select')?.options.length===4);
  check('collection discovery paginates beyond twenty real authorized scopes',await page.getByRole('combobox',{name:'Cakupan dokumen',exact:true}).locator('option').count()===4);
  await filter('Kebijakan*');await page.getByRole('heading',{name:'Belum ada dokumen yang cocok',exact:true}).waitFor();check('title filters keep star literal without saving private query in page URL',!page.url().includes('?'));
  await filter('Kebijakan perjalanan');await page.getByRole('link',{name:'Kebijakan perjalanan perusahaan',exact:true}).click();
  stage='versions';await page.getByRole('heading',{name:'Versi 22',exact:true}).waitFor();
  check('detail separates active published version one from newest draft twenty-two',await page.getByText('Versi 1',{exact:true}).count()===1&&await page.getByText('Versi 22',{exact:true}).count()===2);
  check('parse and index are distinct and null effective dates remain unrecorded',await page.getByText('Ekstraksi teks',{exact:true}).count()>0&&await page.getByText('Indeks pengetahuan',{exact:true}).count()>0&&await page.getByText('Belum dicatat',{exact:true}).count()>0);
  await page.getByRole('button',{name:'Versi berikutnya',exact:true}).click();await page.getByRole('heading',{name:'Versi 1',exact:true}).waitFor();
  check('version pagination shows the final two versions without mixing first-page rows',await page.locator('.workspace-list > li').count()===2&&!await page.getByRole('heading',{name:'Versi 22',exact:true}).count());
  await page.getByRole('button',{name:'Versi sebelumnya',exact:true}).click();await page.getByRole('heading',{name:'Versi 22',exact:true}).waitFor();
  for(const width of [1440,768,390]){await page.setViewportSize({width,height:1000});await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));check('knowledge detail has no horizontal overflow at '+width,await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:out+'knowledge-light-'+width+'.png'});}
  stage='cas_fixture';const versions='**/api/v1/knowledge/documents/'+fixture.document+'/versions?*';
  await page.route(versions,async route=>{const response=await route.fetch();const body=await response.json();body.document_version++;body.items=body.items.map(item=>({...item,document_version:body.document_version}));await route.fulfill({response,json:body});});
  await page.getByRole('button',{name:'Periksa status terbaru',exact:true}).click();await page.getByRole('heading',{name:'Dokumen telah berubah',exact:true}).waitFor();
  check('controlled mismatched-CAS response discards version rows and requests current reload',await page.locator('.workspace-list > li').count()===0);
  await page.unroute(versions);await page.getByRole('button',{name:'Muat ulang dokumen',exact:true}).click();await page.getByRole('heading',{name:'Versi 22',exact:true}).waitFor();
  stage='cancel';await page.goto(fixture.origin+'/knowledge');await page.getByRole('link',{name:'Kebijakan perjalanan perusahaan',exact:true}).waitFor();let release,entered;
  const started=new Promise(resolve=>{entered=resolve;});const held=new Promise(resolve=>{release=resolve;});
  await page.route('**/api/v1/knowledge/documents?*',async route=>{if(new URL(route.request().url()).searchParams.get('q')==='slow'){entered();await held;try{await route.abort();}catch{}}else await route.continue();});
  await filter('slow');await started;await page.setViewportSize({width:1440,height:1000});await page.getByRole('link',{name:'Tampilan',exact:true}).click();release();
  await page.getByLabel('Bahasa antarmuka',{exact:true}).selectOption('en');await page.getByLabel('Theme',{exact:true}).selectOption('dark');
  await page.unroute('**/api/v1/knowledge/documents?*');await page.getByRole('link',{name:'Back to workspace',exact:true}).click();await page.getByRole('link',{name:'Documents',exact:true}).click();await page.getByRole('link',{name:'Kebijakan perjalanan perusahaan',exact:true}).click();await page.getByRole('heading',{name:'Version 22',exact:true}).waitFor();
  check('cancelled catalog navigation preserves verified auth and a fresh detail can load',await page.getByRole('button',{name:'Sign out',exact:true}).count()===1);
  await page.setViewportSize({width:390,height:1000});check('English dark detail reflows on mobile',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:out+'knowledge-dark-390.png'});
  stage='new_session';await open(fixture.newSession);await page.getByRole('link',{name:'Kebijakan perjalanan perusahaan',exact:true}).waitFor();check('same-owner genuine new application session rediscovers the catalog from the server',await page.getByRole('link',{name:'Kebijakan perjalanan perusahaan',exact:true}).count()===1);
  stage='scope';await open(fixture.scoped);await page.getByRole('link',{name:'Dokumen koleksi terbatas',exact:true}).waitFor();
  check('collection-only manager sees only the authorized document and no company-wide option',await page.locator('.workspace-list > li').count()===1&&!await page.getByRole('option',{name:'Dokumen tingkat perusahaan',exact:true}).count());
  await page.goto(fixture.origin+'/knowledge/documents/'+fixture.document);await page.getByRole('heading',{name:'Tidak tersedia',exact:true}).waitFor();check('known uncollected document ID cannot bypass collection manager scope',!await page.getByRole('heading',{name:'Kebijakan perjalanan perusahaan',exact:true}).count());
  stage='foreign';await open(fixture.foreign,'/knowledge/documents/'+fixture.document);await page.getByRole('heading',{name:'Tidak tersedia',exact:true}).waitFor();check('foreign manager cannot render document title or versions',!await page.getByRole('heading',{name:'Riwayat versi',exact:true}).count());
  stage='employee';await open(fixture.employee);await page.getByRole('heading',{name:'Halaman tidak tersedia untuk akses Anda',exact:true}).waitFor();check('employee is not granted catalog authority',!await page.locator('.workspace-list > li').count());
  await writeFile(out+'browser-proof.json',JSON.stringify({passed:true,checks,browser_version:browser.version(),provider_login:'controlled_fixture_session_not_tested',physical_device:'not_tested',fixtures:'Main/scoped documents use actual controlled ingestion. Bulk pagination rows are maintenance-owned metadata fixtures. Mismatched CAS and slow read are explicit browser fault fixtures.'},null,2));console.log(JSON.stringify({passed:true,checks:checks.length}));
}catch{console.log(JSON.stringify({passed:false,stage,checks}));process.exitCode=1;}
finally{clearTimeout(watchdog);await context?.close();await browser?.close();}
