"""Opt-in development verification UI, without rendering identity or token values."""

from fastapi import APIRouter
from starlette.responses import HTMLResponse, Response

router=APIRouter()
HEADERS={"Cache-Control":"no-store","Referrer-Policy":"no-referrer",
    "Content-Security-Policy":"default-src 'none'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options":"nosniff"}
HTML='''<!doctype html><html lang="en"><meta charset="utf-8"><title>Application login check</title>
<h1>Application login check</h1><p>Development verification</p>
<a href="/api/v1/auth/login">Sign in</a><button id="check">Check session</button>
<button id="logout">Log out and verify</button><pre id="result"></pre>
<script src="/identity-check.js"></script></html>'''
SCRIPT='''let csrf;
const result=document.getElementById('result');
async function check(){
 const r=await fetch('/api/v1/me',{headers:{'X-EAI-CSRF-Bootstrap':'1'},cache:'no-store'});
 if(r.ok){const data=await r.json();csrf=data.csrf_token;
  result.textContent=JSON.stringify({authenticated:true,application_session_created:true,membership_count:data.memberships.length});}
 else{csrf=undefined;result.textContent=JSON.stringify({authenticated:false,status:r.status});}
}
document.getElementById('check').onclick=check;
document.getElementById('logout').onclick=async()=>{
 if(!csrf){await check();if(!csrf)return;}
 const r=await fetch('/api/v1/auth/logout',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:'{}',cache:'no-store'});
 if(r.status!==204){result.textContent=JSON.stringify({logged_out:false,status:r.status});return;}
 csrf=undefined;const after=await fetch('/api/v1/me',{headers:{'X-EAI-CSRF-Bootstrap':'1'},cache:'no-store'});
 result.textContent=JSON.stringify({logged_out:after.status===401,session_rejected:after.status===401});
};check();'''


@router.get("/",include_in_schema=False)
async def home():return HTMLResponse(HTML,headers=HEADERS)


@router.get("/identity-check.js",include_in_schema=False)
async def script():return Response(SCRIPT,media_type="application/javascript",headers=HEADERS)
