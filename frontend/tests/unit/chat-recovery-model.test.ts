import {expect,it} from 'vitest';
import {diagnosticsDecoder,recoverDecoder} from '../../src/features/chat/diagnostics-api';
import {writeKeyField,describeWrite} from '../../src/api/write-policy';
import {rid,diagnostic} from './chat-recovery-fixtures';
it('recovery has an internal intent but no wire key',()=>{expect(writeKeyField('POST','/api/v1/runs/'+rid+'/recover')).toBeNull();expect(describeWrite({method:'POST',path:'/api/v1/runs/'+rid+'/recover',body:{expected_version:3}}).status).toBe(200);});
it('projects diagnostics metadata without leaking arbitrary provider payloads',()=>{const row=diagnosticsDecoder(rid)({...diagnostic,prompt:'private',receipt:'secret',usage:{...diagnostic.usage,provider_payload:'secret'}});expect(JSON.stringify(row)).not.toContain('private');expect(JSON.stringify(row)).not.toContain('secret');expect(row.deadline).toBeNull();expect(row.usage.unknown_attempts).toBe(1);});
it('rejects malformed diagnostics identity, events, usage and budgets',()=>{for(const value of [{...diagnostic,id:'bad'},{...diagnostic,events:[...diagnostic.events,...diagnostic.events]},{...diagnostic,steps:9},{...diagnostic,usage:{...diagnostic.usage,known_input_tokens:-1}},{...diagnostic,usage:{...diagnostic.usage,unknown_attempts:3}}])expect(()=>diagnosticsDecoder(rid)(value)).toThrow();});
it('binds recover receipt to exact version transition and durable closure marker',()=>{const closed={...diagnostic,state:'failed',version:4,recovery_available:false,recovery:'closed_without_replay',recovered_at:'2026-11-06T00:00:00Z'};expect(recoverDecoder(rid,3)(closed).version).toBe(4);for(const row of [{...closed,version:5},{...closed,recovery:null},{...closed,recovered_at:null},{...closed,state:'completed'}])expect(()=>recoverDecoder(rid,3)(row)).toThrow();});

it('bounds latest-page discovery and never treats an incomplete scan as the latest result',async()=>{
 const {lastHistoryPage}=await import('../../src/features/chat/turn-api');
 const first={conversation:{} as never,messages:[],next_after:20};let calls=0;
 const found=await lastHistoryPage(first,async after=>{calls++;return {...first,next_after:after===40?null:40};});expect(found.after).toBe(40);expect(calls).toBe(2);
 calls=0;await expect(lastHistoryPage(first,async after=>{calls++;return {...first,next_after:after+20};})).rejects.toThrow();expect(calls).toBe(9);
});
