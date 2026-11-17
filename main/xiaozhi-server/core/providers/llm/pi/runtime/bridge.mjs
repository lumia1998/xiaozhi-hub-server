/** Private stdin/stdout bridge. Hub owns persistence, tools, memory and playback. */
import readline from 'node:readline';
import { once } from 'node:events';
import { runAgentLoopContinue } from '@earendil-works/pi-agent-core';
import { createAssistantMessageEventStream } from '@earendil-works/pi-ai';
import { openAICompletionsApi } from '@earendil-works/pi-ai/api/openai-completions.lazy';
import { openAIResponsesApi } from '@earendil-works/pi-ai/api/openai-responses.lazy';
import { anthropicMessagesApi } from '@earendil-works/pi-ai/api/anthropic-messages.lazy';
import { googleGenerativeAIApi } from '@earendil-works/pi-ai/api/google-generative-ai.lazy';

const APIs={'openai-completions':openAICompletionsApi(),'openai-responses':openAIResponsesApi(),
            'anthropic-messages':anthropicMessagesApi(),'google-generative-ai':googleGenerativeAIApi()};

const active = new Map(), secrets = new Set();
// Provider errors may echo the request's key or headers back; they go to the Hub log and admin page.
function redact(text) {
  let s=String(text??'');
  for(const key of secrets)s=s.split(key).join('***');
  return s.replace(/(bearer\s+)[^\s"',]+/gi,'$1***').replace(/([?&](?:key|api[_-]?key|access_token|token)=)[^&\s"']+/gi,'$1***')
          .replace(/\b(sk-|AIza)[A-Za-z0-9_-]{6,}/g,'$1***').slice(0,500);
}
const usage = () => ({input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}});
let writeChain = Promise.resolve();
function send(id, type, data={}) {
  writeChain = writeChain.then(async () => {
    if (!process.stdout.write(JSON.stringify({id,type,...data})+'\n')) await once(process.stdout,'drain');
  });
  return writeChain;
}

function history(rows, model) {
  const messages=[], names=new Map();
  for (const row of rows) {
    if (row.role==='system') continue;
    if (row.role==='user') { messages.push({role:'user',content:row.content||'',timestamp:Date.now()}); continue; }
    if (row.role==='assistant') {
      const content=[];
      if(row.content)content.push({type:'text',text:String(row.content)});
      for(const call of row.tool_calls||[]) {
        const f=call.function||{};
        let args={};try{args=JSON.parse(f.arguments||'{}');}catch{}
        names.set(call.id,f.name);
        content.push({type:'toolCall',id:call.id,name:f.name,arguments:args,...(call.thoughtSignature?{thoughtSignature:call.thoughtSignature}:{})});
      }
      const stored=row._pi_message;
      const originalText=stored?.content?.filter(c=>c.type==='text').map(c=>c.text).join('')||'';
      const currentText=String(row.content||'');
      // A cut/edited assistant message must not replay the original full answer.
      if(stored?.role==='assistant' && stored.api===model.api && stored.model===model.id && originalText===currentText) {
        messages.push(stored);
      } else if(content.length) {
        messages.push({role:'assistant',content,api:model.api,provider:model.provider,model:model.id,usage:usage(),
                       stopReason:row.tool_calls?.length?'toolUse':'stop',timestamp:Date.now()});
      }
    } else if(row.role==='tool') {
      messages.push({role:'toolResult',toolCallId:row.tool_call_id,toolName:names.get(row.tool_call_id)||'tool',
                     content:[{type:'text',text:String(row.content||'')}],isError:false,timestamp:Date.now()});
    }
  }
  return messages;
}

function failedStream(model, reason, aborted=false) {
  const stream=createAssistantMessageEventStream();
  const message={role:'assistant',content:[],api:model.api,provider:model.provider,model:model.id,usage:usage(),
                 stopReason:aborted?'aborted':'error',errorMessage:reason,timestamp:Date.now()};
  queueMicrotask(()=>{stream.push({type:'error',reason:message.stopReason,error:message});stream.end(message);});
  return stream;
}

async function run(request) {
  const {id,model}=request;
  if(active.has(id))return;
  const control=new AbortController(), pending=new Map();
  active.set(id,{control,pending});
  if(request.api_key?.length>=8)secrets.add(request.api_key);
  let rounds=0, calls=0, firstText=false, providerError=null, timedOut=false;
  const timer=setTimeout(()=>{timedOut=true;control.abort();},request.timeout_ms||180000);
  const seen=new Set(), started=performance.now();
  const askTool=(callId,name,args,signal)=>new Promise((resolve,reject)=>{
    const key=callId;
    const abort=()=>{pending.delete(key);reject(new Error('Cancelled'));};
    if(signal?.aborted){abort();return;}
    signal?.addEventListener('abort',abort,{once:true});
    pending.set(key,reply=>{signal?.removeEventListener('abort',abort);resolve(reply);});
    send(id,'tool_request',{call_id:callId,name,arguments:args}).catch(abort);
  });
  try {
    const tools=(request.tools||[]).map(def=>{
      const f=def.function||def;
      return {name:f.name,label:f.name,description:f.description||'',parameters:f.parameters||{type:'object',properties:{}},
        executionMode:'sequential',execute:async(callId,args,signal)=>{
          const result=await askTool(callId,f.name,args,signal);
          return {content:[{type:'text',text:result.text||''}],details:{hub_action:result.action,isError:!!result.error},terminate:!!result.terminate};
        }};
    });
    const system={role:'system',content:request.dialogue.filter(m=>m.role==='system').map(m=>m.content||'').join('\n\n'),timestamp:Date.now()};
    const context={messages:[system,...history(request.dialogue,model)],tools};
    const config={model,apiKey:request.api_key||'hub-local',convertToLlm:messages=>messages,toolExecution:'sequential',
      maxTokens:request.max_tokens||800,maxRetries:0,timeoutMs:90000,cacheRetention:'none',
      reasoning:request.reasoning==='off'?undefined:request.reasoning,
      onPayload:payload=>Object.assign(payload,request.extra_body||{}),
      beforeToolCall:async({toolCall,args},signal)=>{
        if(signal?.aborted)return {block:true,reason:'本轮已取消'};
        if(++calls>16)return {block:true,reason:'本轮工具调用次数已达上限，请直接回复'};
        const key=toolCall.name+':'+JSON.stringify(args);
        if(seen.has(key))return {block:true,reason:'本轮已调用过相同工具和参数，请使用之前结果，不要重复执行'};
        seen.add(key);
      },
      afterToolCall:async({result,isError})=>({isError:isError||!!result.details?.isError,terminate:result.terminate}),
      prepareRequest:({context:ctx})=>rounds>=6?{context:{tools:[],messages:[...ctx.messages,{role:'system',
          content:'工具调用预算已用完，请根据已有结果直接给出简短答复。',
          toolsRemoved:(ctx.tools||[]).map(t=>({name:t.name})),timestamp:Date.now()}]}}:undefined,
      finishTurn:()=>rounds>=6||control.signal.aborted?{action:'end'}:undefined,
    };
    await runAgentLoopContinue(context,config,async event=>{
      if(event.type==='turn_start') {rounds++;await send(id,'round',{round:rounds});}
      else if(event.type==='message_update'&&event.assistantMessageEvent.type==='text_delta') {
        if(!firstText){firstText=true;await send(id,'timing',{first_text_ms:Math.round(performance.now()-started)});}
        await send(id,'text',{text:event.assistantMessageEvent.delta});
      } else if(event.type==='message_end') {
        let message=event.message;
        if(message.role==='assistant' && message.stopReason==='error') {
          providerError=message.errorMessage||'没有返回错误信息';
          message={...message,errorMessage:redact(providerError)};
        }
        await send(id,'message',{message});
      } else if(event.type==='turn_end') {
        await send(id,'turn_end');
      }
    },control.signal,(m,c,o)=>{
      try{return APIs[m.api].streamSimple(m,c,o);}catch(e){return failedStream(m,'模型服务请求未建立：'+(e?.message||e));}
    });
    if(providerError)await send(id,'error',{message:'模型服务请求失败：'+redact(providerError)});
    else if(timedOut)await send(id,'error',{message:'Pi 本轮超时'});
    await send(id,'done',{aborted:control.signal.aborted,rounds,elapsed_ms:Math.round(performance.now()-started)});
  }catch(e) {
    if(!control.signal.aborted)console.error(redact(e?.stack||e));
    await send(id,'error',{message:timedOut?'Pi 本轮超时':control.signal.aborted?'本轮已取消':'Pi 引擎处理失败：'+redact(e?.message||e)});
    await send(id,'done',{aborted:control.signal.aborted,rounds});
  }finally {
    clearTimeout(timer);control.abort();active.delete(id);
  }
}

const input=readline.createInterface({input:process.stdin,crlfDelay:Infinity});
input.on('line',line=>{
  if(line.length>16*1024*1024)return;
  let message;try{message=JSON.parse(line);}catch{return;}
  if(message.type==='run')void run(message);
  else if(message.type==='abort')active.get(message.id)?.control.abort();
  else if(message.type==='tool_result') {
    const waiting=active.get(message.id)?.pending;
    const resolve=waiting?.get(message.call_id);
    if(resolve){waiting.delete(message.call_id);resolve(message);}
  }
});
input.on('close',()=>{for(const state of active.values())state.control.abort();setTimeout(()=>process.exit(0),1000).unref();});
process.on('SIGTERM',()=>{for(const state of active.values())state.control.abort();setTimeout(()=>process.exit(0),1000).unref();});
await send(null,'ready',{engine:'pi-agent-core',version:'1.5.2'});
