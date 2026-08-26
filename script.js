const elementCatalog = {
  nav:{name:'导航栏'}, hero:{name:'首页大字'}, projects:{name:'作品展示卡片栏'}, blog:{name:'文章列表'}, gallery:{name:'图片画廊'}, stats:{name:'数据栏'}, team:{name:'成员展示'}, timeline:{name:'活动时间线'}, forum:{name:'论坛板块'}, account:{name:'账号登录'}, notice:{name:'公告栏'}, links:{name:'链接集合'}, cta:{name:'行动区域'}, footer:{name:'页脚'}, detail:{name:'详情正文'}
};

const INITIAL_STATE = {
  siteName:'未命名网站', description:'在这里写下网站的一句话介绍。', theme:'minimal', background:'#ffffff', contentWidth:100, sectionGap:false,
  pages:[{id:'page-1',name:'首页',path:'home',parentId:null,kind:'page',elements:[]}], activePageId:'page-1'
};
const state = JSON.parse(JSON.stringify(INITIAL_STATE));
let currentConsoleUser=null;
let DRAFT_KEY='alchemysites:guest:draft:v3';
let AI_UNDO_KEY='alchemysites:guest:ai-undo:v2';
let AI_RELOAD_NOTICE_KEY='alchemysites:guest:ai-reload-notice:v2';

const previewDB = {
  forumPosts:[
    {id:'post-1',title:'今年百团大战，哪些摊位值得逛？',author:'小满',body:'欢迎分享你最期待的社团和理由。',replies:[{author:'星野',body:'AI 社和天文社都很值得逛！'}]},
    {id:'post-2',title:'求推荐一门有趣的公选课',author:'一鸣',body:'想找一门实践多一点的课。',replies:[]},
    {id:'post-3',title:'开源校园地图上线啦！',author:'予安',body:'第一版已经覆盖教学楼和食堂，欢迎反馈。',replies:[{author:'游客',body:'希望可以加上饮水机位置。'},{author:'小满',body:'已加入下一版计划。'}]}
  ],
  registrations:new Set(), joined:false, accountLoggedIn:false
};

let dragPayload=null;
let nextElementId=1;
let nextPageId=2;
let nextItemId=1;
let editingElementId=null;
let editingItemId=null;
let contextElementId=null;
let previewDevice=sessionStorage.getItem('alchemysites:preview-device')||sessionStorage.getItem('miaoda:preview-device')||'desktop';
let previewZoom=Math.max(50,Math.min(125,Number(sessionStorage.getItem('alchemysites:preview-zoom')||sessionStorage.getItem('miaoda:preview-zoom'))||100));
let historyStack=[JSON.stringify(state)];
let historyIndex=0;
let historyTimer=null;

const activePage=()=>state.pages.find(page=>page.id===state.activePageId);
const esc=value=>String(value??'').replace(/[&<>'"]/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
const cssSize=(value,fallback)=>{const text=String(value||'').trim();return /^(?:\d+(?:\.\d+)?(?:px|rem|em|vw|vh|%)|clamp\([\d\s.,%a-z+-]+\))$/i.test(text)?text:fallback};
const cssColor=(value,fallback)=>{const text=String(value||'').trim();return /^(?:#[0-9a-f]{3,8}|rgba?\([\d\s.,%]+\)|hsla?\([\d\s.,%]+\)|transparent|currentColor|var\(--(?:page-bg|page-fg|page-accent|page-soft)\))$/i.test(text)?text:fallback};
const uid=prefix=>`${prefix}-${Date.now()}-${prefix==='page'?nextPageId++:prefix==='item'?nextItemId++:nextElementId++}`;

function pageFullPath(page){
  const parts=[];let current=page;let guard=0;
  while(current&&guard++<20){parts.unshift(current.path);current=current.parentId?state.pages.find(item=>item.id===current.parentId):null}
  return parts.filter(Boolean).join('/');
}
function pageAncestors(page){const result=[];let current=page;while(current){result.unshift(current);current=current.parentId?state.pages.find(item=>item.id===current.parentId):null}return result}
function defaultNavPages(){return state.pages.filter(page=>page.parentId===null&&page.kind!=='detail')}
function selectedNavPages(settings={}){const ids=Array.isArray(settings.pageIds)?settings.pageIds:null;const pages=ids?ids.map(id=>state.pages.find(page=>page.id===id)).filter(Boolean):defaultNavPages();return pages.length?pages:[state.pages[0]]}
function pageLinks(settings={}){return selectedNavPages(settings).map(page=>`<button data-preview-action="navigate" data-page-id="${page.id}" class="${page.id===state.activePageId?'active':''}">${esc(page.name)}</button>`).join('')}

function detailBody(kind,title){
  return kind==='project'?`${title} 从一个校园里的真实需求出发，经历了调研、原型、开发和测试。\n\n这里可以继续补充项目背景、设计过程、技术方案、成员分工与最终成果。`:`${title} 的正文从这里开始。你可以直接编辑这段内容，也可以继续从左侧加入图片、时间线、成员、链接或其他页面元素。\n\n详情页是页面树中的真实子页面，发布后拥有自己的网址。`;
}
function createDetailPage(parentId,kind,item){
  const pageId=uid('page');
  const detailElement={id:uid('el'),type:'detail',settings:{title:item.title,description:item.summary||item.meta||'',body:detailBody(kind,item.title),image:item.image||'',detailType:kind}};
  const page={id:pageId,name:item.title,path:`${kind}-${item.id.split('-').pop()}`,parentId,kind:'detail',elements:[detailElement]};
  state.pages.push(page);item.pageId=pageId;return page;
}
function makeCollectionItem(type,index){
  const projectDefaults=[['校园智能助手','设计 / 开发 / 2026'],['社团活动地图','产品 / 地图 / 2026'],['AI 创作实验','研究 / 创作 / 2026']];
  const blogDefaults=[['如何组织一场校园 Hackathon','08.24 · 6 MIN'],['从提示词到第一个 Demo','08.16 · 4 MIN'],['我们造了一台会说话的树莓派','08.03 · 8 MIN']];
  const source=(type==='projects'?projectDefaults:blogDefaults)[index]||[type==='projects'?`新作品 ${index+1}`:`新文章 ${index+1}`,'刚刚'];
  return {id:uid('item'),title:source[0],meta:source[1],summary:type==='projects'?'一个从校园需求出发的真实作品。':'点击进入真实文章详情页。',image:'',pageId:null};
}
function createElement(type,parentPage=activePage()){
  const element={id:uid('el'),type};
  if(type==='projects'||type==='blog'){
    element.items=[0,1,2].map(index=>makeCollectionItem(type,index));
    element.items.forEach(item=>createDetailPage(parentPage.id,type==='projects'?'project':'article',item));
  }
  return element;
}
function ensureForumAccount(page){const forumIndex=page.elements.findIndex(element=>element.type==='forum');if(forumIndex<0||page.elements.some(element=>element.type==='account'))return false;page.elements.splice(forumIndex,0,createElement('account',page));return true}
function ensureForumAccounts(){let added=false;state.pages.forEach(page=>{if(ensureForumAccount(page))added=true});return added}
ensureForumAccounts();historyStack=[JSON.stringify(state)];historyIndex=0;
function removePageCascade(pageId){
  const ids=new Set([pageId]);let changed=true;
  while(changed){changed=false;state.pages.forEach(page=>{if(page.parentId&&ids.has(page.parentId)&&!ids.has(page.id)){ids.add(page.id);changed=true}})}
  state.pages=state.pages.filter(page=>!ids.has(page.id));
  state.pages.forEach(page=>page.elements.forEach(element=>{if(Array.isArray(element.settings?.pageIds))element.settings.pageIds=element.settings.pageIds.filter(id=>!ids.has(id))}));
}
function cleanupElementDetails(elements){elements.forEach(element=>{if(Array.isArray(element.items))element.items.forEach(item=>{if(item.pageId)removePageCascade(item.pageId)})})}
function collectionItems(element,type){if(!Array.isArray(element.items))element.items=[0,1,2].map(index=>makeCollectionItem(type,index));return element.items}
function imageMarkup(item,index){return item.image?`<img src="${esc(item.image)}" alt="${esc(item.title)}">`:`<span>${String(index+1).padStart(2,'0')}</span>`}

function blockContent(type,element={}){
  const settings=element.settings||{};
  const editing=editingElementId===element.id&&type!=='forum';
  const raw=(key,fallback)=>String(settings[key]??fallback);
  const setting=(key,fallback)=>esc(raw(key,fallback));
  const direct=(key,fallback,multiline=false,placeholder='点击输入文字')=>{const value=raw(key,fallback);const html=esc(value).replace(/\n/g,multiline?'<br>':' ');return editing?`<span class="direct-edit${multiline?' direct-multiline':''}" contenteditable="plaintext-only" spellcheck="false" data-direct-setting="${key}" data-direct-multiline="${multiline?'true':'false'}" data-placeholder="${esc(placeholder)}">${html}</span>`:html};
  const directList=(key,defaults,index,placeholder='点击输入')=>{const values=raw(key,defaults.join('|')).split('|');const value=values[index]??defaults[index]??'';return editing?`<span class="direct-edit" contenteditable="plaintext-only" spellcheck="false" data-direct-list-setting="${key}" data-direct-index="${index}" data-direct-defaults="${esc(defaults.join('|'))}" data-placeholder="${esc(placeholder)}">${esc(value)}</span>`:esc(value)};
  const directItem=(item,key,placeholder='点击输入')=>editing?`<span class="direct-edit" contenteditable="plaintext-only" spellcheck="false" data-direct-item-id="${item.id}" data-direct-item-field="${key}" data-placeholder="${esc(placeholder)}">${esc(item[key]||'')}</span>`:esc(item[key]||'');
  const directPage=page=>editing?`<span class="direct-edit" contenteditable="plaintext-only" spellcheck="false" data-direct-page-name="${page.id}" data-placeholder="页面名称">${esc(page.name)}</span>`:esc(page.name);
  const linkedPages=()=>selectedNavPages(settings).map(page=>`<button data-preview-action="navigate" data-page-id="${page.id}" class="${page.id===state.activePageId?'active':''}">${directPage(page)}</button>`).join('');
  const name=esc(state.siteName||'未命名网站');
  const title=fallback=>direct('title',fallback,true,'点击编辑标题');
  if(type==='nav')return `<nav class="b-nav"><b>${direct('title',state.siteName||'未命名网站',false,'点击编辑站点名称')}</b><div>${linkedPages()}</div></nav>`;
  if(type==='hero')return `<section class="b-hero" style="--hero-title-size:${esc(cssSize(settings.titleSize,'clamp(48px,7vw,102px)'))}"><span>${direct('eyebrow','HELLO / 你好',false,'点击编辑眉题')}</span><h2>${title('把想法，\n变成真正的作品。')}</h2><p>${direct('description',state.description||'在这里写下网站的一句话介绍。',true,'点击编辑介绍')}</p><button class="b-button" data-preview-action="next-section">${direct('button','开始了解',false,'按钮文字')} ↗</button></section>`;
  if(type==='projects'){
    const items=collectionItems(element,'projects');const add=editingElementId===element.id?'<button class="collection-add project-card" data-editor-action="add-item"><div class="project-image">＋</div><h3>新增作品</h3><p>同时创建一个详情子页面</p></button>':'';
    return `<section class="block-section"><header class="block-head"><div><span class="block-kicker">${direct('description','SELECTED WORK',false,'栏目眉题')}</span><h2>${title('最近的作品')}</h2></div><span class="block-link">${direct('button','点击卡片查看详情',false,'引导文字')} →</span></header><div class="project-grid">${items.map((item,index)=>`<button class="project-card${editing?' item-editable':''}" data-preview-action="project" data-item-id="${item.id}" data-page-id="${item.pageId||''}"><div class="project-image">${imageMarkup(item,index)}</div><h3>${directItem(item,'title','作品标题')}</h3><p>${directItem(item,'meta','作品信息')}</p></button>`).join('')}${add}</div></section>`;
  }
  if(type==='blog'){
    const items=collectionItems(element,'blog');const add=editingElementId===element.id?'<button class="collection-add article-row" data-editor-action="add-item"><span>＋</span><b>新增文章</b><em>创建详情页</em></button>':'';
    return `<section class="block-section"><header class="block-head"><div><span class="block-kicker">${direct('description','JOURNAL',false,'栏目眉题')}</span><h2>${title('最新文章')}</h2></div><span class="block-link">${direct('button','进入文章',false,'引导文字')} →</span></header><div class="blog-list">${items.map(item=>`<button class="article-row${editing?' item-editable':''}" data-preview-action="article" data-item-id="${item.id}" data-page-id="${item.pageId||''}"><span>${directItem(item,'meta','日期与阅读时长')}</span><b>${directItem(item,'title','文章标题')}</b><em>DETAIL</em></button>`).join('')}${add}</div></section>`;
  }
  if(type==='gallery')return `<section class="block-section"><header class="block-head"><div><span class="block-kicker">${direct('description','MOMENTS',false,'栏目眉题')}</span><h2>${title('活动瞬间')}</h2></div></header><div class="gallery-grid">${[1,2,3,4].map(n=>`<button data-preview-action="gallery" data-index="${n}">0${n}</button>`).join('')}</div></section>`;
  if(type==='stats'){const valueDefaults=['86','24','12'],labelDefaults=['社团成员','开源项目','本学期活动'];return `<section class="stats-row">${[0,1,2].map(index=>`<div class="stat"><b>${directList('title',valueDefaults,index,'数据')}</b><small>${directList('description',labelDefaults,index,'说明')}</small></div>`).join('')}</section>`}
  if(type==='team'){const names=['林小满','陈星野','周一鸣','苏予安'],roles=['设计','开发','内容','运营'],faces=[':)',':D',';)','^^'];const currentNames=raw('memberNames',names.join('|')).split('|'),currentRoles=raw('memberRoles',roles.join('|')).split('|');return `<section class="block-section"><header class="block-head"><div><span class="block-kicker">${direct('description','OUR TEAM',false,'栏目眉题')}</span><h2>${title('认识成员')}</h2></div></header><div class="team-grid">${names.map((_,index)=>`<button class="member" data-preview-action="member" data-title="${esc(currentNames[index]||names[index])}" data-role="${esc(currentRoles[index]||roles[index])}"><div class="member-face">${directList('memberFaces',faces,index,'头像字符')}</div><h3>${directList('memberNames',names,index,'成员姓名')}</h3><p>${directList('memberRoles',roles,index,'成员职责')}</p></button>`).join('')}</div></section>`}
  if(type==='timeline'){const dates=['09.05','09.12','09.20'],names=['新学期招新','AI 入门工作坊','校园 Hackathon'],places=['大学生活动中心','实验楼 302','创新创业中心'];const currentNames=raw('eventNames',names.join('|')).split('|');return `<section class="block-section"><header class="block-head"><div><span class="block-kicker">${direct('description','UPCOMING',false,'栏目眉题')}</span><h2>${title('活动安排')}</h2></div></header><div class="timeline">${names.map((_,index)=>{const eventName=currentNames[index]||names[index];const joined=previewDB.registrations.has(eventName);return `<article class="time-row"><span>${directList('eventDates',dates,index,'日期')}</span><div><b>${directList('eventNames',names,index,'活动名称')}</b><p>${directList('eventPlaces',places,index,'活动地点')}</p><button class="event-action" data-preview-action="register" data-event="${esc(eventName)}">${joined?'已报名 ✓':'报名活动 →'}</button></div></article>`}).join('')}</div></section>`}
  if(type==='forum')return `<section class="forum-block"><aside class="forum-nav"><b>${name}</b><span>⌂　热门讨论</span><span>◉　AI 研究所</span><span>◇　摄影漫游</span><span>♬　乐队排练室</span><span>＋　发现更多</span></aside><div class="forum-content"><span class="block-kicker">预览效果 · 不代表最终论坛内容</span><h2>${title('大家都在聊')}</h2>${settings.description?`<p>${setting('description','')}</p>`:''}<button class="b-button" data-preview-action="forum-compose">＋ ${setting('button','发布话题')}</button>${previewDB.forumPosts.map((post,index)=>`<button class="forum-topic" data-preview-action="forum-thread" data-post-id="${post.id}"><span>${String(index+1).padStart(2,'0')}</span><b>${esc(post.title)}</b><em>${post.replies.length} 回复</em></button>`).join('')}</div></section>`;
  if(type==='account'){const loginLabel=raw('button','登录账号');const panel=previewDB.accountLoggedIn?`<span class="account-avatar">A</span><div><small>当前账号</small><b>admin</b></div><button data-preview-action="account-logout">退出账号</button>`:`<div><small>预览账号固定为</small><b>admin</b></div><button data-preview-action="account-login">${direct('button','登录账号',false,'按钮文字')} →</button>`;return `<section class="account-block"><div class="account-copy"><span class="block-kicker">${direct('eyebrow','MEMBER ACCESS',false,'栏目眉题')}</span><h2>${title('登录你的账号')}</h2><p>${direct('description','登录后即可查看成员内容与参与社区互动。',true,'点击编辑说明')}</p></div><div class="account-panel" data-login-label="${esc(loginLabel)}">${panel}</div></section>`}
  if(type==='notice')return `<section class="notice-block" style="--notice-bg:${esc(cssColor(settings.background,'var(--page-accent)'))};--notice-fg:${esc(cssColor(settings.color,'#141510'))}"><span>!</span><b>${title('秋季招新开始啦 · 9 月 5 日活动中心见')}</b><button data-preview-action="notice">${direct('button','查看详情',false,'按钮文字')} →</button></section>`;
  if(type==='links'){const labelDefaults=['加入交流群','查看 GitHub','关注公众号'];const urls=raw('description','#group|https://github.com|#wechat').split('|');return `<section class="links-block">${[0,1,2].map(index=>{const label=raw('title',labelDefaults.join('|')).split('|')[index]||'相关链接';return `<button class="link-card" data-preview-action="link" data-title="${esc(label)}" data-url="${esc(urls[index]||'')}"><b>${directList('title',labelDefaults,index,'链接名称')}</b><span>↗</span></button>`}).join('')}</section>`}
  if(type==='cta')return `<section class="cta-block"><h2>${title('一起把下一个好点子做出来。')}</h2><button data-preview-action="join">${previewDB.joined?'已经加入 ✓':direct('button','现在加入',false,'按钮文字')+' ↗'}</button></section>`;
  if(type==='detail'){
    const page=activePage();const parent=page.parentId?state.pages.find(item=>item.id===page.parentId):null;const body=esc(settings.body||detailBody(settings.detailType||'article',settings.title||page.name)).split(/\n\n+/).map(text=>`<p>${text.replace(/\n/g,'<br>')}</p>`).join('');
    const detailBodyMarkup=editing?`<div class="detail-body direct-edit direct-block" contenteditable="plaintext-only" spellcheck="true" data-direct-setting="body" data-direct-multiline="true" data-placeholder="点击这里直接撰写正文">${body}</div>`:`<div class="detail-body">${body}</div>`;
    return `<article class="detail-block">${parent?`<button class="detail-back" data-preview-action="navigate" data-page-id="${parent.id}">← 返回 ${esc(parent.name)}</button>`:''}<span class="block-kicker">${direct('eyebrow',settings.detailType==='project'?'PROJECT DETAIL':'ARTICLE DETAIL',false,'详情类型')}</span><h1 style="--detail-title-size:${esc(cssSize(settings.titleSize,'clamp(48px,8vw,105px)'))}">${title(page.name)}</h1><p class="detail-lead">${direct('description','在这里填写详情摘要。',true,'点击编辑摘要')}</p>${settings.image?`<div class="detail-cover"><img src="${esc(settings.image)}" alt="${esc(settings.title||page.name)}"></div>`:''}${detailBodyMarkup}</article>`;
  }
  return `<footer class="b-footer"><div><b>${direct('title',state.siteName||'未命名网站',false,'点击编辑页脚名称')}</b>${linkedPages()}</div><small>${direct('description','© 2026 · 由 AIchemySites 搭建',false,'点击编辑版权文字')}</small></footer>`;
}

function editDefaults(element){
  const defaults={nav:{title:state.siteName,description:'',button:''},hero:{title:'把想法，\n变成真正的作品。',description:state.description,button:'开始了解',titleSize:'clamp(48px,7vw,102px)'},projects:{title:'最近的作品',description:'SELECTED WORK',button:'点击卡片查看详情'},blog:{title:'最新文章',description:'JOURNAL',button:'进入文章'},gallery:{title:'活动瞬间',description:'MOMENTS',button:''},stats:{title:'86|24|12',description:'社团成员|开源项目|本学期活动',button:''},team:{title:'认识成员',description:'OUR TEAM',button:''},timeline:{title:'活动安排',description:'UPCOMING',button:''},forum:{title:'大家都在聊',description:'',button:'发布话题'},account:{title:'登录你的账号',description:'登录后即可查看成员内容与参与社区互动。',button:'登录账号'},notice:{title:'秋季招新开始啦 · 9 月 5 日活动中心见',description:'',button:'查看详情',background:'var(--page-accent)',color:'#141510'},links:{title:'加入交流群|查看 GitHub|关注公众号',description:'#group|https://github.com|#wechat',button:''},cta:{title:'一起把下一个好点子做出来。',description:'',button:'现在加入'},footer:{title:state.siteName,description:'© 2026 · 由 AIchemySites 搭建',button:''},detail:{title:activePage().name,description:'在这里填写详情摘要。',body:'在这里填写详情正文。',button:''}};
  return {...defaults[element.type],...(element.settings||{})};
}
function inlineEditorMarkup(element){
  const values=editDefaults(element);const complex=element.type==='projects'||element.type==='blog';const currentItem=complex?element.items?.find(item=>item.id===editingItemId):null;
  const navChooser=element.type==='nav'?`<div class="inline-page-chooser"><b>显示在导航中的页面</b>${state.pages.map(page=>`<label style="--depth:${pageAncestors(page).length-1}"><input type="checkbox" data-nav-page="${page.id}" ${selectedNavPages(values).some(item=>item.id===page.id)?'checked':''}><span>${esc(page.name)}</span><small>/${esc(pageFullPath(page))}</small></label>`).join('')}</div>`:'';
  const instanceStyleFields=element.type==='hero'?`<label>当前大字字号<input data-inline-setting="titleSize" value="${esc(values.titleSize)}" placeholder="例如 clamp(38px,5.5vw,78px)"></label>`:element.type==='notice'?`<label>当前公告底色<input data-inline-setting="background" value="${esc(values.background)}" placeholder="var(--page-bg) 或 #ffffff"></label><label>当前公告文字色<input data-inline-setting="color" value="${esc(values.color)}" placeholder="var(--page-fg) 或 #111111"></label>`:'';
  const itemEditor=currentItem?`<div class="inline-item-editor"><header><div><b>${esc(currentItem.title||'未命名条目')}</b><small>标题和辅助信息请直接在上方卡片中修改</small></div><button data-editor-action="close-item">完成条目</button></header><label>详情页摘要<textarea data-item-field="summary" rows="2">${esc(currentItem.summary||'')}</textarea></label><label>图片地址<input data-item-field="image" value="${esc(currentItem.image||'')}" placeholder="https://… 或上传本地图片"></label><label class="inline-upload">上传图片<input type="file" accept="image/*" data-item-upload></label><button class="inline-danger" data-editor-action="delete-item">删除这个条目及详情页</button></div>`:'';
  const detailMedia=element.type==='detail'?`<div class="inline-fields"><label>封面图片地址<input data-inline-setting="image" value="${esc(values.image||'')}" placeholder="https://…；正文直接在页面中修改"></label></div>`:'';
  const linkSettings=element.type==='links'?`<div class="inline-fields">${(values.description||'#group|https://github.com|#wechat').split('|').map((url,index)=>`<label>链接 ${index+1} 地址<input data-pipe-setting="description" data-pipe-index="${index}" value="${esc(url)}" placeholder="https://… 或 #group"></label>`).join('')}</div>`:'';
  const auxiliary=instanceStyleFields?`<div class="inline-fields">${instanceStyleFields}</div>`:'';
  return `<div class="inline-editor direct-editor-dock" data-inline-editor><header><div><small>DIRECT EDIT / 所见即所得</small><b>直接点击上方虚线文字编辑 · ${elementCatalog[element.type].name}</b></div><button data-editor-action="done">完成编辑</button></header>${auxiliary}${navChooser}${linkSettings}${detailMedia}${complex?'<p class="inline-tip">标题和辅助信息可直接输入；点击图片打开图片与详情设置，点击“＋”新增条目。</p>':''}${itemEditor}</div>`;
}
function elementMarkup(element){const immutable=element.type==='forum';const editing=editingElementId===element.id&&!immutable;return `<section class="page-element${editing?' editing':''}" data-element-id="${element.id}" data-element-type="${element.type}"><div class="block-tools"><button class="edit-block" ${immutable?'disabled title="论坛是动态页面，不可编辑"':''}>${immutable?'动态页面不可编辑':editing?'完成':'编辑'}</button><button class="drag-handle" draggable="true" title="拖动排序">拖动</button><button class="delete-block" title="删除元素">删除</button></div>${blockContent(element.type,element)}${editing?inlineEditorMarkup(element):''}</section>`}
function insertionZone(index){return `<div class="insert-zone" data-insert-index="${index}"></div>`}

let draftSaveTimer=null;
function setSaveState(label,saving=false){const node=document.querySelector('#saveState');if(!node)return;node.classList.toggle('saving',saving);node.querySelector('span').textContent=label}
function updateHistoryButtons(){const undo=document.querySelector('#undoBtn'),redo=document.querySelector('#redoBtn');if(undo)undo.disabled=historyIndex<=0;if(redo)redo.disabled=historyIndex>=historyStack.length-1}
function captureHistoryNow(){clearTimeout(historyTimer);const snapshot=JSON.stringify(state);if(snapshot===historyStack[historyIndex]){updateHistoryButtons();return}historyStack=historyStack.slice(0,historyIndex+1);historyStack.push(snapshot);if(historyStack.length>80)historyStack.shift();historyIndex=historyStack.length-1;updateHistoryButtons()}
function scheduleHistoryCapture(){clearTimeout(historyTimer);historyTimer=setTimeout(captureHistoryNow,420)}
async function persistDraftSnapshot(snapshot){
  if(!currentConsoleUser)return;
  try{localStorage.setItem(DRAFT_KEY,snapshot)}catch{}
  const response=await fetch('/api/console/draft',{method:'POST',headers:{'Content-Type':'application/json'},body:snapshot});
  if(response.status===401){showAuthGate('登录已过期，请重新登录');throw new Error('登录已过期')}
  if(!response.ok){let payload={};try{payload=await response.json()}catch{}throw new Error(payload.error||'云端草稿保存失败')}
}
function scheduleDraftSave(){if(!currentConsoleUser)return;setSaveState('保存中…',true);clearTimeout(draftSaveTimer);draftSaveTimer=setTimeout(async()=>{try{const snapshot=JSON.stringify(state);await persistDraftSnapshot(snapshot);const time=new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});setSaveState(`已同步 · ${time}`,false)}catch{setSaveState('同步失败',false)}},350);scheduleHistoryCapture()}
function restoreHistory(index){if(index<0||index>=historyStack.length)return;const message=index<historyIndex?'已撤销':'已重做';historyIndex=index;const snapshot=JSON.parse(historyStack[index]);Object.keys(state).forEach(key=>delete state[key]);Object.assign(state,snapshot);editingElementId=null;editingItemId=null;renderPages();syncFields();renderCanvas();updateHistoryButtons();showToast(message)}
function undoState(){captureHistoryNow();if(historyIndex>0)restoreHistory(historyIndex-1)}
function redoState(){if(historyIndex<historyStack.length-1)restoreHistory(historyIndex+1)}
function renderCanvas(){
  const page=activePage();if(!page)return;
  const canvas=document.querySelector('#siteCanvas');canvas.className=`site-canvas theme-${state.theme}${state.sectionGap?' with-gaps':''}`;canvas.dataset.previewDevice=previewDevice;canvas.style.setProperty('--page-bg',state.background);canvas.style.setProperty('--canvas-width',previewDevice==='desktop'?`${state.contentWidth}%`:`${previewDevice==='tablet'?768:390}px`);canvas.style.zoom=String(previewZoom/100);
  const content=document.querySelector('#canvasContent');content.innerHTML=page.elements.length?insertionZone(0)+page.elements.map((element,index)=>elementMarkup(element)+insertionZone(index+1)).join(''):`<div class="empty-canvas" data-empty-drop><div><i>＋</i><b>这是一个空白页面</b><small>从左侧拖入元素，开始搭建</small></div></div>`;
  bindCanvasEvents();document.querySelector('#currentPageFooter').textContent=`${page.name} · ${page.elements.length} 个元素`;const selected=editingElementId?getElementById(editingElementId):null;document.querySelector('#selectionState').textContent=selected?`正在编辑：${elementCatalog[selected.type]?.name||selected.type}`:`${page.elements.length} 个模块`;document.querySelector('#zoomValue').textContent=`${previewZoom}%`;document.querySelectorAll('[data-preview-device]').forEach(button=>button.classList.toggle('active',button.dataset.previewDevice===previewDevice));scheduleDraftSave();
}

function renderPages(){
  const tree=document.querySelector('#pageTree');
  const branch=(parentId,depth)=>state.pages.filter(page=>page.parentId===parentId).map(page=>`<div class="page-tree-branch"><div class="page-tree-node${page.id===state.activePageId?' active':''}" role="treeitem" aria-current="${page.id===state.activePageId?'page':'false'}" style="--depth:${depth}"><button class="page-tree-select" data-page-id="${page.id}"><span>${page.kind==='detail'?'↳':'□'}</span><b>${esc(page.name)}</b></button><button class="tree-add-child" data-parent-id="${page.id}" title="添加子页面">＋</button><button class="tree-duplicate" data-duplicate-page="${page.id}" title="复制页面及子页面">⧉</button>${page.id!==state.pages[0].id?`<button class="tree-delete" data-delete-page="${page.id}" title="删除页面">×</button>`:'<i></i>'}</div>${branch(page.id,depth+1)}</div>`).join('');
  tree.innerHTML=branch(null,0);tree.querySelectorAll('[data-page-id]').forEach(button=>button.addEventListener('click',()=>selectPage(button.dataset.pageId)));tree.querySelectorAll('[data-parent-id]').forEach(button=>button.addEventListener('click',()=>addPage(button.dataset.parentId)));tree.querySelectorAll('[data-duplicate-page]').forEach(button=>button.addEventListener('click',()=>duplicatePage(button.dataset.duplicatePage)));tree.querySelectorAll('[data-delete-page]').forEach(button=>button.addEventListener('click',()=>deletePage(button.dataset.deletePage)));
  document.querySelector('#pageCount').textContent=`${state.pages.length} 个页面`;document.querySelector('#currentBreadcrumb').textContent=pageAncestors(activePage()).map(page=>page.name).join(' / ');
}
function syncFields(){
  const page=activePage();document.querySelector('#siteName').value=state.siteName;document.querySelector('#siteDescription').value=state.description;document.querySelector('#pageName').value=page.name;document.querySelector('#pagePath').value=page.path;
  const parent=page.parentId?state.pages.find(item=>item.id===page.parentId):null;const base=`/${currentConsoleUser?.username||'…'}/`;document.querySelector('#pathPrefix').textContent=parent?`${base}${pageFullPath(parent)}/`:base;document.querySelector('#titlePreview').textContent=`${page.name}｜${state.siteName}`;
}
function selectPage(id){if(!state.pages.some(page=>page.id===id))return;state.activePageId=id;editingElementId=null;editingItemId=null;renderPages();syncFields();renderCanvas();document.querySelector('#canvasScroll').scrollTop=0}
function addPage(parentId=null){const n=state.pages.length+1;const page={id:uid('page'),name:parentId?`子页面 ${n}`:`页面 ${n}`,path:`page-${n}`,parentId:parentId||null,kind:'page',elements:[]};state.pages.push(page);state.activePageId=page.id;editingElementId=null;renderPages();syncFields();renderCanvas();showToast(`已创建空白页面「${page.name}」`)}
function uniquePagePath(base,parentId){let path=base||'page',suffix=2;const used=value=>state.pages.some(page=>page.parentId===parentId&&page.path===value);while(used(path))path=`${base||'page'}-${suffix++}`;return path}
function duplicatePage(id){
  const source=state.pages.find(page=>page.id===id);if(!source)return;const subtree=[];const collect=pageId=>{const page=state.pages.find(item=>item.id===pageId);if(!page)return;subtree.push(page);state.pages.filter(item=>item.parentId===pageId).forEach(child=>collect(child.id))};collect(id);
  const pageMap=new Map(subtree.map(page=>[page.id,uid('page')]));const clones=subtree.map(page=>{const cloned=JSON.parse(JSON.stringify(page));cloned.id=pageMap.get(page.id);cloned.parentId=page.id===id?page.parentId:pageMap.get(page.parentId);if(page.id===id){cloned.name=`${page.name} 副本`;cloned.path=uniquePagePath(`${page.path}-copy`,cloned.parentId)}cloned.elements=(cloned.elements||[]).map(element=>{element.id=uid('el');if(Array.isArray(element.settings?.pageIds))element.settings.pageIds=element.settings.pageIds.map(pageId=>pageMap.get(pageId)||pageId);if(Array.isArray(element.items))element.items=element.items.map(item=>({...item,id:uid('item'),pageId:pageMap.get(item.pageId)||item.pageId}));return element});return cloned});
  state.pages.push(...clones);state.activePageId=pageMap.get(id);editingElementId=null;editingItemId=null;renderPages();syncFields();renderCanvas();showToast(`已复制「${source.name}」及其子页面`)
}
function deletePage(id){if(id===state.pages[0].id){showToast('首页不能删除');return}removePageCascade(id);if(!state.pages.some(page=>page.id===state.activePageId))state.activePageId=state.pages[0].id;editingElementId=null;renderPages();syncFields();renderCanvas();showToast('页面及其子页面已删除')}
function addElement(type,index=activePage().elements.length){const page=activePage();let accountAdded=false;if(type==='forum'&&!page.elements.some(item=>item.type==='account')){page.elements.splice(index,0,createElement('account',page));index+=1;accountAdded=true}const element=createElement(type,page);page.elements.splice(index,0,element);renderPages();renderCanvas();showToast(accountAdded?'已添加「账号登录」和「论坛板块」；论坛必须登录后互动':`已添加「${elementCatalog[type].name}」`)}

function clearDropHints(){document.querySelectorAll('.insert-zone').forEach(zone=>zone.classList.remove('active'));document.querySelector('[data-empty-drop]')?.classList.remove('drag-active')}
function findDropIndex(clientY){const elements=[...document.querySelectorAll('.page-element')];for(let index=0;index<elements.length;index++){const rect=elements[index].getBoundingClientRect();if(clientY<rect.top+rect.height/2)return index}return elements.length}
function showDropIndex(index){clearDropHints();const zone=document.querySelector(`[data-insert-index="${index}"]`);if(zone)zone.classList.add('active');else document.querySelector('[data-empty-drop]')?.classList.add('drag-active')}
function handleDrop(index){if(!dragPayload)return;const page=activePage();if(dragPayload.source==='palette')addElement(dragPayload.type,index);else{const oldIndex=page.elements.findIndex(item=>item.id===dragPayload.id);if(oldIndex>=0){const [moved]=page.elements.splice(oldIndex,1);const target=oldIndex<index?index-1:index;page.elements.splice(Math.max(0,target),0,moved);renderCanvas()}}dragPayload=null;document.body.classList.remove('is-dragging');clearDropHints()}

function getElementById(id){return activePage().elements.find(item=>item.id===id)}
function syncItemDetail(item){const page=state.pages.find(entry=>entry.id===item.pageId);if(!page)return;page.name=item.title||'未命名详情';const detail=page.elements.find(element=>element.type==='detail');if(detail){detail.settings.title=item.title;detail.settings.description=item.summary||item.meta;detail.settings.image=item.image||''}renderPages()}
function addCollectionItem(element){const item=makeCollectionItem(element.type,element.items.length);element.items.push(item);createDetailPage(activePage().id,element.type==='projects'?'project':'article',item);editingItemId=item.id;renderPages();renderCanvas()}
function deleteCollectionItem(element,itemId){const index=element.items.findIndex(item=>item.id===itemId);if(index<0)return;const [item]=element.items.splice(index,1);if(item.pageId)removePageCascade(item.pageId);editingItemId=null;renderPages();renderCanvas();showToast('条目和对应详情页已删除')}
function openElementEditor(id){const element=getElementById(id);if(element?.type==='forum'){showToast('论坛是动态页面，内容由真实帖子与回复生成');return}editingElementId=editingElementId===id?null:id;editingItemId=null;renderCanvas();if(editingElementId){document.querySelector(`[data-element-id="${id}"]`)?.scrollIntoView({behavior:'smooth',block:'start'});showToast('已进入直接编辑：点击虚线文字即可输入')}else showToast('已完成当前模块编辑')}
function deleteElement(id){const page=activePage();const element=page.elements.find(item=>item.id===id);if(element?.type==='account'&&page.elements.some(item=>item.type==='forum')){showToast('当前页面有论坛，不能单独删除账号登录模块');return}if(element)cleanupElementDetails([element]);page.elements=page.elements.filter(item=>item.id!==id);editingElementId=null;renderPages();renderCanvas()}
function moveElementUp(id){const page=activePage();const index=page.elements.findIndex(item=>item.id===id);if(index>0)[page.elements[index-1],page.elements[index]]=[page.elements[index],page.elements[index-1]];else if(index===0&&page.elements.length>1)page.elements.push(page.elements.shift());renderCanvas()}

function directText(node){return String(node.innerText??node.textContent??'').replace(/\r/g,'').replace(/\u00a0/g,' ')}
function updatePipeSetting(element,field,index,value,defaults=''){
  const values=String(element.settings?.[field]??defaults).split('|');while(values.length<=index)values.push('');values[index]=value;element.settings={...(element.settings||{}),[field]:values.join('|')};
}
function syncDetailOwner(field,value){
  const page=activePage();if(page.kind!=='detail')return;state.pages.forEach(owner=>owner.elements.forEach(element=>(element.items||[]).forEach(item=>{if(item.pageId!==page.id)return;if(field==='title')item.title=value;if(field==='description')item.summary=value;if(field==='image')item.image=value})));renderPages();
}

function bindCanvasEvents(){
  const content=document.querySelector('#canvasContent');content.addEventListener('dragover',event=>{event.preventDefault();showDropIndex(findDropIndex(event.clientY));if(event.dataTransfer)event.dataTransfer.dropEffect=dragPayload?.source==='canvas'?'move':'copy'});content.addEventListener('drop',event=>{event.preventDefault();handleDrop(findDropIndex(event.clientY))});content.addEventListener('dragleave',event=>{if(!content.contains(event.relatedTarget))clearDropHints()});
  document.querySelectorAll('.page-element').forEach(block=>{
    block.addEventListener('dragstart',event=>{if(!event.target.closest('.drag-handle')){event.preventDefault();return}dragPayload={source:'canvas',id:block.dataset.elementId};block.classList.add('dragging');document.body.classList.add('is-dragging');event.dataTransfer.setData('text/plain',block.dataset.elementId);event.dataTransfer.effectAllowed='move'});
    block.addEventListener('dragend',()=>{block.classList.remove('dragging');dragPayload=null;document.body.classList.remove('is-dragging');clearDropHints()});block.querySelector('.edit-block')?.addEventListener('click',()=>openElementEditor(block.dataset.elementId));block.querySelector('.delete-block')?.addEventListener('click',()=>deleteElement(block.dataset.elementId));block.addEventListener('contextmenu',event=>{if(event.target.closest('input,textarea,label,[contenteditable]'))return;event.preventDefault();showElementContext(event.clientX,event.clientY,block.dataset.elementId)});
  });
  document.querySelectorAll('[data-direct-setting]').forEach(node=>{
    node.addEventListener('click',event=>event.stopPropagation());
    node.addEventListener('input',()=>{const element=getElementById(editingElementId);if(!element)return;const field=node.dataset.directSetting;const value=directText(node);element.settings={...(element.settings||{}),[field]:value};if(element.type==='detail'){if(field==='title'){activePage().name=value||'未命名详情';document.querySelector('#pageName').value=activePage().name;document.querySelector('#titlePreview').textContent=`${activePage().name}｜${state.siteName}`}syncDetailOwner(field,value)}scheduleDraftSave()});
    node.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();node.blur()}else if(event.key==='Enter'&&node.dataset.directMultiline!=='true'&&!event.shiftKey){event.preventDefault();node.blur()}});
    node.addEventListener('paste',event=>{event.preventDefault();document.execCommand('insertText',false,event.clipboardData?.getData('text/plain')||'')});
  });
  document.querySelectorAll('[data-direct-list-setting]').forEach(node=>{node.addEventListener('click',event=>event.stopPropagation());node.addEventListener('input',()=>{const element=getElementById(editingElementId);if(!element)return;updatePipeSetting(element,node.dataset.directListSetting,Number(node.dataset.directIndex),directText(node),node.dataset.directDefaults||'');scheduleDraftSave()});node.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();node.blur()}});node.addEventListener('paste',event=>{event.preventDefault();document.execCommand('insertText',false,event.clipboardData?.getData('text/plain')||'')})});
  document.querySelectorAll('[data-direct-item-id]').forEach(node=>{node.addEventListener('click',event=>event.stopPropagation());node.addEventListener('input',()=>{const element=getElementById(editingElementId);const item=element?.items?.find(entry=>entry.id===node.dataset.directItemId);if(!item)return;item[node.dataset.directItemField]=directText(node);syncItemDetail(item);scheduleDraftSave()});node.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();node.blur()}});node.addEventListener('paste',event=>{event.preventDefault();document.execCommand('insertText',false,event.clipboardData?.getData('text/plain')||'')})});
  document.querySelectorAll('[data-direct-page-name]').forEach(node=>{node.addEventListener('click',event=>event.stopPropagation());node.addEventListener('input',()=>{const page=state.pages.find(item=>item.id===node.dataset.directPageName);if(!page)return;page.name=directText(node)||'未命名页面';if(page.id===state.activePageId){document.querySelector('#pageName').value=page.name;document.querySelector('#titlePreview').textContent=`${page.name}｜${state.siteName}`}renderPages();scheduleDraftSave()});node.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();node.blur()}})});
  document.querySelectorAll('[data-inline-setting]').forEach(input=>{input.addEventListener('input',()=>{const element=getElementById(editingElementId);if(!element)return;element.settings={...(element.settings||{}),[input.dataset.inlineSetting]:input.value};if(input.dataset.inlineSetting==='titleSize')document.querySelector(`[data-element-id="${element.id}"] .b-hero`)?.style.setProperty('--hero-title-size',cssSize(input.value,'clamp(48px,7vw,102px)'));if(input.dataset.inlineSetting==='background')document.querySelector(`[data-element-id="${element.id}"] .notice-block`)?.style.setProperty('--notice-bg',cssColor(input.value,'var(--page-accent)'));if(input.dataset.inlineSetting==='color')document.querySelector(`[data-element-id="${element.id}"] .notice-block`)?.style.setProperty('--notice-fg',cssColor(input.value,'#141510'));syncDetailOwner(input.dataset.inlineSetting,input.value);scheduleDraftSave()});if(input.dataset.inlineSetting==='image')input.addEventListener('change',renderCanvas)});
  document.querySelectorAll('[data-pipe-setting]').forEach(input=>input.addEventListener('input',()=>{const element=getElementById(editingElementId);if(!element)return;updatePipeSetting(element,input.dataset.pipeSetting,Number(input.dataset.pipeIndex),input.value);scheduleDraftSave()}));
  document.querySelectorAll('[data-nav-page]').forEach(input=>input.addEventListener('change',()=>{const element=getElementById(editingElementId);if(!element)return;const selected=[...document.querySelectorAll('[data-nav-page]:checked')].map(item=>item.dataset.navPage);element.settings={...(element.settings||{}),pageIds:selected};renderCanvas()}));
  document.querySelectorAll('[data-item-field]').forEach(input=>input.addEventListener('input',()=>{const element=getElementById(editingElementId);const item=element?.items?.find(entry=>entry.id===editingItemId);if(!item)return;item[input.dataset.itemField]=input.value;syncItemDetail(item)}));
  document.querySelector('[data-item-upload]')?.addEventListener('change',event=>{const file=event.target.files?.[0];const element=getElementById(editingElementId);const item=element?.items?.find(entry=>entry.id===editingItemId);if(!file||!item)return;if(file.size>8*1024*1024){showToast('图片请控制在 8 MB 以内');return}const reader=new FileReader();reader.onload=()=>{item.image=String(reader.result);syncItemDetail(item);renderCanvas()};reader.readAsDataURL(file)});
  document.querySelectorAll('[data-editor-action]').forEach(button=>button.addEventListener('click',event=>{event.stopPropagation();const element=getElementById(editingElementId);if(button.dataset.editorAction==='done')openElementEditor(editingElementId);else if(button.dataset.editorAction==='add-item'&&element)addCollectionItem(element);else if(button.dataset.editorAction==='close-item'){editingItemId=null;renderCanvas()}else if(button.dataset.editorAction==='delete-item'&&element)deleteCollectionItem(element,editingItemId)}));
}

function showToast(message){const toast=document.querySelector('#toast');toast.querySelector('span').textContent=message;toast.hidden=false;clearTimeout(showToast.timer);showToast.timer=setTimeout(()=>toast.hidden=true,2600)}
function openModal(title,html){document.querySelector('#modalBody').innerHTML=`<h2 id="modalTitle">${esc(title)}</h2>${html}`;document.querySelector('#previewModal').hidden=false}
function closeModal(){document.querySelector('#previewModal').hidden=true;document.querySelector('#modalBody').innerHTML=''}
function showElementContext(x,y,id){contextElementId=id;const menu=document.querySelector('#elementContextMenu');menu.hidden=false;menu.style.left=`${Math.min(x,window.innerWidth-155)}px`;menu.style.top=`${Math.min(y,window.innerHeight-135)}px`}
function hideElementContext(){document.querySelector('#elementContextMenu').hidden=true;contextElementId=null}
function openThread(post){openModal(post.title,`<p>${esc(post.author)} · 刚刚</p><p>${esc(post.body)}</p><div class="reply-list">${post.replies.length?post.replies.map(reply=>`<div class="reply"><b>${esc(reply.author)}</b><p>${esc(reply.body)}</p></div>`).join(''):'<div class="reply"><p>还没有回复，来坐第一排。</p></div>'}</div><form class="runtime-form" data-runtime-form="reply" data-post-id="${post.id}"><input name="author" placeholder="你的昵称" required><textarea name="body" rows="3" placeholder="写下回复……" required></textarea><button>发布回复</button></form>`)}

document.querySelector('#siteCanvas').addEventListener('click',event=>{
  if(event.target.closest('[contenteditable]'))return;
  const target=event.target.closest('[data-preview-action]');if(!target)return;const action=target.dataset.previewAction;
  const targetBlock=target.closest('.page-element');if(editingElementId===targetBlock?.dataset.elementId&&!['project','article'].includes(action))return;
  if(action==='navigate'){selectPage(target.dataset.pageId);return}
  if(action==='project'||action==='article'){const block=target.closest('.page-element');if(editingElementId===block?.dataset.elementId){editingItemId=target.dataset.itemId;renderCanvas()}else if(target.dataset.pageId)selectPage(target.dataset.pageId);return}
  if(action==='next-section'){const current=target.closest('.page-element');const next=current?.nextElementSibling?.nextElementSibling;next?.scrollIntoView({behavior:'smooth',block:'start'});if(!next)showToast('继续从左侧添加下一个页面元素');return}
  if(action==='gallery'){openModal(`活动照片 0${target.dataset.index}`,`<div style="height:280px;display:grid;place-items:center;background:#e7e8e1;font-size:60px">0${target.dataset.index}</div><p>图片大图预览。后续可接入真实上传服务。</p>`);return}
  if(action==='member'){openModal(target.dataset.title,`<p>${esc(target.dataset.role)} · 社团成员</p><p>点击成员卡片可以查看简介、负责项目和联系方式。</p>`);return}
  if(action==='register'){const name=target.dataset.event;if(previewDB.registrations.has(name)){previewDB.registrations.delete(name);showToast(`已取消「${name}」报名`)}else{previewDB.registrations.add(name);showToast(`已报名「${name}」`)}renderCanvas();return}
  if(action==='forum-compose'){openModal('发布新话题',`<p>帖子将写入当前预览的临时数据，刷新页面后自动清除。</p><form class="runtime-form" data-runtime-form="topic"><input name="author" placeholder="你的昵称" required><input name="title" placeholder="话题标题" required><textarea name="body" rows="4" placeholder="想和大家聊什么？" required></textarea><button>发布话题</button></form>`);return}
  if(action==='forum-thread'){const post=previewDB.forumPosts.find(item=>item.id===target.dataset.postId);if(post)openThread(post);return}
  if(action==='account-login'){openModal('账号登录',`<p>预览账号和密码均固定为 admin。</p><form class="runtime-form" data-runtime-form="login"><label>账号<input name="username" value="admin" readonly></label><label>密码<input name="password" type="password" placeholder="请输入 admin" required></label><button>登录</button></form>`);return}
  if(action==='account-logout'){previewDB.accountLoggedIn=false;renderCanvas();showToast('admin 已退出');return}
  if(action==='notice'){openModal('秋季招新开始啦',`<p>9 月 5 日 14:00—18:00，大学生活动中心一楼。带上你的好奇心就可以来，零基础完全没问题。</p>`);return}
  if(action==='link'){showToast(`预览：${target.dataset.title}`);return}
  if(action==='join'){previewDB.joined=!previewDB.joined;renderCanvas();showToast(previewDB.joined?'欢迎加入，已记录在临时数据中':'已取消加入')}
});

document.querySelector('#previewModal').addEventListener('click',event=>{if(event.target.id==='previewModal')closeModal()});
document.querySelector('#previewModal').addEventListener('submit',event=>{const form=event.target.closest('[data-runtime-form]');if(!form)return;event.preventDefault();const data=new FormData(form);if(form.dataset.runtimeForm==='login'){if(String(data.get('password'))!=='admin'){showToast('账号或密码错误：请使用 admin / admin');return}previewDB.accountLoggedIn=true;closeModal();renderCanvas();showToast('已以 admin 身份登录')}else if(form.dataset.runtimeForm==='topic'){previewDB.forumPosts.unshift({id:`post-${Date.now()}`,title:String(data.get('title')),author:String(data.get('author')),body:String(data.get('body')),replies:[]});closeModal();renderCanvas();showToast('话题已发布到临时论坛')}else if(form.dataset.runtimeForm==='reply'){const post=previewDB.forumPosts.find(item=>item.id===form.dataset.postId);if(post)post.replies.push({author:String(data.get('author')),body:String(data.get('body'))});closeModal();renderCanvas();showToast('回复已发布')}});
document.querySelector('#closeModalBtn').addEventListener('click',closeModal);

document.querySelectorAll('.sidebar-tabs button').forEach(button=>button.addEventListener('click',()=>{document.querySelectorAll('.sidebar-tabs button').forEach(item=>{item.classList.toggle('active',item===button);item.setAttribute('aria-selected',String(item===button))});document.querySelectorAll('[data-panel-content]').forEach(panel=>{const active=panel.dataset.panelContent===button.dataset.panel;panel.hidden=!active;panel.classList.toggle('active',active)})}));
document.querySelectorAll('.element-card').forEach(card=>{card.addEventListener('dragstart',event=>{dragPayload={source:'palette',type:card.dataset.element};card.classList.add('dragging');document.body.classList.add('is-dragging');event.dataTransfer.setData('text/plain',card.dataset.element);event.dataTransfer.effectAllowed='copy'});card.addEventListener('dragend',()=>{card.classList.remove('dragging');dragPayload=null;document.body.classList.remove('is-dragging');clearDropHints()});card.addEventListener('click',()=>addElement(card.dataset.element))});
document.querySelectorAll('[data-theme]').forEach(button=>button.addEventListener('click',()=>{state.theme=button.dataset.theme;document.querySelectorAll('[data-theme]').forEach(item=>{const active=item===button;item.classList.toggle('active',active);item.setAttribute('aria-checked',String(active))});renderCanvas()}));
document.querySelectorAll('[data-bg]').forEach(button=>button.addEventListener('click',()=>{state.background=button.dataset.bg;document.querySelectorAll('[data-bg]').forEach(item=>item.classList.toggle('active',item===button));renderCanvas()}));
document.querySelector('#contentWidth').addEventListener('input',event=>{state.contentWidth=Number(event.target.value);document.querySelector('#widthValue').textContent=`${state.contentWidth}%`;renderCanvas()});document.querySelector('#sectionGap').addEventListener('change',event=>{state.sectionGap=event.target.checked;renderCanvas()});
document.querySelector('#siteName').addEventListener('input',event=>{state.siteName=event.target.value;document.querySelector('#titlePreview').textContent=`${activePage().name}｜${state.siteName}`;renderCanvas()});document.querySelector('#siteDescription').addEventListener('input',event=>{state.description=event.target.value;renderCanvas()});document.querySelector('#pageName').addEventListener('input',event=>{activePage().name=event.target.value||'未命名页面';renderPages();document.querySelector('#titlePreview').textContent=`${activePage().name}｜${state.siteName}`;renderCanvas()});document.querySelector('#pagePath').addEventListener('input',event=>{activePage().path=event.target.value.replace(/[^a-zA-Z0-9-_]/g,'').toLowerCase()||'page';renderPages();scheduleDraftSave()});
document.querySelector('#addPageBtn').addEventListener('click',()=>addPage(null));document.querySelector('#clearPageBtn').addEventListener('click',()=>{if(!activePage().elements.length){showToast('当前页面已经是空白的');return}cleanupElementDetails(activePage().elements);activePage().elements=[];editingElementId=null;renderPages();renderCanvas();showToast('当前页面已清空')});document.querySelector('#toast button').addEventListener('click',()=>document.querySelector('#toast').hidden=true);
document.querySelectorAll('.ai-examples button').forEach(button=>button.addEventListener('click',()=>{document.querySelector('#aiPrompt').value=button.textContent}));
document.querySelector('#undoBtn').addEventListener('click',undoState);document.querySelector('#redoBtn').addEventListener('click',redoState);
document.querySelectorAll('[data-preview-device]').forEach(button=>button.addEventListener('click',()=>{previewDevice=button.dataset.previewDevice;sessionStorage.setItem('alchemysites:preview-device',previewDevice);renderCanvas();showToast(`已切换到${button.textContent}预览`)}));
function setPreviewZoom(value){previewZoom=Math.max(50,Math.min(125,value));sessionStorage.setItem('alchemysites:preview-zoom',String(previewZoom));renderCanvas()}
document.querySelector('#zoomOutBtn').addEventListener('click',()=>setPreviewZoom(previewZoom-10));document.querySelector('#zoomInBtn').addEventListener('click',()=>setPreviewZoom(previewZoom+10));
document.addEventListener('keydown',event=>{const editingText=event.target.closest?.('input,textarea,[contenteditable]');if(editingText)return;if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='z'){event.preventDefault();if(event.shiftKey)redoState();else undoState()}else if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='y'){event.preventDefault();redoState()}else if(event.key==='Escape'&&editingElementId)openElementEditor(editingElementId)});

let pendingAiProposal=null;
let lastAiUndo=null;
const cloneJson=value=>JSON.parse(JSON.stringify(value));
function aiSafeSnapshot(){
  const snapshot=cloneJson(state);
  snapshot.pages.forEach(page=>page.elements.forEach(element=>{(element.items||[]).forEach(item=>{if(String(item.image||'').startsWith('data:'))item.image='[本地图片数据已省略，但必须保留原值]' });if(String(element.settings?.image||'').startsWith('data:'))element.settings.image='[本地图片数据已省略，但必须保留原值]'}));
  return {site:snapshot,activePageId:state.activePageId,elementTypes:Object.keys(elementCatalog),rules:['未明确要求时保留全部现有内容与 ID','预览交互数据刷新后清空','发布站点使用服务端持久化']};
}
function syncEditorAfterAI(){
  if(!state.pages.some(page=>page.id===state.activePageId))state.activePageId=state.pages[0]?.id;
  editingElementId=null;editingItemId=null;renderPages();syncFields();renderCanvas();
  document.querySelectorAll('[data-theme]').forEach(item=>{const active=item.dataset.theme===state.theme;item.classList.toggle('active',active);item.setAttribute('aria-checked',String(active))});
  document.querySelectorAll('[data-bg]').forEach(item=>item.classList.toggle('active',item.dataset.bg===state.background));
  document.querySelector('#contentWidth').value=state.contentWidth;document.querySelector('#widthValue').textContent=`${state.contentWidth}%`;document.querySelector('#sectionGap').checked=state.sectionGap;
}
function restoreState(snapshot){Object.keys(state).forEach(key=>delete state[key]);Object.assign(state,cloneJson(snapshot));syncEditorAfterAI()}
function applyAiSiteOperations(operations){
  const backup=cloneJson(state);const allowedSiteFields=new Set(['siteName','description','theme','background','contentWidth','sectionGap']);const createdPages=new Map();const createdElements=new Map();const resolvePage=id=>state.pages.find(item=>item.id===(createdPages.get(id)||id));const resolveElement=(page,id)=>page?.elements.find(item=>item.id===(createdElements.get(id)||id));
  try{for(const operation of operations||[]){const op=operation.op;
    if(op==='set_site'){
      if(!allowedSiteFields.has(operation.field))throw new Error(`不支持修改站点字段 ${operation.field}`);let value=operation.value;
      if(operation.field==='theme'&&!['minimal','editorial','terminal','soft'].includes(value))throw new Error('AI 返回了无效主题');
      if(operation.field==='background'&&!/^#[0-9a-f]{6}$/i.test(String(value)))throw new Error('AI 返回了无效背景色');
      if(operation.field==='contentWidth')value=Math.max(70,Math.min(100,Number(value)||100));if(operation.field==='sectionGap')value=Boolean(value);state[operation.field]=value;
    }else if(op==='set_page'){
      const page=resolvePage(operation.pageId);if(!page||!['name','path'].includes(operation.field))throw new Error('AI 指定的页面或字段不存在');page[operation.field]=operation.field==='path'?String(operation.value).replace(/[^a-zA-Z0-9-_]/g,'').toLowerCase()||'page':String(operation.value)||'未命名页面';
    }else if(op==='add_page'){
      const parent=operation.parentId?resolvePage(operation.parentId):null;if(operation.parentId&&!parent)throw new Error('AI 指定的父页面不存在');const newPage={id:uid('page'),name:String(operation.name||'新页面'),path:String(operation.path||'new-page').replace(/[^a-zA-Z0-9-_]/g,'').toLowerCase()||'new-page',parentId:parent?.id||null,kind:'page',elements:[]};state.pages.push(newPage);if(operation.tempId)createdPages.set(operation.tempId,newPage.id);
    }else if(op==='remove_page'){
      const page=resolvePage(operation.pageId);if(!page)throw new Error('AI 指定的页面不存在');if(page.id===state.pages[0].id)throw new Error('AI 不能删除首页');removePageCascade(page.id);
    }else if(op==='add_element'){
      const page=resolvePage(operation.pageId);if(!page||!elementCatalog[operation.type])throw new Error('AI 指定的页面或元素类型不存在');const element=createElement(operation.type,page);if(operation.settings&&typeof operation.settings==='object')element.settings=cloneJson(operation.settings);const index=Math.max(0,Math.min(page.elements.length,Number(operation.index)));page.elements.splice(Number.isFinite(index)?index:page.elements.length,0,element);if(operation.tempId)createdElements.set(operation.tempId,element.id);
    }else if(op==='update_element'){
      const page=resolvePage(operation.pageId);const element=resolveElement(page,operation.elementId);if(!element||!operation.settings||typeof operation.settings!=='object')throw new Error('AI 指定的元素不存在或设置无效');element.settings={...(element.settings||{}),...cloneJson(operation.settings)};
    }else if(op==='remove_element'){
      const page=resolvePage(operation.pageId);const resolvedId=createdElements.get(operation.elementId)||operation.elementId;const index=page?.elements.findIndex(item=>item.id===resolvedId)??-1;if(!page||index<0)throw new Error('AI 指定的元素不存在');cleanupElementDetails([page.elements[index]]);page.elements.splice(index,1);
    }else if(op==='move_element'){
      const page=resolvePage(operation.pageId);const resolvedId=createdElements.get(operation.elementId)||operation.elementId;const from=page?.elements.findIndex(item=>item.id===resolvedId)??-1;if(!page||from<0)throw new Error('AI 指定的元素不存在');const [element]=page.elements.splice(from,1);const to=Math.max(0,Math.min(page.elements.length,Number(operation.index)||0));page.elements.splice(to,0,element);
    }else if(op==='set_items'){
      const page=resolvePage(operation.pageId);const element=resolveElement(page,operation.elementId);if(!page||!element||!['projects','blog'].includes(element.type)||!Array.isArray(operation.items))throw new Error('AI 指定的作品或文章列表无效');const previous=new Map((element.items||[]).map(item=>[item.id,item]));const incoming=operation.items.slice(0,30).map((raw,index)=>{const old=previous.get(raw.id);const item={id:old?.id||uid('item'),title:String(raw.title||`条目 ${index+1}`),meta:String(raw.meta||''),summary:String(raw.summary||''),image:old&&raw.image==='[本地图片数据已省略，但必须保留原值]'?old.image:String(raw.image||''),pageId:old?.pageId||null};previous.delete(item.id);if(!item.pageId)createDetailPage(page.id,element.type==='projects'?'project':'article',item);return item});previous.forEach(item=>{if(item.pageId)removePageCascade(item.pageId)});element.items=incoming;element.items.forEach(syncItemDetail);
    }else throw new Error(`不支持的 AI 操作 ${op}`);
  }ensureForumAccounts()}catch(error){restoreState(backup);throw error}syncEditorAfterAI();return backup;
}
function describeAiOperation(operation){const page=state.pages.find(item=>item.id===operation.pageId);const element=page?.elements.find(item=>item.id===operation.elementId);const names={set_site:'修改站点设置',set_page:'修改页面',add_page:'新增页面',remove_page:'删除页面',add_element:'新增元素',update_element:'编辑元素',remove_element:'删除元素',move_element:'移动元素',set_items:'更新作品/文章'};return `${names[operation.op]||operation.op}${page?` · ${page.name}`:''}${element?` · ${elementCatalog[element.type]?.name||element.type}`:''}`}
function showAiError(message){const result=document.querySelector('#aiResult');result.textContent=message;result.classList.add('error');result.hidden=false}
function renderAiProposal(proposal){
  pendingAiProposal=proposal;document.querySelector('#aiResult').hidden=true;const panel=document.querySelector('#aiProposal');panel.hidden=false;document.querySelector('#aiProposalSummary').textContent=proposal.summary||'Kimi 已生成修改方案。';document.querySelector('#aiProposalTitle').textContent=`${(proposal.siteOperations||[]).length} 项站点修改 · ${(proposal.sourceChanges||[]).length} 项源码修改`;
  const risk=document.querySelector('#aiRiskBadge');risk.textContent=String(proposal.risk||'medium').toUpperCase();risk.className=proposal.risk||'medium';
  const changes=[...(proposal.siteOperations||[]).map(item=>describeAiOperation(item)),...(proposal.assumptions||[]).map(item=>`假设：${item}`),...(proposal.checks||[]).map(item=>`检查：${item}`)];document.querySelector('#aiChangeList').innerHTML=changes.length?changes.map(item=>`<div class="ai-change">${esc(item)}</div>`).join(''):'<div class="ai-change">未修改站点结构或内容</div>';
  document.querySelector('#aiDiffList').innerHTML=(proposal.sourceChanges||[]).map(change=>`<details><summary>${esc(change.path)} · ${esc(change.reason||'修改源码')}</summary><pre>－ ${esc(String(change.search).slice(0,1800))}\n＋ ${esc(String(change.replace).slice(0,1800))}${String(change.search).length>1800||String(change.replace).length>1800?'\n…差异过长，已截断预览':''}</pre></details>`).join('');
}
async function loadAiStatus(){try{const response=await fetch('/api/ai/status');const status=await response.json();const badge=document.querySelector('#aiStatusBadge');badge.textContent=status.configured?'可用':'未配置';badge.className=status.configured?'ready':'error';document.querySelector('#aiModelLabel').textContent=`${status.model||'Kimi'} · 可编辑 ${(status.editableFiles||[]).length} 个源码文件`;document.querySelector('#aiAdjustBtn').disabled=!status.configured}catch{document.querySelector('#aiStatusBadge').textContent='服务异常';document.querySelector('#aiStatusBadge').className='error'}}
function saveDraftNow(){clearTimeout(draftSaveTimer);if(!currentConsoleUser)return;try{const snapshot=JSON.stringify(state);void persistDraftSnapshot(snapshot).catch(()=>setSaveState('同步失败',false))}catch{}}
function persistAiUndo(){try{if(lastAiUndo)sessionStorage.setItem(AI_UNDO_KEY,JSON.stringify(lastAiUndo));else sessionStorage.removeItem(AI_UNDO_KEY)}catch{}}
function notifyPublishedReload(reason){try{const channel=new BroadcastChannel('alchemysites-live-preview');channel.postMessage({type:'reload',reason,at:Date.now()});channel.close()}catch{}try{localStorage.setItem('alchemysites:published-reload',JSON.stringify({reason,at:Date.now()}))}catch{}}
function refreshAiChangedFiles(files=[]){
  if(files.includes('styles.css'))document.querySelectorAll('link[rel="stylesheet"]').forEach(link=>{const url=new URL(link.href,location.href);if(url.pathname.endsWith('/styles.css')){url.searchParams.set('ai',String(Date.now()));link.href=url.toString()}});
  const viewerReloaded=files.some(name=>['viewer.html','viewer.js'].includes(name));if(viewerReloaded)notifyPublishedReload('AI 已更新发布站点运行代码');
  return {editorReloadNeeded:files.some(name=>['index.html','script.js'].includes(name)),viewerReloaded};
}
async function restartLocalServer(){
  const response=await fetch('/api/server/restart',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});const payload=await response.json();if(!response.ok)throw new Error(payload.error||'本地服务重启失败');
  await new Promise(resolve=>setTimeout(resolve,700));
  for(let attempt=0;attempt<60;attempt++){try{const check=await fetch(`/api/ai/status?restart=${Date.now()}`,{cache:'no-store'});if(check.ok)return}catch{}await new Promise(resolve=>setTimeout(resolve,250))}
  throw new Error('本地服务重启后未能重新连接')
}
function scheduleEditorReload(message){saveDraftNow();try{sessionStorage.setItem(AI_RELOAD_NOTICE_KEY,message)}catch{}setTimeout(()=>location.reload(),420)}
function restoreAiReloadUi(){
  let notice='';try{notice=sessionStorage.getItem(AI_RELOAD_NOTICE_KEY)||'';sessionStorage.removeItem(AI_RELOAD_NOTICE_KEY)}catch{}
  if(notice){const result=document.querySelector('#aiResult');result.classList.remove('error');result.textContent=notice;result.hidden=false;showToast('AI 修改已实时加载')}
  if(lastAiUndo)document.querySelector('#aiUndoBtn').hidden=false;
}
async function consoleRequest(path,options={}){
  if(window.AIchemySitesAuth)return window.AIchemySitesAuth.request(path,options);
  const request={method:options.method||'GET',headers:{'Accept':'application/json'}};
  if(options.body!==undefined){request.headers['Content-Type']='application/json';request.body=JSON.stringify(options.body)}
  const response=await fetch(path,request);let payload={};try{payload=await response.json()}catch{payload={error:'服务返回了无法解析的内容'}}
  if(!response.ok){const error=new Error(payload.error||`请求失败（${response.status}）`);error.status=response.status;throw error}return payload;
}
function showAuthGate(message=''){
  currentConsoleUser=null;if(window.AIchemySitesAuth){window.AIchemySitesAuth.showGate(message);return}const gate=document.querySelector('#authGate');gate.hidden=false;const error=document.querySelector('#authError');error.textContent=message;error.hidden=!message;setSaveState('等待登录',false);
}
function updateConsoleAccount(){
  if(!currentConsoleUser)return;const username=currentConsoleUser.username;const initial=username.slice(0,1).toUpperCase();
  document.querySelector('#consoleAvatar').textContent=initial;document.querySelector('#menuAvatar').textContent=initial;document.querySelector('#consoleUsername').textContent=username;document.querySelector('#menuUsername').textContent=username;document.querySelector('#accountUsername').textContent=username;document.querySelector('#accountPublishPath').textContent=`/${username}`;document.querySelector('#menuRole').textContent=currentConsoleUser.role==='admin'?'管理员':'用户';document.querySelector('#inviteManagerBtn').hidden=currentConsoleUser.role!=='admin';
}
async function enterConsole(user){
  currentConsoleUser=user;const username=user.username;DRAFT_KEY=`alchemysites:${username}:draft:v3`;AI_UNDO_KEY=`alchemysites:${username}:ai-undo:v2`;AI_RELOAD_NOTICE_KEY=`alchemysites:${username}:ai-reload-notice:v2`;
  let draft=null;try{draft=(await consoleRequest('/api/console/draft')).draft}catch(error){if(error.status===401){showAuthGate('登录已过期，请重新登录');return}showToast(`读取云端草稿失败：${error.message}`)}
  if(!draft){try{draft=JSON.parse(localStorage.getItem(DRAFT_KEY)||'null')}catch{}if(!draft){try{draft=JSON.parse(localStorage.getItem(`miaoda:${username}:draft:v3`)||'null')}catch{}}if(!draft&&username.toLowerCase()==='test'){try{draft=JSON.parse(localStorage.getItem('miaoda:test:draft:v2')||'null')}catch{}}}
  const next=draft?.pages?.length?draft:INITIAL_STATE;restoreState(next);ensureForumAccounts();syncEditorAfterAI();historyStack=[JSON.stringify(state)];historyIndex=0;updateHistoryButtons();pendingAiProposal=null;lastAiUndo=null;try{const savedUndo=JSON.parse(sessionStorage.getItem(AI_UNDO_KEY)||'null');if(savedUndo?.proposalId)lastAiUndo=savedUndo}catch{}
  updateConsoleAccount();document.querySelector('#authGate').hidden=true;syncFields();setSaveState(draft?'草稿已同步':'新草稿',false);restoreAiReloadUi();void loadAiStatus();if(!draft)scheduleDraftSave();
}
function sessionLabel(userAgent=''){
  const browser=/Edg/i.test(userAgent)?'Edge':/Chrome/i.test(userAgent)?'Chrome':/Firefox/i.test(userAgent)?'Firefox':/Safari/i.test(userAgent)?'Safari':'浏览器';const system=/Windows/i.test(userAgent)?'Windows':/Mac OS/i.test(userAgent)?'macOS':/Android/i.test(userAgent)?'Android':/iPhone|iPad/i.test(userAgent)?'iOS':'未知系统';return `${browser} · ${system}`;
}
async function openPasswordSettings(){
  try{const payload=await consoleRequest('/api/auth/sessions');const sessions=(payload.sessions||[]).map(item=>`<div class="security-session${item.current?' current':''}"><div><b>${item.current?'当前会话':sessionLabel(item.userAgent)}</b><small>${item.current?sessionLabel(item.userAgent):`最后活动 ${esc(String(item.lastSeenAt||item.createdAt||'').replace('T',' '))}`}</small></div><span>${item.current?'正在使用':'已登录'}</span></div>`).join('');openModal('账号安全',`<div class="account-security"><header><span class="console-avatar">${esc(currentConsoleUser?.username?.slice(0,1).toUpperCase()||'?')}</span><div><b>${esc(currentConsoleUser?.username||'')}</b><small>${currentConsoleUser?.role==='admin'?'管理员':'普通用户'} · 发布目录 /${esc(currentConsoleUser?.username||'')}</small></div></header><section><h3>修改密码</h3><p>修改密码后会注销其他所有设备，当前设备自动换发新会话。</p><form class="account-settings-form" data-console-form="password"><label>当前密码<input name="oldPassword" type="password" autocomplete="current-password" required></label><label>新密码<input name="newPassword" type="password" autocomplete="new-password" minlength="8" maxlength="128" required></label><label>确认新密码<input name="confirmPassword" type="password" autocomplete="new-password" minlength="8" maxlength="128" required></label><button>确认修改密码</button></form></section><section><div class="security-title"><div><h3>登录设备</h3><p>${(payload.sessions||[]).length} 个有效会话</p></div><button data-revoke-other-sessions>退出其他设备</button></div><div class="security-sessions">${sessions}</div></section></div>`)}catch(error){showToast(error.message)}
}
function inviteRows(invites){
  if(!invites.length)return '<div class="invite-empty">还没有邀请码，先生成一批。</div>';
  return invites.map(item=>{const detail=item.status==='used'?`已由 ${esc(item.usedBy||'用户')} 使用`:item.status==='revoked'?'已撤销':'尚未使用';const action=item.status==='available'?`<div><button data-invite-copy="${esc(item.code)}">复制</button> <button data-invite-revoke="${esc(item.code)}">撤销</button></div>`:`<span class="status">${item.status==='used'?'已使用':'已撤销'}</span>`;return `<div class="invite-row"><div><code>${esc(item.code)}</code><small>${detail} · ${esc(String(item.createdAt||'').replace('T',' '))}</small></div>${action}</div>`}).join('');
}
async function openInviteManager(){
  try{const [invitePayload,userPayload]=await Promise.all([consoleRequest('/api/admin/invites'),consoleRequest('/api/admin/users')]);const users=(userPayload.users||[]).map(item=>{const own=item.username.toLowerCase()===currentConsoleUser?.username?.toLowerCase();const status=item.status==='active'?'正常':'已停用';return `<div class="admin-user-row"><span class="console-avatar">${esc(item.username.slice(0,1).toUpperCase())}</span><div><b>${esc(item.username)}</b><small>${item.role==='admin'?'管理员':'用户'} · ${item.published?'已发布':'未发布'} · ${item.sessionCount} 个会话</small></div><em class="${item.status}">${status}</em>${own?'<i>当前账号</i>':`<button data-user-status="${item.status==='active'?'disabled':'active'}" data-user-name="${esc(item.username)}">${item.status==='active'?'停用':'启用'}</button>`}</div>`}).join('')||'<div class="invite-empty">暂无用户</div>';openModal('账号与注册管理',`<div class="admin-console"><section><header><div><small>USERS</small><h3>控制台用户</h3></div><b>${userPayload.total||0}</b></header><div class="admin-user-list">${users}</div></section><section class="invite-manager"><header><div><small>INVITATIONS</small><h3>注册邀请码</h3></div></header><p>随机 16 位 hex，每个只能注册一次。可生成、复制或撤销尚未使用的邀请码。</p><form class="invite-generator" data-console-form="invite-generate"><label>本次生成数量<input name="count" type="number" min="1" max="20" value="5" required></label><button>生成邀请码</button></form><div class="invite-list">${inviteRows(invitePayload.invites||[])}</div></section></div>`)}catch(error){showToast(error.message)}
}
async function logoutConsole(){
  try{await persistDraftSnapshot(JSON.stringify(state))}catch{}document.querySelector('#consoleAccountMenu').hidden=true;if(window.AIchemySitesAuth)await window.AIchemySitesAuth.logout();else showAuthGate();showToast('已退出控制台账号');
}
document.querySelector('#aiAdjustBtn').addEventListener('click',async()=>{const prompt=document.querySelector('#aiPrompt').value.trim();if(!prompt){showToast('先准确描述要修改的内容');return}const button=document.querySelector('#aiAdjustBtn');const result=document.querySelector('#aiResult');button.disabled=true;button.querySelector('b').textContent='Kimi 正在阅读项目…';result.hidden=true;result.classList.remove('error');document.querySelector('#aiProposal').hidden=true;try{const response=await fetch('/api/ai/propose',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt,context:aiSafeSnapshot(),includeSite:document.querySelector('#aiScopeSite').checked,includeSource:document.querySelector('#aiScopeCode').checked})});const payload=await response.json();if(!response.ok)throw new Error(payload.error||'AI 请求失败');renderAiProposal(payload.proposal||{})}catch(error){showAiError(`生成失败：${error.message}`)}finally{button.disabled=false;button.querySelector('b').textContent='生成变更方案'}});
document.querySelector('#aiCancelBtn').addEventListener('click',()=>{pendingAiProposal=null;document.querySelector('#aiProposal').hidden=true;showToast('已放弃这次 AI 方案')});
document.querySelector('#aiApplyBtn').addEventListener('click',async()=>{
  if(!pendingAiProposal)return;const button=document.querySelector('#aiApplyBtn');button.disabled=true;button.textContent='检查并应用中…';const proposal=pendingAiProposal;let sourceResult=null;let stateBackup=null;
  try{
    const response=await fetch('/api/ai/apply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({proposalId:proposal.id})});sourceResult=await response.json();if(!response.ok)throw new Error(sourceResult.error||'源码应用失败');
    stateBackup=applyAiSiteOperations(proposal.siteOperations||[]);const live=refreshAiChangedFiles(sourceResult.changedFiles||[]);lastAiUndo={proposalId:proposal.id,state:stateBackup,sourceApplied:Boolean(sourceResult.undoAvailable)};persistAiUndo();pendingAiProposal=null;document.querySelector('#aiProposal').hidden=true;saveDraftNow();
    const result=document.querySelector('#aiResult');result.classList.remove('error');result.textContent=`已实时应用：${(proposal.siteOperations||[]).length} 项站点修改，${(sourceResult.changedFiles||[]).length} 个源码文件。`;result.hidden=false;document.querySelector('#aiUndoBtn').hidden=false;
    if(sourceResult.restartRequired){button.textContent='正在重启本地服务…';await restartLocalServer()}
    if(live.editorReloadNeeded){scheduleEditorReload('AI 已修改编辑器源码，并自动保存草稿、刷新和恢复页面。')}else showToast(sourceResult.restartRequired?'AI 修改已应用，本地服务已自动重启':'AI 修改已实时应用');
  }catch(error){
    if(stateBackup)restoreState(stateBackup);if(sourceResult?.undoAvailable){try{await fetch('/api/ai/undo',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({proposalId:proposal.id})})}catch{}}
    lastAiUndo=null;persistAiUndo();showAiError(`应用失败，未保留不完整修改：${error.message}`)
  }finally{button.disabled=false;button.textContent='确认应用'}
});
document.querySelector('#aiUndoBtn').addEventListener('click',async()=>{
  if(!lastAiUndo)return;const undo=lastAiUndo;const button=document.querySelector('#aiUndoBtn');button.disabled=true;let sourceUndo={restoredFiles:[],restartRequired:false};
  try{
    if(undo.sourceApplied){const response=await fetch('/api/ai/undo',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({proposalId:undo.proposalId})});sourceUndo=await response.json();if(!response.ok)throw new Error(sourceUndo.error||'源码恢复失败')}
    restoreState(undo.state);saveDraftNow();const live=refreshAiChangedFiles(sourceUndo.restoredFiles||[]);lastAiUndo=null;persistAiUndo();button.hidden=true;
    if(sourceUndo.restartRequired)await restartLocalServer();const result=document.querySelector('#aiResult');result.classList.remove('error');result.textContent='已撤销上一次 AI 修改。';result.hidden=false;
    if(live.editorReloadNeeded)scheduleEditorReload('已撤销 AI 修改，并自动刷新恢复编辑器。');else showToast('已恢复 AI 修改前的状态')
  }catch(error){showAiError(`撤销失败：${error.message}`)}finally{button.disabled=false}
});
function buildPublishPayload(){
  ensureForumAccounts();
  const previousPage=state.activePageId,previousEditing=editingElementId,previousAccountState=previewDB.accountLoggedIn;editingElementId=null;previewDB.accountLoggedIn=false;
  const pages=state.pages.map(page=>{state.activePageId=page.id;return {id:page.id,name:page.name,path:pageFullPath(page),parentId:page.parentId,kind:page.kind,html:page.elements.map(element=>blockContent(element.type,element)).join('')}});
  state.activePageId=previousPage;editingElementId=previousEditing;previewDB.accountLoggedIn=previousAccountState;return {username:currentConsoleUser?.username||'',siteName:state.siteName,description:state.description,theme:state.theme,background:state.background,contentWidth:state.contentWidth,sectionGap:state.sectionGap,pages,forumPosts:previewDB.forumPosts};
}
function siteAdminCredentialMarkup(admin){return `<div class="site-admin-credential"><small>本站独立管理员 · 仅显示这一次</small><b>${esc(admin.username)}</b><code>${esc(admin.password)}</code><button data-modal-action="copy" data-copy="用户名：${esc(admin.username)}\n密码：${esc(admin.password)}">复制管理员凭据</button><p>请登录发布网站后妥善保管。它不等于炼丹社Sites控制台账号，也不能登录其他网站。</p></div>`}
document.querySelector('#publishBtn').onclick=async()=>{const button=document.querySelector('#publishBtn');button.disabled=true;button.firstChild.textContent='发布中 ';try{const response=await fetch('/api/publish',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(buildPublishPayload())});const payload=await response.json();if(response.status===401){showAuthGate('登录已过期，请重新登录');throw new Error('登录已过期')}if(!response.ok)throw new Error(payload.error||'发布失败');notifyPublishedReload('站点已重新发布');const credential=payload.siteAdmin?siteAdminCredentialMarkup(payload.siteAdmin):payload.accountEnabled?'<div class="site-account-existing"><p>本站独立账号数据库已保留，重新发布不会覆盖用户和登录密码。</p><button data-site-account-reset>忘记站长密码？重置密码</button></div>':'';openModal('发布成功',`<p>网站及页面树已经发布到 <b>${esc(payload.url)}</b>，已经打开的发布预览会自动刷新。</p>${credential}<a class="modal-action" href="${payload.url}" target="_blank">打开 ${esc(payload.url)} ↗</a>`)}catch(error){showToast(`发布失败：${error.message}`)}finally{button.disabled=false;button.firstChild.textContent='发布 '}};
document.querySelector('.preview-button').addEventListener('click',()=>window.open(`/${encodeURIComponent(currentConsoleUser?.username||'test')}`,'_blank'));
document.querySelector('#consoleAccountBtn').addEventListener('click',event=>{event.stopPropagation();const menu=document.querySelector('#consoleAccountMenu');menu.hidden=!menu.hidden;document.querySelector('#consoleAccountBtn').setAttribute('aria-expanded',String(!menu.hidden))});
document.querySelector('#consoleAccountMenu').addEventListener('click',event=>event.stopPropagation());
document.querySelector('#consoleAccountAction').addEventListener('click',openPasswordSettings);
document.querySelector('#inviteManagerBtn').addEventListener('click',()=>void openInviteManager());
document.querySelector('#consoleLogoutBtn').addEventListener('click',()=>void logoutConsole());
document.querySelector('#previewModal').addEventListener('submit',event=>{const form=event.target.closest('[data-console-form]');if(!form)return;event.preventDefault();const data=new FormData(form);const button=form.querySelector('button');button.disabled=true;void(async()=>{try{if(form.dataset.consoleForm==='password'){const next=String(data.get('newPassword')||'');if(next!==String(data.get('confirmPassword')||''))throw new Error('两次输入的新密码不一致');await consoleRequest('/api/auth/change-password',{method:'POST',body:{oldPassword:String(data.get('oldPassword')||''),newPassword:next}});closeModal();showToast('密码已修改，其他设备的会话已退出')}else if(form.dataset.consoleForm==='invite-generate'){const payload=await consoleRequest('/api/admin/invites',{method:'POST',body:{count:Number(data.get('count')||1)}});await navigator.clipboard?.writeText((payload.codes||[]).join('\n'));showToast(`已生成 ${payload.codes?.length||0} 个邀请码，并复制到剪贴板`);await openInviteManager()}}catch(error){showToast(error.message)}finally{button.disabled=false}})()});
document.querySelector('#previewModal').addEventListener('click',event=>{const otherSessions=event.target.closest('[data-revoke-other-sessions]');if(otherSessions){otherSessions.disabled=true;void consoleRequest('/api/auth/sessions/revoke-others',{method:'POST',body:{}}).then(payload=>{showToast(`已退出其他设备（${payload.removed} 个会话）`);return openPasswordSettings()}).catch(error=>{otherSessions.disabled=false;showToast(error.message)});return}const userStatus=event.target.closest('[data-user-status]');if(userStatus){const verb=userStatus.dataset.userStatus==='disabled'?'停用':'启用';if(!window.confirm(`确认${verb}用户 ${userStatus.dataset.userName}？`))return;userStatus.disabled=true;void consoleRequest('/api/admin/users/status',{method:'POST',body:{username:userStatus.dataset.userName,status:userStatus.dataset.userStatus}}).then(()=>{showToast(`已${verb}用户`);return openInviteManager()}).catch(error=>{userStatus.disabled=false;showToast(error.message)});return}const reset=event.target.closest('[data-site-account-reset]');if(reset){if(!window.confirm('重置后，原站长密码会立即失效。确认继续？'))return;reset.disabled=true;void consoleRequest('/api/site-account/reset-owner',{method:'POST',body:{}}).then(payload=>{openModal('管理员密码已重置',siteAdminCredentialMarkup(payload.siteAdmin));showToast('本站管理员密码已重置')}).catch(error=>{reset.disabled=false;showToast(error.message)});return}const modalCopy=event.target.closest('[data-modal-action="copy"]');if(modalCopy){void navigator.clipboard?.writeText(modalCopy.dataset.copy||'');showToast('已复制本站管理员凭据');return}const copy=event.target.closest('[data-invite-copy]');if(copy){void navigator.clipboard?.writeText(copy.dataset.inviteCopy||'');showToast('邀请码已复制');return}const revoke=event.target.closest('[data-invite-revoke]');if(revoke){if(!window.confirm(`确认撤销邀请码 ${revoke.dataset.inviteRevoke}？撤销后不能用于注册。`))return;revoke.disabled=true;void consoleRequest('/api/admin/invites/revoke',{method:'POST',body:{code:revoke.dataset.inviteRevoke}}).then(()=>{showToast('邀请码已撤销');return openInviteManager()}).catch(error=>{revoke.disabled=false;showToast(error.message)})}});
document.querySelectorAll('[data-context-action]').forEach(button=>button.addEventListener('click',()=>{const id=contextElementId;if(!id)return;const action=button.dataset.contextAction;hideElementContext();if(action==='edit')openElementEditor(id);if(action==='move')moveElementUp(id);if(action==='delete')deleteElement(id)}));document.addEventListener('click',event=>{if(!event.target.closest('#elementContextMenu'))hideElementContext()});document.addEventListener('scroll',hideElementContext,true);window.addEventListener('resize',hideElementContext);
document.addEventListener('click',()=>{const menu=document.querySelector('#consoleAccountMenu');menu.hidden=true;document.querySelector('#consoleAccountBtn').setAttribute('aria-expanded','false')});

renderPages();syncFields();renderCanvas();
window.addEventListener('alchemysites:authenticated',event=>void enterConsole(event.detail.user));
window.addEventListener('alchemysites:logged-out',()=>{currentConsoleUser=null;setSaveState('等待登录',false)});
window.addEventListener('alchemysites:toast',event=>showToast(event.detail.message));
