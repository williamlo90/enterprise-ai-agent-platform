import {writeFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {chromium} from '../../frontend/node_modules/playwright-core/index.mjs';
let input='';for await (const chunk of process.stdin) input+=chunk;
const fixture=JSON.parse(input);input='';
const out=fileURLToPath(new URL('../../local/fe-org-01/',import.meta.url));
const checks=[];let browser,context,page,stage='launch';
function check(name,passed){checks.push({name,passed:!!passed});if(!passed)throw new Error('controlled_check_failed');}
const watchdog=setTimeout(()=>{void browser?.close();},100000);
async function open(token,path){
  await context?.close();context=await browser.newContext({ignoreHTTPSErrors:true,serviceWorkers:'block'});
  await context.route('**/*',route=>new URL(route.request().url()).origin===fixture.origin?route.continue():route.abort());
  await context.addCookies([{name:'__Host-eai-session',value:token,url:fixture.origin,httpOnly:true,secure:true,sameSite:'Lax'}]);
  page=await context.newPage();page.setDefaultTimeout(9000);await page.goto(fixture.origin+path);
}
try{
  browser=await chromium.launch({channel:'msedge',headless:true});
  stage='directory';await open(fixture.admin,'/organization');await page.getByRole('heading',{name:'Organisasi',exact:true}).waitFor();
  await page.locator('.workspace-list').waitFor();check('admin discovers registered members through actual restricted API',await page.getByRole('link',{name:'Anton Wijaya',exact:true}).count()===1);
  await page.getByRole('textbox',{name:'Nama atau identitas anggota',exact:true}).fill('Anton*');await page.getByRole('button',{name:'Terapkan filter',exact:true}).click();await page.getByRole('heading',{name:'Tidak ada anggota yang cocok',exact:true}).waitFor();check('star remains literal through the browser transport and restricted server search',await page.locator('.workspace-list > li').count()===0);
  await page.getByRole('textbox',{name:'Nama atau identitas anggota',exact:true}).fill('Anton');await page.getByRole('button',{name:'Terapkan filter',exact:true}).click();
  await page.waitForFunction(()=>document.querySelectorAll('.workspace-list > li').length===1);check('literal directory filtering uses scoped server results',await page.locator('.workspace-list > li').count()===1);
  await page.getByRole('link',{name:'Anton Wijaya',exact:true}).click();await page.getByRole('textbox',{name:'Departemen',exact:true}).waitFor();
  check('cleared local name does not copy the canonical account fallback',await page.getByRole('textbox',{name:'Nama dalam perusahaan',exact:true}).inputValue()==='');
  stage='edit';await page.getByRole('textbox',{name:'Departemen',exact:true}).fill('Finance');await page.getByRole('textbox',{name:'Jabatan',exact:true}).fill('Manager');await page.getByRole('textbox',{name:'Grup',exact:true}).fill('Finance\nTravel');
  await page.getByRole('button',{name:'Simpan atribut',exact:true}).click();await page.getByRole('heading',{name:'Atribut berhasil disimpan',exact:true}).waitFor();
  await page.getByRole('textbox',{name:'Departemen',exact:true}).waitFor();await page.reload();await page.getByRole('textbox',{name:'Departemen',exact:true}).waitFor();
  check('saved organization attributes persist across a real browser reload',await page.getByRole('textbox',{name:'Departemen',exact:true}).inputValue()==='Finance'&&await page.getByRole('textbox',{name:'Jabatan',exact:true}).inputValue()==='Manager');
  for(const width of [1440,768,390]){
    await page.setViewportSize({width,height:1000});await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    check('organization form has no horizontal overflow at '+width,await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await page.screenshot({path:out+'organization-light-'+width+'.png',fullPage:true});
  }
  stage='validation';await page.getByRole('textbox',{name:'Nama dalam perusahaan',exact:true}).fill('x'.repeat(201));await page.getByRole('button',{name:'Simpan atribut',exact:true}).click();
  const summary=page.getByRole('alert').filter({has:page.getByRole('heading',{name:'Periksa isian berikut',exact:true})});await summary.waitFor();
  check('invalid form focuses an announced linked error summary',await summary.evaluate(el=>el===document.activeElement)&&await summary.getByRole('link').count()>0);
  await page.getByRole('textbox',{name:'Nama dalam perusahaan',exact:true}).fill('Anton perusahaan');await page.getByRole('button',{name:'Muat ulang data',exact:true}).click();
  await page.getByRole('dialog',{name:'Buang perubahan formulir?',exact:true}).waitFor();await page.getByRole('button',{name:'Tetap mengedit',exact:true}).click();
  check('cancel reload retains unsaved form values',await page.getByRole('textbox',{name:'Nama dalam perusahaan',exact:true}).inputValue()==='Anton perusahaan');
  await page.getByRole('button',{name:'Muat ulang data',exact:true}).click();await page.getByRole('button',{name:'Buang dan muat ulang',exact:true}).click();await page.getByRole('textbox',{name:'Nama dalam perusahaan',exact:true}).waitFor();
  stage='unknown';let writes=0;
  await page.route('**/api/v1/organization/members/'+fixture.membership,async route=>{if(route.request().method()==='PATCH'){writes++;await route.fetch();await route.abort('failed');}else await route.continue();});
  await page.getByRole('textbox',{name:'Departemen',exact:true}).fill('Legal');await page.getByRole('button',{name:'Simpan atribut',exact:true}).click();await page.getByRole('heading',{name:'Hasil penyimpanan belum diketahui',exact:true}).waitFor();
  check('lost committed reply locks form without automatic write retry',writes===1&&await page.getByRole('textbox',{name:'Departemen',exact:true}).isDisabled());
  await page.unroute('**/api/v1/organization/members/'+fixture.membership);await page.getByRole('button',{name:'Periksa hasil penyimpanan',exact:true}).click();await page.getByRole('heading',{name:'Atribut berhasil disimpan',exact:true}).waitFor();await page.getByRole('textbox',{name:'Departemen',exact:true}).waitFor();
  check('explicit original-key reconciliation reloads current server state',await page.getByRole('textbox',{name:'Departemen',exact:true}).inputValue()==='Legal');
  stage='dark';await page.setViewportSize({width:1440,height:1000});await page.getByRole('link',{name:'Tampilan',exact:true}).click();await page.getByLabel('Bahasa antarmuka',{exact:true}).selectOption('en');await page.getByLabel('Theme',{exact:true}).selectOption('dark');await page.getByRole('link',{name:'Back to workspace',exact:true}).click();await page.getByRole('heading',{name:'Organization',exact:true}).waitFor();await page.getByRole('link',{name:'Anton Wijaya',exact:true}).click();await page.getByRole('textbox',{name:'Department',exact:true}).waitFor();
  for(const width of [1440,390]){await page.setViewportSize({width,height:1000});check('English dark form reflows at '+width,await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:out+'organization-dark-'+width+'.png',fullPage:true});}
  check('only enum UI preferences persist in the browser',await page.evaluate(()=>Object.keys(localStorage).every(key=>['eai.ui.locale','eai.ui.theme'].includes(key))&&sessionStorage.length===0));
  stage='deny';await open(fixture.employee,'/organization/members/'+fixture.membership);await page.getByRole('heading',{name:'Halaman tidak tersedia untuk akses Anda',exact:true}).waitFor();check('employee cannot open organization form',await page.getByRole('textbox').count()===0);
  const proof={passed:true,checks,browser_version:browser.version(),provider_login:'controlled_fixture_session_not_tested',physical_device:'not_tested'};await writeFile(out+'browser-proof.json',JSON.stringify(proof,null,2));console.log(JSON.stringify({passed:true,checks:checks.length}));
}catch{console.log(JSON.stringify({passed:false,stage,checks}));process.exitCode=1;}
finally{clearTimeout(watchdog);await context?.close();await browser?.close();}
