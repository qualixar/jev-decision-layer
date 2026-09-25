/** Offline existing-tab contract tests. No browser is launched and no provider is called. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,chmod,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {availableActions,originOf,parseState,createSession,run,ipc,loadConfig} from '../plugins/qualixar-jev-decision-layer/skills/jev-browser-choice/bridge.mjs';
const snapshot=(label='Next',origin='https://example.org',extra='')=>`Browser tab: Demo URL: "${origin}/page".\n1 link ${label}\n2 text field Query\n${extra}`;
function tab(states=[snapshot()]) {
 let n=0;const calls=[];
 return {calls,async getAXState(){return states[Math.min(n++,states.length-1)];},async click(i){calls.push(['click',i]);},async pressKey(k){calls.push(['press',k]);},async reload(){calls.push(['reload']);}};
}
const config=(extra={})=>({goal:'Read the next result page',allowedOrigins:['https://example.org'],maxSteps:3,maxMs:1000,waitMs:1,decide:async()=>({choice:'a0',confidence:.99,receipt_id:'a'.repeat(64)}),...extra});
test('private bridge config loads from an explicit config root',async t=>{
 const root=await mkdtemp(join(tmpdir(),'jev-browser-config-'));
 t.after(()=>rm(root,{recursive:true,force:true}));
 const folder=join(root,'qualixar-jev-decision-layer');await mkdir(folder,{mode:0o700});
 const file=join(folder,'auto-bridge.json');
 const record={socketPath:'/tmp/synthetic.sock',allowedOrigins:['https://example.org'],maxSteps:3};
 await writeFile(file,JSON.stringify({workspaces:{[resolve('/synthetic-workspace')]:record}}),{mode:0o600});
 assert.deepEqual(await loadConfig('/synthetic-workspace',root),record);
 await chmod(file,0o644);
 await assert.rejects(()=>loadConfig('/synthetic-workspace',root),/UNSAFE_BRIDGE_CONFIGURATION/);
});
test('parses observed controls',()=>{assert.equal(parseState(snapshot()).length,2);});
test('origin includes only scheme and authority',()=>assert.equal(originOf(snapshot()),'https://example.org'));
test('unknown origin is rejected',()=>assert.throws(()=>originOf('not a state')));
test('discovers observed navigation, not a text field',()=>{const a=availableActions(snapshot());assert.equal(a.length,1);assert.equal(a[0].index,1);});
test('ambiguous duplicate names not clicked',()=>assert.equal(availableActions(snapshot('Next',undefined,'3 button Next')).length,0));
test('consequential controls are not discovered',()=>assert.equal(availableActions(snapshot('Pay now')).length,0));
test('explicit checkout and account controls are never executable',()=>{
 for(const label of ['Continue to checkout','Place order','Proceed','Open account settings','View checkout','Add to cart']) {
  assert.equal(availableActions(snapshot(label),[{op:'click',name:label}],false).length,0,label);
 }
});
test('explicit text typing not supported',()=>assert.throws(()=>availableActions(snapshot(),[{op:'type',text:'anything'}])));
test('explicit safe navigation controls map to observed indexes',()=>assert.equal(availableActions(snapshot('View reports'),[{op:'click',name:'View reports'}],false)[0].index,1));
test('unobserved explicit control omitted',()=>assert.equal(availableActions(snapshot(),[{op:'click',name:'Absent'}],false).length,0));
test('press Enter is not an implicit submit',()=>assert.equal(availableActions(snapshot(),[{op:'press',key:'Enter'}],false).length,0));
test('duplicate explicit/discovered actions coalesce',()=>assert.equal(availableActions(snapshot(),[{op:'click',name:'Next'}]).length,1));
test('uses only existing tab and returns compact output',async()=>{
 const t=tab();const result=await run(t,config());assert.equal(t.calls.length,1);assert.equal(result.reason,'no_progress');assert.equal(result.native_browser_reused,true);assert.ok(!('state' in result));assert.ok(!('history' in result));
});
test('DONE is not claimed independent acceptance',async()=>{const result=await run(tab(),config({decide:async()=>({choice:'DONE',confidence:.99})}));assert.equal(result.status,'needs_verification');assert.equal(result.requiresCodexVerification,true);});
test('model handoff leaves task to Codex',async()=>{const t=tab();assert.equal((await run(t,config({decide:async()=>({choice:'HANDOFF',confidence:.99})}))).reason,'model_handoff');assert.equal(t.calls.length,0);});
test('stale state cannot use old control index',async()=>{const t=tab([snapshot(),snapshot('Previous')]);const result=await run(t,config({maxSteps:1}));assert.equal(t.calls.length,0);assert.equal(result.reason,'step_limit');});
test('origin change blocks action',async()=>{const t=tab([snapshot(),snapshot('Next','https://other.example')]);const result=await run(t,config());assert.equal(result.reason,'origin_changed');assert.equal(t.calls.length,0);});
test('uncertain decision does not act',async()=>{const t=tab();const result=await run(t,config({decide:async()=>({choice:'a0',confidence:.2})}));assert.equal(result.reason,'low_confidence');assert.equal(t.calls.length,0);});
test('invalid candidate never executed',async()=>{const t=tab();const result=await run(t,config({decide:async()=>({choice:'a99',confidence:.99})}));assert.equal(result.reason,'invalid_decision');assert.equal(t.calls.length,0);});
test('nonfinite confidence rejected',async()=>assert.equal((await run(tab(),config({decide:async()=>({choice:'a0',confidence:NaN})}))).reason,'invalid_decision'));
test('provider error hands off, not endless retry',async()=>{let calls=0;const result=await run(tab(),config({decide:async()=>{calls++;throw Error('failure');}}));assert.equal(result.reason,'decision_unavailable');assert.equal(calls,1);});
test('WAIT bounded at three observations',async()=>{const t=tab();const result=await run(t,config({decide:async()=>({choice:'WAIT',confidence:.99}),maxSteps:10}));assert.equal(result.reason,'loading_timeout');assert.equal(t.calls.length,0);});
test('step budget is enforced',async()=>{const t=tab([snapshot(),snapshot(),snapshot('Previous'),snapshot('Previous'),snapshot('Next')]);assert.equal((await run(t,config({maxSteps:1}))).reason,'step_limit');});
test('zero or excessive step limits rejected',async()=>{for(const maxSteps of [0,31])await assert.rejects(()=>run(tab(),config({maxSteps})));});
test('run overrides cannot expand enrolled browser authority',async()=>{
 const s=createSession(tab(),config({maxSteps:2}));
 await assert.rejects(()=>s.run({maxSteps:3}),/BROWSER_STEP_BUDGET/);
 await assert.rejects(()=>s.run({allowedOrigins:['https://other.example']}),/BROWSER_AUTHORITY_OVERRIDE/);
 await assert.rejects(()=>s.run({controls:[{op:'click',name:'Next'}]}),/BROWSER_AUTHORITY_OVERRIDE/);
});
test('invalid confidence configuration rejected',async()=>{await assert.rejects(()=>run(tab(),config({minConfidence:NaN})));});
test('action failure returns control',async()=>{const t=tab();t.click=async()=>{throw Error('stale');};assert.equal((await run(t,config())).reason,'action_or_origin_error');});
test('nothing observed does not invent a selector',async()=>{const t=tab([snapshot('Unrelated')]);assert.equal((await run(t,config())).reason,'no_observed_candidate');assert.equal(t.calls.length,0);});
test('session retains inspectable local history only on demand',async()=>{const s=createSession(tab(),config());await s.run();assert.ok(s.inspect().history.length>=1);assert.ok(!('state' in s.inspect()));});
