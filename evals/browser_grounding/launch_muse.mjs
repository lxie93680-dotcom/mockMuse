// Launch the actual Muse extension in an isolated test profile, never personal Chrome.
import {chromium} from './runtime/node_modules/playwright/index.mjs';
const [extension,profile,executable,url]=process.argv.slice(2);
const context=await chromium.launchPersistentContext(profile,{executablePath:executable,channel:'chromium',headless:true,viewport:{width:1280,height:800},args:[`--disable-extensions-except=${extension}`,`--load-extension=${extension}`]});
const worker=context.serviceWorkers()[0]||await context.waitForEvent('serviceworker',{timeout:15000});
await context.pages()[0].goto(url);
console.log(JSON.stringify({ready:true,worker:worker.url(),browser:context.browser()?.version()}));
process.stdin.resume();
process.stdin.on('data',async()=>{await context.close();process.exit(0);});
process.stdin.on('end',async()=>{await context.close();process.exit(0);});
