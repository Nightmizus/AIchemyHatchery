/* Keep the sandboxed preview alive across console updates and device resizing. */
(() => {
  let frame=null, layer=null, url='', revision='', token='', retry=null;
  let slowTimer=null, failureTimer=null, loadTimer=null;
  const root=()=>document.querySelector('#canvasContent');
  function clearTimers(){clearTimeout(slowTimer);clearTimeout(failureTimer);clearTimeout(loadTimer)}
  function ensureLayer(){
    if(layer?.isConnected)return;
    layer=document.createElement('div');layer.className='preview-load-state';
    layer.innerHTML='<div class="preview-load-content"><span class="preview-load-icon" aria-hidden="true"></span><strong></strong><p></p><div class="preview-load-actions"><button type="button" data-preview-retry>重新加载</button><a target="_blank" rel="noopener noreferrer">在新标签页打开</a></div></div>';
    layer.setAttribute('role','status');layer.setAttribute('aria-live','polite');
    layer.querySelector('button').addEventListener('click',()=>retry?.());
    root().append(layer);
  }
  function state(kind,title,detail){
    ensureLayer();root().dataset.previewState=kind;
    root().setAttribute('aria-busy',kind==='loading'||kind==='slow'?'true':'false');
    layer.hidden=kind==='ready';
    layer.querySelector('strong').textContent=title||'';
    layer.querySelector('p').textContent=detail||'';
    layer.querySelector('.preview-load-actions').hidden=!['slow','error'].includes(kind);
    const open=layer.querySelector('a');open.hidden=!url;if(url)open.href=url;
  }
  function wait(title='正在加载预览…',detail='页面准备好后会自动显示。'){
    clearTimers();state('loading',title,detail);
    slowTimer=setTimeout(()=>state('slow','预览还在加载','页面或网络响应较慢，你可以继续等待，也可以重新加载。'),8000);
    failureTimer=setTimeout(()=>fail('预览暂时未能加载，请重试。'),30000);
  }
  function reset(){
    clearTimers();frame?.remove();layer?.remove();frame=null;layer=null;url='';revision='';token='';
    const container=root();delete container.dataset.previewState;container.removeAttribute('aria-busy');
  }
  function loading(){
    document.querySelector('#siteCanvas').classList.add('file-site-canvas');
    wait('正在读取网站…','正在准备你的页面和文件。');
  }
  function fail(message='预览页面未能正常打开，请重新加载。'){
    clearTimers();state('error','预览加载失败',message);
  }
  function show(nextUrl,nextRevision,{force=false,empty=false}={}){
    if(empty){reset();state('empty','网站还没有页面','在左侧告诉 AI 你想做什么，创建页面后就能在这里预览。');return}
    if(frame?.isConnected&&url===nextUrl&&revision===nextRevision&&!force)return;
    clearTimers();url=nextUrl;revision=nextRevision;
    token=crypto.randomUUID?.()||`${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const currentToken=token;
    wait();
    if(!frame?.isConnected){
      frame=document.createElement('iframe');frame.id='siteFilePreview';frame.title='网站预览';
      frame.setAttribute('sandbox','allow-scripts allow-forms allow-popups allow-downloads');
      frame.referrerPolicy='no-referrer';
      frame.addEventListener('error',()=>fail());
      frame.addEventListener('load',()=>{
        if(root().dataset.previewState==='ready')return;
        const loadedToken=token;
        loadTimer=setTimeout(()=>{if(token===loadedToken&&root().dataset.previewState!=='ready')fail()},500);
      });
    }
    const target=new URL(nextUrl,location.origin);
    target.searchParams.set('v',String(nextRevision||'current'));
    target.searchParams.set('__hatchery_preview',currentToken);
    frame.src=target.href;
    if(!frame.isConnected)root().prepend(frame);
  }
  window.addEventListener('message',event=>{
    if(!frame||event.source!==frame.contentWindow||event.data?.type!=='hatchery-preview-ready'||event.data.token!==token)return;
    clearTimers();state('ready');
  });
  window.HatcheryPreview={show,reset,loading,fail,get navigationId(){return token},configure(callback){retry=callback},reload(){if(url)show(url,revision,{force:true})}};
})();
