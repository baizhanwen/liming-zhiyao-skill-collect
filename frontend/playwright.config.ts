import { defineConfig } from '@playwright/test';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
const root=resolve(import.meta.dirname,'..');
const data=mkdtempSync(join(tmpdir(),'skill-collect-ui-'));
const port=Number(process.env.UI_PORT||8766);
export default defineConfig({testDir:'./tests',fullyParallel:false,workers:1,timeout:90000,expect:{timeout:10000},reporter:[['list'],['json',{outputFile:'../artifacts/verification/playwright-report.json'}]],use:{actionTimeout:10000,baseURL:`http://127.0.0.1:${port}`,browserName:'chromium',channel:process.env.PW_CHANNEL==='chromium'?undefined:process.env.PW_CHANNEL||'chrome',viewport:{width:1440,height:1100},trace:'retain-on-failure'},webServer:{command:`"${join(root,'.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python')}" -m uvicorn backend.main:app --host 127.0.0.1 --port ${port} --no-access-log`,url:`http://127.0.0.1:${port}/api/status`,cwd:root,env:{SKILL_COLLECT_DATA:data},reuseExistingServer:false,timeout:30000}});
