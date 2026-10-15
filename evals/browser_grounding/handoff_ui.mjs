// Only drives a local synthetic handoff fixture, never a real CAPTCHA.
import {chromium} from '../../vendor/playwright/node_modules/playwright/index.mjs';
import readline from 'node:readline';
const [url,executable]=process.argv.slice(2);
const browser=await chromium.launch({executablePath:executable,headless:true});
const page=await browser.newPage();await page.goto(url);
await page.waitForSelector('#manual:not([hidden])',{timeout:15000});
console.log(JSON.stringify({auto_takeover:true,status:await page.textContent('#status'),reason:await page.textContent('#humanReason')}));
for await(const line of readline.createInterface({input:process.stdin})){
 if(line==='resume'){
  await page.click('#resumeHuman');
  await page.waitForFunction(()=>document.getElementById('status').textContent==='已完成');
  console.log(JSON.stringify({resumed:true,handoff_hidden:await page.locator('#humanHandoff').getAttribute('hidden')!==null}));
  break;
 }
}
await browser.close();

process.exit(0);
