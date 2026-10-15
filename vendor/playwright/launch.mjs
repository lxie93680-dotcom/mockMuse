import {chromium} from './node_modules/playwright/index.mjs';
import fs from 'node:fs/promises';
const [profile,extension,executable,initial,headless]=process.argv.slice(2);
const context=await chromium.launchPersistentContext(profile,{executablePath:executable,channel:'chromium',headless:headless==='1',viewport:{width:1280,height:800},args:['--remote-debugging-port=0',`--disable-extensions-except=${extension}`,`--load-extension=${extension}`]});
const portFile=(await fs.readFile(profile+'/DevToolsActivePort','utf8')).trim().split('\n');
console.log(JSON.stringify({endpoint:`ws://127.0.0.1:${portFile[0]}${portFile[1]}`,version:context.browser().version()}));
let saved=[];try{saved=JSON.parse(await fs.readFile(profile+'/muse-tabs.json','utf8'));}catch{}
const urls=saved.filter(u=>/^https?:\/\//.test(u));
// Preserve the last pages, including an unfinished human-verification page.
(async()=>{for(let i=0;i<100;i++){try{const r=await fetch(initial);if(r.ok){await context.pages()[0].goto(urls[0]||initial);for(const url of urls.slice(1)){const p=await context.newPage();await p.goto(url);}return;}}catch{}await new Promise(r=>setTimeout(r,200));}})();
process.stdin.resume();
let closing=false;async function close(){if(closing)return;closing=true;const pages=context.pages().map(p=>p.url()).filter(u=>/^https?:\/\//.test(u));await fs.writeFile(profile+'/muse-tabs.json',JSON.stringify(pages),{mode:0o600});await context.close();process.exit(0);}
process.stdin.on('data',close);
process.stdin.on('end',close);
