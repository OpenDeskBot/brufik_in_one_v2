/* Use only the disposable database created by run_sop_browser.py. */
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright-core');
const assert = require('node:assert/strict');

(async () => {
  const base = process.env.DESKBOT_BROWSER_URL;
  assert(base && new URL(base).hostname === '127.0.0.1');
  const browser = await chromium.launch({headless:true, executablePath:process.env.CHROME_PATH || undefined});
  const page = await browser.newPage({viewport:{width:1440,height:1000}});
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const headers = {'X-Deskbot-Device':'agent-browser-device'};
  try {
    await page.request.post(base+'/login', {headers:{Accept:'application/json'}, form:{email:'browser-ux@example.com',password:'browser-password-123'}});
    await page.goto(base+'/robot-settings');
    await page.waitForFunction(() => !!window.DeskbotUI);
    await page.evaluate(() => DeskbotUI.switchDevice('agent-browser-device'));
    await page.locator('#realtime-prompt').waitFor();
    for (const selector of ['#flecket-agent','#flecket-url','#realtime-key','input[type=password]']) {
      assert.equal(await page.locator('#robotApp '+selector).count(), 0);
    }
    assert(!/ws:\/\/|API Key|Base URL/.test(await page.locator('#robotApp').innerText()));
    const provider = page.getByRole('combobox',{name:'模型选择',exact:true});
    await provider.selectOption('moss-live');
    assert(await page.locator('#realtime-voice option').count() > 0);
    await provider.selectOption('flecket');
    await page.locator('#realtime-prompt').fill('Model settings browser check');
    const pending = page.waitForRequest(r => r.url().endsWith('/api/robot-settings/conversation') && r.method()==='POST');
    await page.getByRole('button',{name:'保存模型选择',exact:true}).click();
    const payload = (await pending).postDataJSON();
    assert(!('url' in payload) && !('api_key' in payload));
    await page.getByText('模型选择已保存，机器人将自动重连',{exact:true}).waitFor();
    const status = await (await page.request.get(base+'/api/robot-settings',{headers})).json();
    assert.equal(status.conversation.prompt,'Model settings browser check');
    assert.equal(status.conversation.url,'ws://localhost:9110/v1/realtime?model=agent_A');
    assert(status.conversation.configured);

    assert.deepEqual(await provider.locator('option').evaluateAll(items=>items.map(x=>x.value)), ['moss-live','flecket']);
    assert.equal(await page.locator('#selected-asr,#selected-llm,#selected-tts,#selected-decision').count(),0);
    const retired=await page.request.post(base+'/api/robot-settings/conversation',{headers,data:{provider:'local'}});
    assert.equal(retired.status(),400);
    assert(!('local' in status.conversation.profiles));
    await page.screenshot({path:'/tmp/deskbot-model-selection.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    await page.setViewportSize({width:1440,height:1000});

    await page.goto(base+'/model-settings');
    await page.locator('#flecket-url').waitFor();
    await page.locator('#realtime-key').waitFor();
    assert.equal(await page.locator('h1').innerText(),'模型配置');
    await page.screenshot({path:'/tmp/deskbot-model-configuration.png',fullPage:true});
    assert.deepEqual(errors,[]);
    console.log('PASS external-only model selection, preserved credentials, developer configuration and mobile layout');
  } finally { await browser.close(); }
})().catch(e => {console.error(e);process.exit(1);});
