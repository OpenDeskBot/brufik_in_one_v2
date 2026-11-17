/* Uses only mocked process status on the disposable seeded browser service. */
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright-core');
const assert=require('node:assert/strict');
(async()=>{
  const base=process.env.DESKBOT_BROWSER_URL;
  assert(base && new URL(base).hostname==='127.0.0.1');
  const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH || undefined});
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  const services=[{name:'insightface-engine',state:'stopped',installed:true,installed_version:'1.1.0',auto_start:true},
    {name:'wespeaker-resnet34',state:'stopped',installed:true,installed_version:'1.0.0',auto_start:false}];
  const operations=[];
  await page.route('**/api/perception-services**',async route=>{
    const request=route.request(),url=new URL(request.url());
    if(request.method()==='POST'){
      const parts=url.pathname.split('/'),s=services.find(s=>s.name===parts[3]),op=parts[4];
      operations.push({name:s.name,op});
      if(op==='auto-start')s.auto_start=request.postDataJSON().enabled;
      else {s.state=op==='stop'?'stopped':'running';s.pid=op==='stop'?null:123;s.healthy=op!=='stop';}
      await route.fulfill({json:{ok:true,service:s}});
    } else await route.fulfill({json:{ok:true,services}});
  });
  try{
    await page.request.post(base+'/login',{headers:{Accept:'application/json'},form:{email:'browser-ux@example.com',password:'browser-password-123'}});
    await page.goto(base+'/perception-services');
    await page.getByRole('heading',{name:'感知服务',exact:true}).waitFor();
    await page.waitForFunction(()=>document.querySelectorAll('.perception-card').length===2);
    const face=page.locator('.perception-card').filter({has:page.getByRole('heading',{name:'人脸识别',exact:true})});
    await face.getByRole('button',{name:'启动',exact:true}).click();
    await face.getByText('运行正常',{exact:true}).waitFor();
    await face.getByRole('button',{name:'停止',exact:true}).click();
    await face.getByText('已停止',{exact:true}).waitFor();
    const voice=page.locator('.perception-card').filter({has:page.getByRole('heading',{name:'声纹识别',exact:true})});
    await voice.getByRole('checkbox').check();
    await page.waitForFunction(()=>document.querySelectorAll('.perception-card input')[1]?.checked);
    assert(operations.some(op=>op.name==='wespeaker-resnet34' && op.op==='auto-start'));
    await page.screenshot({path:'/tmp/deskbot-perception-desktop.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.evaluate(()=>scrollTo(0,0));
    await page.screenshot({path:'/tmp/deskbot-perception-mobile.png'});
    assert.deepEqual(errors,[]);
    console.log('PASS perception services: exactly two engines, controls, automatic start and mobile layout');
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
