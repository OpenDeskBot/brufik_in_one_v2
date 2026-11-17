/* Run only against the disposable seeded browser test service. */
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright-core');
const assert=require('node:assert/strict');
const image=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAFAAAAAoCAIAAADmAupWAAAAV0lEQVR4nOXOMQEAMAyAMIZ/z52L9iAK8oYWiZEYiZEYiZEYiZEYiZEYiZEYiZEYiZEYiZEYiZEYiZEYiZEYiZEYiZEYiZEYiZEYiZEYiZEYiZEYrwPbPovGAU/teAiZAAAAAElFTkSuQmCC','base64');
const music=Buffer.alloc(44+6400);
music.write('RIFF',0);music.writeUInt32LE(music.length-8,4);music.write('WAVEfmt ',8);
music.writeUInt32LE(16,16);music.writeUInt16LE(1,20);music.writeUInt16LE(1,22);
music.writeUInt32LE(16000,24);music.writeUInt32LE(32000,28);music.writeUInt16LE(2,32);
music.writeUInt16LE(16,34);music.write('data',36);music.writeUInt32LE(6400,40);

(async()=>{
  const base=process.env.DESKBOT_BROWSER_URL;
  assert(base && new URL(base).hostname==='127.0.0.1');
  const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH || undefined});
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  try{
    await page.request.post(base+'/login',{headers:{Accept:'application/json'},form:{email:'browser-ux@example.com',password:'browser-password-123'}});
    await page.goto(base+'/my/media');
    await page.waitForFunction(()=>!!window.DeskbotUI);
    await page.evaluate(()=>DeskbotUI.switchDevice('agent-browser-device'));
    await page.getByRole('heading',{name:'媒体库',exact:true}).waitFor();
    await page.getByLabel('选择文件').setInputFiles({name:'红色图片.png',mimeType:'image/png',buffer:image});
    await page.getByRole('button',{name:'上传到当前设备'}).click();
    await page.getByText('上传成功，现在可以让小歪播放或展示了',{exact:true}).waitFor();
    await page.waitForFunction(()=>document.querySelectorAll('.media-card').length===1);
    await page.waitForFunction(()=>document.querySelector('.media-card img')?.naturalWidth===284);
    await page.getByLabel('选择文件').setInputFiles({name:'测试音乐.wav',mimeType:'audio/wav',buffer:music});
    await page.getByRole('button',{name:'上传到当前设备'}).click();
    await page.waitForFunction(()=>document.querySelectorAll('.media-card').length===2);
    assert.equal(await page.locator('.media-card audio').count(),1);
    assert(await page.getByRole('button',{name:'在机器人播放',exact:true}).isDisabled());
    await page.screenshot({path:'/tmp/deskbot-agent-media-desktop.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
    assert.equal(await page.locator('#mediaApp').count(),1);
    await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    await page.screenshot({path:'/tmp/deskbot-agent-media-mobile.png'});
    await page.locator('.media-card').filter({has:page.getByRole('heading',{name:'红色图片'})}).getByRole('button',{name:'删除'}).click();
    await page.waitForFunction(()=>document.querySelectorAll('.media-card').length===1);
    await page.evaluate(()=>DeskbotUI.switchDevice('agent-browser-device-b'));
    await page.getByText('这里还没有资料。上传后，小歪就能按名字找到它们。',{exact:true}).waitFor();
    assert.deepEqual(errors,[]);
    console.log('PASS media upload, preview, device isolation, deletion, disabled offline controls and mobile layout');
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
