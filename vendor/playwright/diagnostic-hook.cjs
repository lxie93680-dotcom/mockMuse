// Version-checked runtime instrumentation; never logs protocol payloads or credentials.
const Module = require('node:module');
const fs = require('node:fs');
const {AsyncLocalStorage} = require('node:async_hooks');
const als = new AsyncLocalStorage();
let written=0;
globalThis.__museTrace = (stage,extra={},ctx=als.getStore()) => {
  if (ctx?.background && !extra.error) return;
  if (!process.env.MUSE_MCP_TRACE_FILE || written>4*1024*1024) return;
  try {const line=JSON.stringify({time:Date.now()/1000,stage,trace_id:ctx?.trace_id,tool:ctx?.tool,background:!!ctx?.background,...extra})+'\n';fs.appendFileSync(process.env.MUSE_MCP_TRACE_FILE,line,{mode:0o600});written+=Buffer.byteLength(line);} catch {}
};
const original=Module.prototype._compile;
Module.prototype._compile=function(source,filename){
 if(filename.endsWith('/playwright-core/lib/coreBundle.js')){
  const edits=[
   ['      async callTool(name, rawArguments = {}, signal) {', `      async callTool(name, rawArguments = {}, signal) {
        const ctx={trace_id:rawArguments._meta?.muse_trace_id,tool:name,background:!!rawArguments._meta?.muse_background};
        return globalThis.__museALS.run(ctx,async()=>{
          globalThis.__museTrace('mcp_tool_received');
          try {const r=await this._museCallTool(name,rawArguments,signal);globalThis.__museTrace('mcp_tool_return',{error:!!r.isError});return r;}
          catch(e){globalThis.__museTrace('mcp_tool_exception',{error:true,error_type:e.name});throw e;}
        });
      }
      async _museCallTool(name, rawArguments = {}, signal) {`],
   ['          await handle(parsedArguments, response2);', "          globalThis.__museTrace('mcp_handler_enter');\n          await handle(parsedArguments, response2);\n          globalThis.__museTrace('mcp_handler_return');"],
   ['        const id = ++this._lastId;\n        this._ws.send(JSON.stringify({ id, method, params: params2 }));', "        const id = ++this._lastId;\n        globalThis.__museTrace('extension_send',{protocol_id:id,method,cdp_method:method==='chrome.debugger.sendCommand'?params2?.[1]:undefined});\n        this._ws.send(JSON.stringify({ id, method, params: params2 }));"],
   ['          this._callbacks.set(id, { resolve, reject, error });', '          this._callbacks.set(id, { resolve, reject, error, museContext:globalThis.__museALS.getStore() });'],
   ['          this._callbacks.delete(object.id);\n          if (object.error)', "          this._callbacks.delete(object.id);\n          globalThis.__museTrace('extension_reply',{protocol_id:object.id,error:!!object.error},callback.museContext);\n          if (object.error)"],
   ['      _onClose(code, reason) {\n        const message = reason.toString();', "      _onClose(code, reason) {\n        globalThis.__museTrace('extension_closed',{code,error:true});\n        const message = reason.toString();"],
   ['      _onError(event) {\n        debugLogger2', "      _onError(event) {\n        globalThis.__museTrace('extension_socket_error',{error:true});\n        debugLogger2"]
  ];
  for(let i=0;i<edits.length;i++){
    const [from,to]=edits[i];
    const start=i<2?0:source.indexOf('// packages/playwright-core/src/tools/mcp/cdpRelay.ts');
    const end=i<2?source.length:source.indexOf('// packages/playwright-core/src/tools/mcp/extensionContextFactory.ts',start);
    let section=source.slice(start,end);
    if(start<0||end<0||section.split(from).length!==2)throw new Error('Muse MCP instrumentation incompatible at patch '+i);
    source=source.slice(0,start)+section.replace(from,to)+source.slice(end);
  }
  const more=[
    ['    serverDebug("callTool", request2);', `    globalThis.__museALS.enterWith({trace_id:request2.params.arguments?._meta?.muse_trace_id,tool:request2.params.name,background:!!request2.params.arguments?._meta?.muse_background});
    globalThis.__museTrace('mcp_request_received');
    serverDebug("callTool", request2);`],
    ['      const backend = await backendPromise;\n      const toolResult', "      globalThis.__museTrace('mcp_backend_wait');\n      const backend = await backendPromise;\n      globalThis.__museTrace('mcp_backend_ready');\n      const toolResult"],
    ['        await this._openConnectPageInBrowser(clientName);', "        globalThis.__museTrace('extension_connect_page');\n        await this._openConnectPageInBrowser(clientName);"],
    ['          await this._extensionConnectionPromise;', "          globalThis.__museTrace('extension_connection_wait');\n          await this._extensionConnectionPromise;\n          globalThis.__museTrace('extension_connected');"]
  ];
  for(const [from,to] of more){if(source.split(from).length!==2)throw new Error('Muse MCP instrumentation mismatch');source=source.replace(from,to);}
  globalThis.__museALS=als;
  globalThis.__museTrace('instrumentation_ready');
 }
 return original.call(this,source,filename);
};
