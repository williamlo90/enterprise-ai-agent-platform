import { writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { chromium } from '../../frontend/node_modules/playwright-core/index.mjs';
let input=''; for await (const chunk of process.stdin) input+=chunk;
const fixture=JSON.parse(input); input='';
const out=fileURLToPath(new URL('../../local/fe-auth-01/',import.meta.url));
const checks=[];
const proof={checks,mode:'Actual built React UI + real HTTPS identity API/restricted disposable PostgreSQL; controlled session cookies',provider_login:'not_tested',physical_device:'not_tested'};
let browser,context,page,stage='launch';
function check(name,value){checks.push({name,passed:!!value});if(!value)throw new Error('controlled_check_failed');}
const watchdog=setTimeout(()=>{void browser?.close();},100000);
async function open(token,path='/'){
  await context?.close();
  context=await browser.newContext({ignoreHTTPSErrors:true,serviceWorkers:'block'});
  await context.route('**/*',r=>new URL(r.request().url()).origin===fixture.origin?r.continue():r.abort());
  if(token)await context.addCookies([{name:'__Host-eai-session',value:token,url:fixture.origin,httpOnly:true,secure:true,sameSite:'Lax'}]);
  page=await context.newPage();page.setDefaultTimeout(9000);
  await page.goto(fixture.origin+path);
}
try{
  browser=await chromium.launch({channel:'msedge',headless:true});proof.browser_version=browser.version();
  stage='ready';await open(fixture.a,'/chat');
  await page.getByRole('heading',{name:'Fitur ini sedang disiapkan',exact:true}).waitFor();
  check('built auth entry boots through actual /me under HTTPS and native Web Locks',await page.evaluate(()=>isSecureContext&&!!navigator.locks));
  check('current employee and KM see both routes without fabricated business data',await page.getByRole('link',{name:'Percakapan',exact:true}).isVisible()&&await page.getByRole('link',{name:'Dokumen',exact:true}).isVisible()&&!await page.getByText('Kebijakan Cuti 2026.pdf').count());
  const cookies=await context.cookies();check('session remains Secure HttpOnly Lax',cookies.some(c=>c.name==='__Host-eai-session'&&c.secure&&c.httpOnly&&c.sameSite==='Lax'));
  for(const width of [1440,768,390,320]){
    await page.setViewportSize({width,height:900});
    check('responsive workspace has no horizontal overflow at '+width,await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    if(width===1440||width===390)await page.screenshot({path:out+'light-'+width+'.png',fullPage:true});
  }
  check('HP reading layout stacks sources beneath content',await page.locator('.reading-layout').evaluate(el=>getComputedStyle(el).gridTemplateColumns.split(' ').length===1));
  check('active company remains visible on HP',await page.locator('.public-scope').isVisible()&&await page.locator('.public-scope').textContent()==='Fiktif Alpha');
  await page.evaluate(()=>document.documentElement.style.fontSize='200%');
  check('large text reflows without horizontal overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  await page.evaluate(()=>document.documentElement.style.fontSize='');
  stage='modal';await page.getByRole('button',{name:'Buka navigasi'}).click();
  const menu=page.getByRole('dialog',{name:'Navigasi utama',exact:true});await menu.waitFor();
  await page.keyboard.press('Tab');check('shadcn dialog traps keyboard focus',await menu.evaluate(el=>el.contains(document.activeElement)));
  await page.mouse.click(3,3);check('outside click does not dismiss the controlled dialog',await menu.isVisible());
  await page.keyboard.press('Escape');await menu.waitFor({state:'hidden'});
  await page.waitForFunction(()=>document.activeElement?.textContent==='Buka navigasi');
  check('Escape returns focus to mobile navigation trigger',await page.getByRole('button',{name:'Buka navigasi'}).evaluate(el=>el===document.activeElement));
  await page.setViewportSize({width:1440,height:900});
  check('desktop reading layout keeps sources to the right',await page.locator('.reading-layout').evaluate(el=>{const [a,b]=el.children;return b.getBoundingClientRect().left>=a.getBoundingClientRect().right;}));
  stage='preferences';await page.getByRole('link',{name:'Tampilan',exact:true}).click();
  await page.getByLabel('Bahasa antarmuka',{exact:true}).selectOption('en');await page.getByLabel('Theme',{exact:true}).selectOption('dark');
  await page.getByRole('button',{name:'About these settings',exact:true}).click();
  const modal=page.getByRole('dialog',{name:'Settings in this browser',exact:true});await modal.waitFor();
  check('safe initial modal focus and description are linked',await modal.getByRole('button',{name:'Close',exact:true}).evaluate(el=>el===document.activeElement)&&!!await modal.getAttribute('aria-describedby'));
  await page.keyboard.press('Escape');await page.getByRole('link',{name:'Back to workspace',exact:true}).click();
  await page.getByRole('heading',{name:'This feature is being prepared',exact:true}).waitFor();
  for(const width of [1440,390]){
    await page.setViewportSize({width,height:900});
    await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    if(width===390){
      proof.mobile_brand=await page.locator('.brand').evaluate(el=>({text:el.textContent,display:getComputedStyle(el).display,box:{x:el.getBoundingClientRect().x,y:el.getBoundingClientRect().y,width:el.getBoundingClientRect().width,height:el.getBoundingClientRect().height}}));
      check('product identity remains visible on English dark HP',await page.getByRole('link',{name:'Enterprise AI Agent Platform',exact:true}).isVisible());
    }
    await page.screenshot({path:out+'dark-'+width+'.png',fullPage:true});
  }
  await page.reload();await page.getByRole('heading',{name:'This feature is being prepared',exact:true}).waitFor();
  check('reload restores only locale/theme, with no identity or private persistence',await page.evaluate(()=>document.documentElement.lang==='en'&&document.documentElement.dataset.theme==='dark'&&JSON.stringify(Object.keys(localStorage).sort())===JSON.stringify(['eai.ui.locale','eai.ui.theme'])&&sessionStorage.length===0));
  await page.setViewportSize({width:1440,height:900});
  stage='two_tabs';const second=await context.newPage();second.setDefaultTimeout(9000);await second.goto(fixture.origin+'/chat');await second.getByRole('button',{name:'Sign out',exact:true}).waitFor();
  await page.getByRole('button',{name:'Change company',exact:true}).click();await page.getByLabel('Company',{exact:true}).selectOption(fixture.memberB);
  await page.getByRole('button',{name:'Use this company',exact:true}).click();
  await page.waitForURL(fixture.origin+'/knowledge');await page.getByRole('heading',{name:'Documents',exact:true}).waitFor();
  check('actual membership switch rotates session and lands KM-only on documents',await page.getByText('Fiktif Beta',{exact:true}).count()>0&&!await page.getByRole('link',{name:'Conversations',exact:true}).count());
  await second.evaluate(()=>window.dispatchEvent(new Event('focus')));
  await second.getByText('Fiktif Beta',{exact:true}).first().waitFor();
  check('other tab revalidates and clears employee navigation after context signal/resume',!await second.getByRole('link',{name:'Conversations',exact:true}).count());await second.close();
  stage='unknown_switch';let posts=0;
  await page.route('**/api/v1/session/tenant',async route=>{posts++;await route.fetch();await route.abort('failed');});
  await page.getByRole('button',{name:'Change company',exact:true}).click();
  // Select the other membership without putting any fixture credential in the page.
  const values=await page.getByLabel('Company',{exact:true}).locator('option').evaluateAll(options=>options.map(o=>o.value));
  await page.getByLabel('Company',{exact:true}).selectOption(values.find(v=>v!==fixture.memberB));
  await page.getByRole('button',{name:'Use this company',exact:true}).click();
  await page.getByText(/The result of the request is not yet known/).waitFor();
  check('lost response to an actual session write masks workspace and reports unknown',!await page.getByRole('link',{name:'Documents',exact:true}).count()&&posts===1);
  await page.unroute('**/api/v1/session/tenant');await page.getByRole('button',{name:'Check session again',exact:true}).click();
  await page.waitForFunction(()=>!document.body.textContent.includes('Checking your session'));
  check('explicit reconciliation does not replay the session POST',posts===1);
  stage='no_membership';await open(fixture.noMembership);await page.getByRole('heading',{name:'Belum ada akses perusahaan',exact:true}).waitFor();
  check('actual no-membership session has no company selection or private menu',!await page.getByLabel('Perusahaan',{exact:true}).count()&&!await page.getByRole('link',{name:'Percakapan',exact:true}).count());
  await page.getByRole('button',{name:'Keluar',exact:true}).click();await page.getByRole('link',{name:'Masuk melalui akun perusahaan',exact:true}).waitFor();
  check('actual logout 204 ends local app session without promising SSO logout',await page.getByRole('link',{name:'Masuk melalui akun perusahaan',exact:true}).getAttribute('href')==='/api/v1/auth/login?return_to=home');
  await context.addCookies([{name:'__Host-eai-session',value:fixture.noMembership,url:fixture.origin,httpOnly:true,secure:true,sameSite:'Lax'}]);await page.reload();await page.getByRole('link',{name:'Masuk melalui akun perusahaan',exact:true}).waitFor();
  check('old session credential is rejected after real logout',!await page.getByRole('heading',{name:'Belum ada akses perusahaan',exact:true}).count());
  stage='no_tenant';await open(fixture.noTenant,'/tenants');await page.getByLabel('Perusahaan',{exact:true}).waitFor();
  check('actual identity without active tenant presents server memberships',await page.getByLabel('Perusahaan',{exact:true}).locator('option').count()===2);
  await page.getByLabel('Perusahaan',{exact:true}).selectOption(fixture.memberB);await page.getByRole('button',{name:'Gunakan perusahaan',exact:true}).click();await page.waitForURL(fixture.origin+'/knowledge');
  check('tenant picker uses actual backend membership selection and role landing',await page.getByRole('heading',{name:'Dokumen',exact:true}).isVisible());
  stage='direct_guard';await open(fixture.admin,'/chat');await page.getByRole('heading',{name:'Halaman tidak tersedia untuk akses Anda',exact:true}).waitFor();
  check('actual administrator identity cannot open employee chat by URL',!await page.getByRole('link',{name:'Percakapan',exact:true}).count());
  stage='unsupported';await context.close();context=await browser.newContext({ignoreHTTPSErrors:true,serviceWorkers:'block'});await context.addInitScript(()=>Object.defineProperty(navigator,'locks',{value:undefined,configurable:true}));page=await context.newPage();page.setDefaultTimeout(9000);
  await page.goto(fixture.origin+'/');await page.getByRole('heading',{name:'Browser belum mendukung akses aman',exact:true}).waitFor();check('missing native Web Locks fails closed before access',!await page.getByRole('link',{name:'Percakapan',exact:true}).count());
  proof.passed=true;
}catch{proof.passed=false;proof.failed_stage=stage;try{await page?.screenshot({path:out+'failure.png',fullPage:true});}catch{}}
finally{clearTimeout(watchdog);await browser?.close();proof.owned_browser_closed=true;await writeFile(out+'browser-proof.json',JSON.stringify(proof,null,2));console.log(JSON.stringify({passed:proof.passed,checks:checks.length,failed_stage:proof.failed_stage}));}
if(!proof.passed)process.exitCode=1;
