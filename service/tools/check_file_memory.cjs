/* Against an isolated seeded app only. Never point this at a real device. */
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright-core');
const assert = require('node:assert/strict');

(async () => {
  const base = process.env.DESKBOT_BROWSER_URL;
  assert(base && new URL(base).hostname === '127.0.0.1', 'Use a disposable local application');
  const browser = await chromium.launch({headless:true, executablePath:process.env.CHROME_PATH || undefined});
  const page = await browser.newPage({viewport:{width:1280,height:1000}});
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  try {
    await page.goto(base+'/login');
    await page.locator('#email').fill('browser-memory@example.com');
    await page.locator('#password').fill('browser-password-123');
    await page.locator('button[type=submit]').click();
    await page.waitForURL('**/home');
    await page.evaluate(() => DeskbotUI.switchDevice('memory-browser-device'));
    await page.goto(base+'/my/memories');
    await page.getByLabel('新人物姓名').fill('浏览器小明');
    await page.getByRole('button',{name:'新增人物',exact:true}).click();
    await page.waitForFunction(() => document.querySelector('select[aria-label="筛选人物"]')?.value.startsWith('p'));
    await page.getByLabel('资料分块').selectOption('爱好');
    const draft = page.getByLabel('新增人物资料');
    await draft.fill('浏览器验证：喜欢摄影');
    await page.getByRole('button',{name:'添加资料',exact:true}).click();
    await page.locator('.note').filter({hasText:'浏览器验证：喜欢摄影'}).waitFor();
    let note = page.locator('.note').filter({hasText:'浏览器验证：喜欢摄影'});
    await note.getByRole('button',{name:'查看来源',exact:true}).click();
    await page.getByRole('region',{name:'来源记录'}).waitFor();
    await note.getByRole('button',{name:'编辑',exact:true}).click();
    await page.getByLabel('编辑人物资料').fill('浏览器验证：喜欢画画');
    await page.getByRole('button',{name:'保存纠正',exact:true}).click();
    await page.locator('.colwrap .note').filter({hasText:'浏览器验证：喜欢画画'}).waitFor();
    console.log('PASS person creation, sourced fact, source lookup and versioned correction');
    await page.getByLabel('筛选人物').selectOption('');
    await page.waitForFunction(() => document.querySelector('#app')?.textContent.includes('共 56 条'));
    await page.getByRole('button',{name:'下一页',exact:true}).click();
    await page.waitForFunction(() => document.querySelector('#app')?.textContent.includes('第 2 页'));
    assert.equal(await page.locator('.colwrap .note').count(),6);
    console.log('PASS complete management pagination beyond 30 entries');
    await page.getByRole('button',{name:'每日记录',exact:true}).click();
    await page.getByLabel('搜索记忆').fill('浏览器验证：喜欢画画');
    await page.getByRole('button',{name:'搜索',exact:true}).click();
    await page.waitForFunction(() => document.querySelector('#app')?.textContent.includes('共 1 条'));
    await page.getByRole('button',{name:'遗忘这段记录',exact:true}).click();
    await page.waitForFunction(() => document.querySelector('#app')?.textContent.includes('共 0 条'));
    const forgotten = await (await page.request.get(base+'/app/api/memory/chat?query='+encodeURIComponent('浏览器验证'))).json();
    assert.equal(forgotten.total,0);
    console.log('PASS linked forgetting removes original source and corrected profile history');
    await page.getByLabel('搜索记忆').fill('');
    await page.getByRole('button',{name:'人物资料',exact:true}).click();
    const selectedPerson = await page.getByLabel('筛选人物').locator('option').filter({hasText:'浏览器小明'}).getAttribute('value');
    await page.getByLabel('筛选人物').selectOption(selectedPerson);
    await draft.fill('重试验证');
    let dropped=false;
    await page.route('**/app/api/memories', async route => {
      if(route.request().method()==='POST'&&!dropped){dropped=true;await route.fetch();await route.abort();}
      else await route.continue();
    });
    await page.getByRole('button',{name:'添加资料',exact:true}).click();
    await page.waitForFunction(() => document.querySelector('p.lead')?.textContent.includes('连接失败'));
    assert.equal(await draft.inputValue(),'重试验证');
    await page.getByRole('button',{name:'添加资料',exact:true}).click();
    await page.waitForFunction(() => document.querySelector('input[aria-label="新增人物资料"]')?.value==='');
    const list=await (await page.request.get(base+'/app/api/memories?query='+encodeURIComponent('重试验证'))).json();
    assert.equal(list.total,1);
    await page.unroute('**/app/api/memories');
    console.log('PASS uncertain response retry keeps draft and writes only one fact');
    await page.screenshot({path:'/tmp/deskbot-file-memory-desktop.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth+2));
    await page.screenshot({path:'/tmp/deskbot-file-memory-mobile.png',fullPage:true});
    assert.deepEqual(errors,[]);
    console.log('PASS desktop/mobile layout and zero page script errors');
  } catch(error) {
    console.log((await page.locator('body').innerText()).slice(-2500));
    throw error;
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exit(1);});
