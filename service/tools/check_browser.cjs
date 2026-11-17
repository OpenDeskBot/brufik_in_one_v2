/* Run against tests.sop_browser_app with a temporary database, never production. */
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright-core');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.env.CHROME_PATH || undefined});
  const page = await browser.newPage({viewport:{width:1440,height:1000}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const base = process.env.DESKBOT_BROWSER_URL;
  assert(base, 'DESKBOT_BROWSER_URL must point to an isolated test application');
  try {
    const health = await page.request.get(base+'/health');
    assert.deepEqual(await health.json(), {status:'ok'});
    assert(health.headers()['x-request-id']);
    await page.goto(base+'/register');
    await page.locator('#email').fill('browser-sop@example.com');
    await page.locator('#password').fill('browser-password-123');
    await page.locator('#confirm_password').fill('browser-password-123');
    await page.locator('button[type=submit]').click();
    await page.waitForURL('**/home');
    await page.locator('#home').waitFor();
    const pages = ['/home','/my/devices','/my/memories','/my/reminders','/my/people','/advanced',
      '/robot-settings','/model-settings','/expr','/quest','/dev/users','/debug/devices','/debug/llm','/debug/tts','/debug/simulation','/flash','/my/miot','/lab'];
    for (const path of pages) {
      await page.goto(base+path);
      await page.waitForFunction(() => !document.querySelector('#page-status'));
      await page.waitForTimeout(150);
      assert(!await page.locator('body').innerText().then(text=>text.includes('Unknown endpoint:')),path);
      console.log('PASS '+path);
    }
    assert.deepEqual(errors,[]);
    await page.goto(base+'/home');
    await page.waitForFunction(() => !document.querySelector('#page-status'));
    await page.screenshot({path:process.env.DESKBOT_BROWSER_SCREENSHOT || '/tmp/deskbot-sop-home.png',fullPage:true});
    console.log('PASS registration, page scripts and health contract');
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exit(1);});
