import {writeFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {chromium} from '../../frontend/node_modules/playwright-core/index.mjs';
let input='';for await(const chunk of process.stdin)input+=chunk;
const fixture=JSON.parse(input);input='';const out=fileURLToPath(new URL('../../local/fe-source-01/',import.meta.url));
const checks=[];let browser,context,page,stage='launch';
function check(name,passed){checks.push({name,passed:!!passed});if(!passed)throw new Error('controlled_check_failed');}
async function save(name,passed){await writeFile(out+name+'.json',JSON.stringify({passed,stage,checks:[...checks],browser_version:browser?.version(),provider_login:'controlled_fixture_session',physical_device:'not_tested'},null,2));}
const watchdog=setTimeout(()=>{void browser?.close();},100000);
async function open(token,path='/search'){
 await context?.close();context=await browser.newContext({ignoreHTTPSErrors:true,serviceWorkers:'block'});
 await context.route('**/*',route=>new URL(route.request().url()).origin===fixture.origin?route.continue():route.abort());
 await context.addCookies([{name:'__Host-eai-session',value:token,url:fixture.origin,httpOnly:true,secure:true,sameSite:'Lax'}]);
 page=await context.newPage();page.setDefaultTimeout(7000);await page.goto(fixture.origin+path);
}
async function search(query='perjalanan',language='id'){
 await page.getByRole('textbox',{name:'Kata pencarian',exact:true}).fill(query);await page.getByRole('combobox',{name:'Bahasa pencarian',exact:true}).selectOption(language);await page.getByRole('button',{name:'Cari',exact:true}).click();
}
async function control(action){const response=await context.request.post(fixture.origin+'/__fixture/stage',{headers:{'x-fixture-control':fixture.control,'x-fixture-action':action}});check('actual fixture stage '+action+' commits',response.status()===200);}
async function inspectSource(){await page.getByRole('button',{name:'Kebijakan perjalanan — teks sumber lengkap',exact:true}).click();await page.locator('mark').waitFor();}
try{
 browser=await chromium.launch({channel:'msedge',headless:true});
 stage='positive-search';await open(fixture.employee);await page.getByRole('textbox',{name:'Kata pencarian',exact:true}).waitFor();
 const routes=[];page.on('request',request=>{const path=new URL(request.url()).pathname;if(path.startsWith('/api/'))routes.push(path);});
 await search();await page.getByRole('button',{name:'Kebijakan perjalanan — teks sumber lengkap',exact:true}).waitFor();
 check('reader manual lexical search shows only active published version',await page.locator('.source-results > li').count()===1&&await page.getByText('Versi 2 · Halaman 1 · Chunk 0',{exact:true}).count()===1);
 check('private query stays out of address/history and browser storage',!page.url().includes('?')&&await page.evaluate(()=>Object.keys(localStorage).length===0&&Object.keys(sessionStorage).length===0));
 check('reader never uses manager directory or organization discovery',routes.every(path=>path==='/api/v1/me'||path==='/api/v1/knowledge/search'));
 await page.setViewportSize({width:1440,height:1000});await inspectSource();
 check('desktop preserves search on left with authorized source panel on right',await page.locator('.source-workspace-main').isVisible()&&await page.locator('.source-workspace-panel').isVisible());
 const expectedText='😀 Biaya perjalanan terbaru. Fictional travel currentword policy.';
 check('exact source chunk is highlighted with page/location metadata',await page.locator('mark').textContent()===expectedText&&await page.getByText('Posisi teks 0–'+[...expectedText].length,{exact:true}).count()===1);
 for(const width of [1440,768,390]){
  await page.setViewportSize({width,height:1000});await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  check('source has no horizontal overflow at '+width,await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  if(width<900)check('mobile source replaces main content at '+width,!await page.locator('.source-workspace-main').isVisible());
  await page.screenshot({path:out+'source-light-'+width+'.png'});
 }
 await page.getByRole('button',{name:'Kembali ke posisi sebelumnya',exact:true}).click();await page.locator('.source-workspace-main').waitFor({state:'visible'});
 await page.waitForFunction(()=>document.activeElement?.textContent==='Kebijakan perjalanan — teks sumber lengkap');check('mobile back restores result trigger after current identity check',await page.locator('mark').count()===0);
 await search('currentword','en');await page.getByRole('button',{name:'Kebijakan perjalanan — teks sumber lengkap',exact:true}).waitFor();check('English query searches the actual lexical API without translation',await page.locator('.source-results > li').count()===1);
 await search('oldword','en');await page.getByRole('heading',{name:'Tidak ada sumber yang cocok',exact:true}).waitFor();check('superseded text is excluded from current search',await page.locator('.source-results > li').count()===0);
 await page.goto(fixture.origin+'/sources/'+fixture.old);await page.getByText('Versi 1 · Versi lama — telah digantikan',{exact:true}).waitFor();check('exact superseded deep link remains visibly labelled',await page.locator('.source-text').textContent()==='😀 Biaya perjalanan lama. Fictional travel oldword policy.');
 await page.goto(fixture.origin+'/sources/'+fixture.draft);await page.getByRole('heading',{name:'Sumber tidak tersedia',exact:true}).waitFor();check('unpublished source deep link does not reveal text',await page.locator('.source-text').count()===0);
 await page.goto(fixture.origin+'/search');check('reload discards private search query',await page.getByRole('textbox',{name:'Kata pencarian',exact:true}).inputValue()==='');
 await control('checkpoint');await save('stage-b-browser-positive',true);
 stage='actual-revoke';await search();await inspectSource();await control('revoke');await page.getByRole('button',{name:'Periksa akses terbaru',exact:true}).click();await page.getByRole('heading',{name:'Sumber tidak tersedia',exact:true}).waitFor();
 check('actual ACL revoke clears source highlight and stale result excerpts',await page.locator('.source-text').count()===0&&await page.locator('.source-results > li').count()===0);
 await save('stage-c-revoke-browser',true);await control('grant');await page.getByRole('button',{name:'Kembali ke posisi sebelumnya',exact:true}).click();await search();await inspectSource();
 stage='actual-withdraw';await control('withdraw');await page.getByRole('button',{name:'Periksa akses terbaru',exact:true}).click();await page.getByRole('heading',{name:'Sumber tidak tersedia',exact:true}).waitFor();
 check('actual terminal withdrawal clears source and prior search excerpts',await page.locator('.source-text').count()===0&&await page.locator('.source-results > li').count()===0);await save('stage-c-withdraw-browser',true);
 stage='other-identities';for(const [name,token] of [['admin',fixture.admin],['foreign',fixture.foreign]]){
  await open(token,'/sources/'+fixture.old);await page.getByRole('heading',{name:'Sumber tidak tersedia',exact:true}).waitFor();check(name+' source attempt reveals no stale text',await page.locator('.source-text').count()===0);
 }
 stage='error-fixture';await open(fixture.employee);await page.route('**/api/v1/knowledge/search?*',async route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:{code:'unavailable',message:'controlled',retryable:true,request_id:'66666666-6666-4666-8666-666666666666'}})}));await search();await page.getByRole('heading',{name:'Data belum dapat dimuat',exact:true}).waitFor();check('controlled unavailable search stays distinct from authorized empty',await page.getByRole('heading',{name:'Tidak ada sumber yang cocok',exact:true}).count()===0);
 await page.setViewportSize({width:1440,height:1000});await page.getByRole('link',{name:'Tampilan',exact:true}).click();await page.getByLabel('Bahasa antarmuka',{exact:true}).selectOption('en');await page.getByLabel('Theme',{exact:true}).selectOption('dark');await page.getByRole('link',{name:'Back to workspace',exact:true}).click();await page.goto(fixture.origin+'/search');await page.getByRole('heading',{name:'Find sources',exact:true}).waitFor();await page.setViewportSize({width:390,height:1000});check('English dark search reflows on mobile',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:out+'search-dark-390.png'});
 await save('browser-proof',true);console.log(JSON.stringify({passed:true,checks:checks.length}));
}catch{await save('browser-partial',false);console.log(JSON.stringify({passed:false,stage,checks}));process.exitCode=1;}
finally{clearTimeout(watchdog);await context?.close();await browser?.close();}
