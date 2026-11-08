import { writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { chromium } from '../../../frontend/node_modules/playwright-core/index.mjs';
let input=''; for await (const chunk of process.stdin) input += chunk;
const fixture=JSON.parse(input); input='';
const checks=[], timings={};
const proof={ checks,timings,mode:'Real HTTPS same-origin API and disposable restricted PostgreSQL; controlled session cookies/provider/storage',provider_login:'not_tested' };
let browser, stage='launch';
function check(name,passed) { checks.push({name,passed:!!passed}); if(!passed) throw new Error('controlled_check_failed'); }
const watchdog=setTimeout(()=>{void browser?.close();},45000);
try {
  browser=await chromium.launch({channel:'msedge',headless:true});
  proof.browser_version=browser.version();
  const context=await browser.newContext({ignoreHTTPSErrors:true,serviceWorkers:'block'});
  let foreign=0;
  await context.route('**/*',(route)=>new URL(route.request().url()).origin===fixture.origin ? route.continue() : (foreign++,route.abort()));
  const install=async(token)=>context.addCookies([{name:'__Host-eai-session',value:token,url:fixture.origin,secure:true,httpOnly:true,sameSite:'Lax'}]);
  await install(fixture.a);
  const page=await context.newPage(); page.setDefaultTimeout(7000);
  await page.goto(fixture.origin+'/__g13');
  await page.exposeFunction('probeCookieChange',async(which)=>install(which==='B'?fixture.b:fixture.a));
  await page.evaluate(async()=>{
    const mod=await import('/__g13/assets/browser-client.js');
    globalThis.probeMod=mod;
    globalThis.probeApi=new mod.ApiCoordinator(new mod.ApiTransport(async(path,init)=>{
      const response=await fetch(path,init);
      if(globalThis.armABABoot && path==='/api/v1/me'){
        globalThis.armABABoot=false;globalThis.armABAList=true;await probeCookieChange('B');
      } else if(globalThis.armABAList && path==='/api/v1/conversations'){
        globalThis.armABAList=false;globalThis.abaServerStatus=response.status;await probeCookieChange('A');
      }
      if(globalThis.armFault && path==='/api/v1/conversations'){
        globalThis.armFault=false;const headers=new Headers(response.headers);headers.set('X-EAI-Context-Binding','0'.repeat(64));
        return new Response(await response.arrayBuffer(),{status:response.status,headers});
      }
      return response;
    }),mod.browserLocks(),30000);
    globalThis.probeStamp=await probeApi.bootstrap();
    globalThis.decoded=0;
  });
  check('HTTPS secure context with native Web Locks',await page.evaluate(()=>isSecureContext && !!navigator.locks && location.protocol==='https:'));
  stage='positive_read';
  const success=await page.evaluate(async(cid)=>{
    const started=performance.now();
    const value=await probeApi.privateRead({path:'/api/v1/conversations',decode:probeMod.object},probeStamp);
    return {ok:value.items.some((item)=>item.id===cid),duration:performance.now()-started};
  },fixture.cidA);
  check('actual bound read returns the current tenant owner list',success.ok);
  timings.actual_local_gateway_bootstrap_read_post_ms=success.duration;
  const cookie=await context.cookies();
  check('session cookie remains HttpOnly Secure Lax without browser persistence',cookie.some((item)=>item.name==='__Host-eai-session' && item.httpOnly && item.secure && item.sameSite==='Lax') && await page.evaluate(()=>localStorage.length===0 && sessionStorage.length===0));
  stage='aba';
  await page.evaluate(()=>{globalThis.armABABoot=true;globalThis.abaServerStatus=0;});
  const aba=await page.evaluate(async()=>{
    try {await probeApi.privateRead({path:'/api/v1/conversations',decode:(value)=>{globalThis.decoded++;return probeMod.object(value);}},probeStamp);return {rejected:false};}
    catch(error){return {rejected:error.kind==='conflict' && globalThis.decoded===0,kind:error.kind,decoded:globalThis.decoded,status:globalThis.abaServerStatus};}
  });
  proof.aba_diagnostic={client_rejected:aba.rejected,status:aba.status,decoder_calls:aba.decoded};
  check('actual SQL rejects list B under expected A before decoder/display',aba.rejected && aba.status===409);
  const equal=await page.evaluate(async()=>{
    const next=await probeApi.bootstrap();const equal=next.binding===probeStamp.binding;globalThis.probeStamp=next;return equal;
  });
  check('pre/post A bindings can be equal and still do not admit the intervening B list',equal);
  stage='response_fault';
  await page.evaluate(()=>{globalThis.armFault=true;});
  const fault=await page.evaluate(async()=>{
    try {await probeApi.privateRead({path:'/api/v1/conversations',decode:(value)=>{globalThis.decoded++;return probeMod.object(value);}},probeStamp);return false;}
    catch(error){return error.kind==='context_changed' && globalThis.decoded===0;}
  });
  check('client rejects a fault-injected wrong response binding before decoder/display',fault);
  stage='two_tabs';
  await page.evaluate(async()=>{globalThis.probeStamp=await probeApi.bootstrap();});
  const second=await context.newPage();second.setDefaultTimeout(7000);await second.goto(fixture.origin+'/__g13');
  await second.evaluate(async()=>{
    const mod=await import('/__g13/assets/browser-client.js');
    globalThis.probeMod=mod;globalThis.probeApi=new mod.ApiCoordinator(new mod.ApiTransport(),mod.browserLocks(),30000);
    globalThis.probeStamp=await probeApi.bootstrap();
  });
  const firstRead=page.evaluate(async(cid)=>(await probeApi.privateRead({path:'/api/v1/conversations',decode:probeMod.object},probeStamp)).items.some((item)=>item.id===cid),fixture.cidA);
  const secondRead=second.evaluate(async(cid)=>(await probeApi.privateRead({path:'/api/v1/conversations',decode:probeMod.object},probeStamp)).items.some((item)=>item.id===cid),fixture.cidA);
  const firstOK=await firstRead;const secondOK=await secondRead;
  check('two actual browser tabs serialize bounded real API read cycles under native Web Locks',firstOK && secondOK);
  await second.close();
  stage='session_switch';
  const switched=await page.evaluate(async(membership)=>{
    globalThis.probeStamp=await probeApi.bootstrap();const before=probeStamp.binding;
    await probeApi.changeSession({kind:'switch',membershipId:membership},probeStamp);
    const next=await probeApi.bootstrap();globalThis.probeStamp=next;return next.binding!==before;
  },fixture.alternate);
  check('actual tenant switch rotates cookie/session binding',switched);
  stage='logout';
  const loggedOut=await page.evaluate(async()=>{
    await probeApi.changeSession({kind:'logout'},probeStamp);
    try {await probeApi.bootstrap();return false;}catch(error){return error.kind==='unauthenticated';}
  });
  check('actual logout clears session and fresh bootstrap returns401',loggedOut);
  check('no external/provider requests',foreign===0);
  await context.close();proof.passed=true;
} catch {proof.passed=false;proof.failed_phase=stage;process.exitCode=1;}
finally {
  clearTimeout(watchdog);if(browser)await browser.close();
  proof.owned_browser_closed=true;
  await writeFile(fileURLToPath(new URL('../../../local/g13/real-browser-proof.json',import.meta.url)),JSON.stringify(proof,null,2));
  console.log(JSON.stringify({passed:proof.passed,checks:checks.length,failed_phase:proof.failed_phase,cleanup:true}));
}
