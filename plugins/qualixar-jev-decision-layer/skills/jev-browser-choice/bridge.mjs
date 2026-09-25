/**
 * Adapted from wy-coliney/jev-browser-use @ f14b60e (MIT).
 * Copyright (c) 2026 Jev Browser Use contributors. Qualixar adaptations:
 * private IPC, no browser credentials, compact returns, deadlines, recoverable handoffs.
 * Uses only an existing authorized Computer Use tab. Never launches a browser.
 */
import {readFile, lstat} from 'node:fs/promises';
import {homedir, userInfo} from 'node:os';
import {join, resolve} from 'node:path';
import net from 'node:net';

// This compatibility bridge has no native per-click confirmation channel.
// Only observed navigation links may be clicked automatically; cart, form,
// account and payment controls stay with Codex's native browser interface.
const clickRoles=new Set(['link']);
const keys=new Set(['Escape','Tab','Shift+Tab','PageUp','PageDown','Home','End']);
const dangerous=/\b(delete|remove|purchase|pay|payment|checkout|order|account|settings|cart|send|publish|upload|transfer|subscribe|submit|confirm|place|proceed|log out|logout)\b/i;
const navigation=/^(next|previous|back|forward|show more|show less|expand|collapse|open|view|details|results|search|page \d+|load more)(\b|$)/i;
export function parseState(state) {
  return state.split('\n').map(x=>x.trim()).map(x=>x.match(/^(\d+) (text field|text area|combo box|radio button|menu item|[\w]+)(?: \([^)]*\))? (?:Description: )?(.*)$/)).filter(Boolean).map(m=>({index:Number(m[1]),role:m[2],name:m[3]}));
}
export function originOf(state) {
  const url=state.match(/^Browser tab:.* URL: "([^"]+)"\./m)?.[1];
  try{return new URL(url).origin;}catch{throw new Error('ORIGIN_UNVERIFIED');}
}
export function availableActions(state,controls=[],discover=true) {
  const entries=parseState(state),actions=[];
  for (const c of controls) {
    if(c.op==='click') {
      if(typeof c.name!=='string'||!navigation.test(c.name)||dangerous.test(c.name)) continue;
      const matches=entries.filter(e=>clickRoles.has(e.role)&&(e.name===c.name||e.name.startsWith(c.name+', Value:')));
      if(matches.length===1) actions.push({op:'click',index:matches[0].index,description:'Click '+matches[0].name});
    } else if(c.op==='scroll'&&['up','down'].includes(c.direction)) actions.push({op:'scroll',direction:c.direction,description:'Scroll '+c.direction});
    else if(c.op==='press'&&keys.has(c.key)) actions.push({op:'press',key:c.key,description:'Press '+c.key});
    else if(c.op==='reload') actions.push({op:'reload',description:'Reload current page'});
    else if(!['click','scroll','press','reload'].includes(c.op)) throw new Error('UNSUPPORTED_CONTROL');
  }
  if(discover) for(const e of entries) {
    if(clickRoles.has(e.role)&&navigation.test(e.name)&&!dangerous.test(e.name)&&entries.filter(x=>x.name===e.name).length===1)
      actions.push({op:'click',index:e.index,description:'Click '+e.name});
  }
  const seen=new Set();
  return actions.filter(a=>{const k=JSON.stringify(a);if(seen.has(k))return false;seen.add(k);return true;}).slice(0,30).map((a,i)=>({...a,id:'a'+i}));
}
export async function loadConfig(workspacePath, configHome) {
  const root=configHome ? resolve(configHome) : join(homedir(),'.config');
  const path=join(root,'qualixar-jev-decision-layer','auto-bridge.json');
  const st=await lstat(path);
  if(!st.isFile()||st.isSymbolicLink()||(st.mode&0o077)||st.uid!==userInfo().uid)throw new Error('UNSAFE_BRIDGE_CONFIGURATION');
  const cfg=JSON.parse(await readFile(path,'utf8'));
  const record=cfg.workspaces?.[resolve(workspacePath)];
  if(!record)throw new Error('WORKSPACE_NOT_ENROLLED');
  return record;
}
export async function ipc(socketPath,payload,timeoutMs=15000) {
  const st=await lstat(socketPath);
  if(!st.isSocket()||st.isSymbolicLink()||(st.mode&0o077)||st.uid!==userInfo().uid)throw new Error('BROKER_SOCKET_UNAVAILABLE');
  return new Promise((ok,no)=>{
    const socket=net.createConnection({path:socketPath});socket.setEncoding('utf8');let data='',settled=false;
    const finish=(err,result)=>{if(settled)return;settled=true;clearTimeout(timer);socket.destroy();err?no(err):ok(result);};
    const timer=setTimeout(()=>finish(new Error('BROKER_TIMEOUT')),timeoutMs);
    socket.on('connect',()=>socket.write(JSON.stringify(payload)+'\n'));
    socket.on('error',()=>finish(new Error('BROKER_UNAVAILABLE')));
    socket.on('end',()=>{if(!settled)finish(new Error('BROKER_EOF'));});
    socket.on('data',b=>{
      data+=b.toString('utf8');
      if(data.length>512000)return finish(new Error('BROKER_RESPONSE_SIZE'));
      if(data.includes('\n')) {
        try {const r=JSON.parse(data.split('\n')[0]);r.ok?finish(null,r.result):finish(new Error('DECISION_UNAVAILABLE'));}
        catch {finish(new Error('BROKER_SCHEMA'));}
      }
    });
  });
}
async function execute(tab,a) {
  if(a.op==='click')await tab.click(a.index);
  else if(a.op==='scroll')await tab.pressKey(a.direction==='down'?'PageDown':'PageUp');
  else if(a.op==='press')await tab.pressKey(a.key);
  else if(a.op==='reload')await tab.reload();
}
export function createSession(tab,config) {
  const history=[];let lastReceipt=null;
  async function run(overrides={}) {
    const enrolled={controls:[],discover:true,maxSteps:10,maxMs:45000,minConfidence:.55,...config};
    if(overrides.maxSteps!==undefined&&overrides.maxSteps>enrolled.maxSteps)throw new Error('BROWSER_STEP_BUDGET');
    if(overrides.allowedOrigins!==undefined||overrides.socketPath!==undefined||overrides.controls!==undefined||overrides.goal!==undefined||overrides.decide!==undefined||overrides.discover!==undefined||
       (overrides.maxMs!==undefined&&overrides.maxMs>enrolled.maxMs)||
       (overrides.minConfidence!==undefined&&overrides.minConfidence<enrolled.minConfidence))throw new Error('BROWSER_AUTHORITY_OVERRIDE');
    const c={...enrolled,...overrides};
    if(typeof c.goal!=='string'||!c.goal||!Array.isArray(c.allowedOrigins)||!c.allowedOrigins.length||typeof c.discover!=='boolean'||!Number.isInteger(c.maxSteps)||c.maxSteps<1||c.maxSteps>30||!Number.isFinite(c.maxMs)||c.maxMs<1||c.maxMs>45000||!Number.isFinite(c.minConfidence)||c.minConfidence<.55||c.minConfidence>1||!Array.isArray(c.controls)||(c.waitMs!==undefined&&(!Number.isFinite(c.waitMs)||c.waitMs<1||c.waitMs>5000)))throw new Error('INVALID_BROWSER_CONTRACT');
    const started=performance.now(),offset=history.length;
    const finish=(status,reason)=>({status,reason,actionsExecuted:history.slice(offset).filter(x=>x.executed).length,
      decisions:history.length-offset,elapsedMs:Math.round(performance.now()-started),receipt_id:lastReceipt,
      requiresCodexVerification:status==='needs_verification',native_browser_reused:true});
    const observe=async()=>{
      const s=await tab.getAXState({emit:false,disableDiffing:true});
      if(typeof s!=='string'||s.length>24000||!c.allowedOrigins.includes(originOf(s)))throw new Error('BROWSER_STATE_OR_ORIGIN');
      return s;
    };
    let state,waits=0;
    try {state=await observe();}catch{return finish('handoff','observation_unavailable');}
    for(let i=0;i<c.maxSteps;i++) {
      if(performance.now()-started>=c.maxMs)return finish('handoff','wall_clock_budget');
      let actions;
      try{actions=availableActions(state,c.controls,c.discover);}catch{return finish('handoff','unsupported_controls');}
      if(!actions.length)return finish('handoff','no_observed_candidate');
      let decision;
      try {
        const payload={op:'browser',origin:originOf(state),goal:c.goal,state,actions,history:history.slice(-5),step_index:i};
        decision=c.decide?await c.decide(payload):await ipc(c.socketPath,payload,Math.max(1,Math.min(15000,c.maxMs-(performance.now()-started))));
      } catch{return finish('handoff','decision_unavailable');}
      if(!decision||!Number.isFinite(decision.confidence)||decision.confidence<0||decision.confidence>1||!['DONE','WAIT','HANDOFF',...actions.map(a=>a.id)].includes(decision.choice))return finish('handoff','invalid_decision');
      let fresh;
      try{fresh=await observe();}catch{return finish('handoff','origin_changed');}
      if(performance.now()-started>=c.maxMs)return finish('handoff','wall_clock_budget');
      lastReceipt=decision.receipt_id||null;
      if(fresh!==state){history.push({choice:decision.choice,executed:false,reason:'stale_state'});state=fresh;continue;}
      if(decision.confidence<c.minConfidence)return finish('handoff','low_confidence');
      if(decision.choice==='DONE')return finish('needs_verification','model_reports_visible_completion');
      if(decision.choice==='HANDOFF')return finish('handoff','model_handoff');
      if(decision.choice==='WAIT') {
        history.push({choice:'WAIT',executed:false});
        if(++waits>=3)return finish('handoff','loading_timeout');
        await new Promise(r=>setTimeout(r,Math.max(1,Math.min(c.waitMs??300,c.maxMs-(performance.now()-started)))));
        try{state=await observe();}catch{return finish('handoff','observation_unavailable');}continue;
      }
      waits=0;const action=actions.find(a=>a.id===decision.choice);
      if(history.at(-1)?.noEffect&&history.at(-1).description===action.description)return finish('handoff','no_progress');
      try {
        await execute(tab,action);const next=await observe();
        history.push({choice:decision.choice,description:action.description,executed:true,noEffect:next===state});state=next;
      } catch{return finish('handoff','action_or_origin_error');}
    }
    return finish('handoff','step_limit');
  }
  return {run,inspect:()=>({history:[...history],lastReceipt})};
}
export async function run(tab,config){return createSession(tab,config).run();}
