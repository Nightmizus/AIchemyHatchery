const elementCatalog = {
  nav:{name:'导航栏'}, hero:{name:'首页大字'}, projects:{name:'作品展示卡片栏'}, blog:{name:'文章列表'}, gallery:{name:'图片画廊'}, stats:{name:'数据栏'}, team:{name:'成员展示'}, timeline:{name:'活动时间线'}, profile:{name:'个人名片'}, schedule:{name:'课程表'}, countdown:{name:'目标倒计时'}, skills:{name:'技能进度'}, awards:{name:'荣誉墙'}, departments:{name:'部门介绍'}, steps:{name:'招新流程'}, faq:{name:'常见问题'}, downloads:{name:'资料下载'}, poll:{name:'投票'}, forum:{name:'论坛板块'}, account:{name:'账号登录'}, notice:{name:'公告栏'}, links:{name:'链接集合'}, cta:{name:'行动区域'}, footer:{name:'页脚'}, detail:{name:'详情正文'}
};

const INITIAL_STATE = {
  siteName:'未命名网站', description:'在这里写下网站的一句话介绍。', theme:'minimal', background:'#ffffff', contentWidth:100,
  pages:[{id:'page-1',name:'首页',path:'home',parentId:null,kind:'page',code:'',elements:[],objects:[]}], activePageId:'page-1'
};
const state = JSON.parse(JSON.stringify(INITIAL_STATE));
let currentConsoleUser=null;
let DRAFT_KEY='alchemyhatchery:guest:draft:v3';
let AI_UNDO_KEY='alchemyhatchery:guest:ai-undo:v2';
let AI_RELOAD_NOTICE_KEY='alchemyhatchery:guest:ai-reload-notice:v2';
let AI_RUN_KEY='alchemyhatchery:guest:ai-run:v1';
let AI_MODE_KEY='alchemyhatchery:guest:ai-mode:v1';
let AI_MODEL_KEY='alchemyhatchery:guest:ai-model:v1';
// 当前 AI 会话（GPT 式）：null 表示未保存的新聊天；列表来自服务端
let aiChatSessionId=null;
let aiSessionList=[];
let aiSessionSaving=false;
let aiChatViewEpoch=0;

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
let previewDevice=sessionStorage.getItem('alchemyhatchery:preview-device')||'desktop';if(!['desktop','mobile'].includes(previewDevice))previewDevice='desktop';
let historyStack=[JSON.stringify(state)];
let historyIndex=0;
let historyTimer=null;
let selectedObjectRef=null;
let objectPointerSession=null;
let activeTextRange=null;
let activeTextBookmark=null;
let isComposingText=false;
let deferredCanvasRender=false;
const SNAP_DISTANCE=7;

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
  const page={id:pageId,name:item.title,path:`${kind}-${item.id.split('-').pop()}`,parentId,kind:'detail',elements:[detailElement],objects:[]};
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
function objectStyle(element,key){element.objectStyles=element.objectStyles||{};element.objectStyles[key]=element.objectStyles[key]||{};return element.objectStyles[key]}
function existingObjectStyle(element,key){return element.objectStyles?.[key]||{}}
function paragraphStyle(element,key){element.paragraphStyles=element.paragraphStyles||{};element.paragraphStyles[key]=element.paragraphStyles[key]||{};return element.paragraphStyles[key]}
function pageObjects(page=activePage()){page.objects=Array.isArray(page.objects)?page.objects:[];return page.objects}
function sanitizeRichHtml(value,allowBlocks=false){
  const source=document.createElement('template');source.innerHTML=String(value||'');const allowedTags=new Set(['BR','SPAN','FONT','B','STRONG','I','EM','U','S','STRIKE',...(allowBlocks?['P','DIV']:[])]);
  const clean=node=>{
    if(node.nodeType===Node.TEXT_NODE)return document.createTextNode(node.textContent||'');
    if(node.nodeType!==Node.ELEMENT_NODE)return document.createDocumentFragment();
    const fragment=document.createDocumentFragment();if(!allowedTags.has(node.tagName)){[...node.childNodes].forEach(child=>fragment.append(clean(child)));return fragment}
    const output=document.createElement(node.tagName==='FONT'?'span':node.tagName.toLowerCase()),style=node.style;
    const copyStyle=(property,pattern)=>{const candidate=style.getPropertyValue(property).trim();if(candidate&&pattern.test(candidate))output.style.setProperty(property,candidate)};
    copyStyle('font-family',/^[\w\s,"'\-\u4e00-\u9fff]+$/);copyStyle('font-size',/^(?:[8-9]|[1-9]\d|1[0-5]\d|160)(?:\.\d+)?px$/);copyStyle('font-weight',/^(?:normal|bold|[1-9]00)$/);copyStyle('font-style',/^(?:normal|italic)$/);copyStyle('text-decoration',/^(?:none|underline|line-through|underline line-through|line-through underline)$/);copyStyle('text-decoration-line',/^(?:none|underline|line-through|underline line-through|line-through underline)$/);copyStyle('color',/^(?:#[0-9a-f]{3,8}|rgba?\([\d\s.,%]+\)|hsla?\([\d\s.,%]+\))$/i);copyStyle('background-color',/^(?:transparent|#[0-9a-f]{3,8}|rgba?\([\d\s.,%]+\)|hsla?\([\d\s.,%]+\))$/i);if(allowBlocks){copyStyle('line-height',/^(?:[1-3](?:\.\d+)?)$/);copyStyle('text-align',/^(?:left|center|right|justify)$/)}
    if(node.tagName==='FONT'){const face=String(node.getAttribute('face')||'').trim(),color=String(node.getAttribute('color')||'').trim();if(/^[\w\s,"'\-\u4e00-\u9fff]+$/.test(face))output.style.fontFamily=face;if(/^(?:#[0-9a-f]{3,8}|rgba?\([\d\s.,%]+\)|hsla?\([\d\s.,%]+\))$/i.test(color))output.style.color=color}
    [...node.childNodes].forEach(child=>output.append(clean(child)));if(output.tagName==='SPAN'&&!output.getAttribute('style')){const unwrapped=document.createDocumentFragment();while(output.firstChild)unwrapped.append(output.firstChild);return unwrapped}return output;
  };
  const result=document.createElement('template');[...source.content.childNodes].forEach(node=>result.content.append(clean(node)));return result.innerHTML;
}
function richObjectHtml(element,key,fallback,plainText=''){const record=element.richText?.[key],allowBlocks=key==='setting:body';if(typeof record==='string')return sanitizeRichHtml(record,allowBlocks);if(record&&typeof record.html==='string'&&String(record.text??'')===String(plainText??''))return sanitizeRichHtml(record.html,allowBlocks);return fallback}
function numberValue(value,fallback=0){const number=Number(value);return Number.isFinite(number)?number:fallback}
function scaledMobileFontSize(size){return 40+(size-40)*.1}
function refreshMobileFontScaling(root=document.querySelector('#siteCanvas')){
  if(!root)return;root.querySelectorAll('[data-mobile-font-scaled]').forEach(node=>{node.removeAttribute('data-mobile-font-scaled');node.removeAttribute('data-mobile-font-original');node.style.removeProperty('--mobile-font-size')});root.removeAttribute('data-mobile-font-scaled');root.removeAttribute('data-mobile-font-original');root.style.removeProperty('--mobile-font-size');if(root.dataset.previewDevice!=='mobile')return;
  const candidates=[root,...root.querySelectorAll('*')].filter(node=>node.matches?.('input,textarea,select')||[...node.childNodes].some(child=>child.nodeType===Node.TEXT_NODE&&child.textContent.trim()));root.dataset.previewDevice='desktop';void root.offsetWidth;const sizes=candidates.map(node=>[node,Number.parseFloat(getComputedStyle(node).fontSize)]);root.dataset.previewDevice='mobile';void root.offsetWidth;sizes.forEach(([node,size])=>{if(!(size>40))return;node.dataset.mobileFontScaled='true';node.dataset.mobileFontOriginal=String(size);node.style.setProperty('--mobile-font-size',`${scaledMobileFontSize(size)}px`)})
}
function objectCss(element,key,kind='text'){
  const style=existingObjectStyle(element,key);const declarations=[];const x=numberValue(style.x),y=numberValue(style.y),rotation=numberValue(style.rotation);
  if(x||y||rotation)declarations.push(`transform:translate(${x}px,${y}px) rotate(${rotation}deg)`);
  if(numberValue(style.width)>0)declarations.push(`width:${Math.round(numberValue(style.width))}px`,`max-width:none`);
  if(numberValue(style.height)>0)declarations.push(`height:${Math.round(numberValue(style.height))}px`);
  if(kind!=='text'){if(style.fontFamily)declarations.push(`font-family:${style.fontFamily}`);if(numberValue(style.fontSize)>0)declarations.push(`font-size:${numberValue(style.fontSize)}px`);if(style.fontWeight)declarations.push(`font-weight:${style.fontWeight}`);if(style.fontStyle)declarations.push(`font-style:${style.fontStyle}`);if(style.textDecoration)declarations.push(`text-decoration:${style.textDecoration}`);if(style.textAlign)declarations.push(`text-align:${style.textAlign}`);if(style.color)declarations.push(`color:${style.color}`);if(style.backgroundColor)declarations.push(`background-color:${style.backgroundColor}`);if(numberValue(style.lineHeight)>0)declarations.push(`line-height:${numberValue(style.lineHeight)}`)}
  if(kind==='text'){const paragraph=element.paragraphStyles?.[key]||{};if(paragraph.textAlign)declarations.push(`text-align:${paragraph.textAlign}`);if(numberValue(paragraph.lineHeight)>0)declarations.push(`line-height:${numberValue(paragraph.lineHeight)}`)}
  if(numberValue(style.zIndex)>0)declarations.push(`z-index:${Math.round(numberValue(style.zIndex))}`);if(kind==='image')declarations.push(`--image-fit:${style.objectFit||'contain'}`,`object-fit:${style.objectFit||'contain'}`);
  return declarations.join(';');
}
function objectDataAttrs(element,key,kind,editing){const css=objectCss(element,key,kind);return `${editing?` data-editor-object data-object-key="${esc(key)}" data-object-kind="${kind}"`:''}${css?` style="${esc(css)}"`:''}`}
function imageMarkup(item,index,element,editing=true){const key=`item:${item.id}:image`;if(!item.image)return `<span>${String(index+1).padStart(2,'0')}</span>${!editing&&buildingPreviewHtml?'<small class="image-hint">请使用AI助手编辑图片</small>':''}`;return `<img class="${editing?'editor-object image-object':'published-object'}" src="${esc(item.image)}" alt="${esc(item.title)}"${objectDataAttrs(element,key,'image',editing)}>`}
function floatingObjectMarkup(object,editing=true){const css=objectCss(object,'image','image');const classes=editing?'floating-page-image editor-object image-object':'floating-page-image published-image';return `<figure class="${classes}" data-page-object-id="${esc(object.id)}"${editing?' data-editor-object data-object-key="image" data-object-kind="image"':''}${css?` style="${esc(css)}"`:''}><img src="${esc(object.settings?.image||'')}" alt="${esc(object.settings?.alt||'插入的图片')}"></figure>`}
function floatingObjectLayer(page,editing=true){const objects=pageObjects(page);if(!objects.length)return '';return `<div class="${editing?'page-object-layer':'published-object-layer'}">${objects.map(object=>floatingObjectMarkup(object,editing)).join('')}</div>`}

function blockContent(type,element={},editing=true){
  const settings=element.settings||{};
  const raw=(key,fallback)=>String(settings[key]??fallback);
  const setting=(key,fallback)=>esc(raw(key,fallback));
  const textObject=(key,html,attributes='',multiline=false,placeholder='点击输入文字',plainText='')=>{const css=objectCss(element,key,'text'),content=richObjectHtml(element,key,html,plainText);if(!editing)return css?`<span class="published-object" style="${esc(css)}">${content}</span>`:content;return `<span class="direct-edit editor-object text-object${multiline?' direct-multiline':''}" contenteditable="true" spellcheck="false" ${attributes} data-placeholder="${esc(placeholder)}"${objectDataAttrs(element,key,'text',true)}>${content}</span>`};
  const direct=(key,fallback,multiline=false,placeholder='点击输入文字')=>{const value=raw(key,fallback);return textObject(`setting:${key}`,esc(value).replace(/\n/g,multiline?'<br>':' '),`data-direct-setting="${key}" data-direct-multiline="${multiline?'true':'false'}"`,multiline,placeholder,value)};
  const directList=(key,defaults,index,placeholder='点击输入')=>{const values=raw(key,defaults.join('|')).split('|');const value=values[index]??defaults[index]??'';return textObject(`list:${key}:${index}`,esc(value),`data-direct-list-setting="${key}" data-direct-index="${index}" data-direct-defaults="${esc(defaults.join('|'))}"`,false,placeholder,value)};
  const directItem=(item,key,placeholder='点击输入')=>{const value=String(item[key]||'');return textObject(`item:${item.id}:${key}`,esc(value),`data-direct-item-id="${item.id}" data-direct-item-field="${key}"`,false,placeholder,value)};
  const directPage=page=>textObject(`page:${page.id}`,esc(page.name),`data-direct-page-name="${page.id}"`,false,'页面名称',page.name);
  const linkedPages=()=>selectedNavPages(settings).map(page=>`<button data-preview-action="navigate" data-page-id="${page.id}" class="${page.id===state.activePageId?'active':''}">${directPage(page)}</button>`).join('');
  const name=esc(state.siteName||'未命名网站');
  const title=fallback=>direct('title',fallback,true,'点击编辑标题');
  if(type==='nav')return `<nav class="b-nav"><b>${direct('title',state.siteName||'未命名网站',false,'点击编辑站点名称')}</b><div>${linkedPages()}</div></nav>`;
  if(type==='hero')return `<section class="b-hero" style="--hero-title-size:${esc(cssSize(settings.titleSize,'clamp(48px,7vw,102px)'))}"><span>${direct('eyebrow','HELLO / 你好',false,'点击编辑眉题')}</span><h2>${title('把想法，\n变成真正的作品。')}</h2><p>${direct('description',state.description||'在这里写下网站的一句话介绍。',true,'点击编辑介绍')}</p><button class="b-button" data-preview-action="next-section">${direct('button','开始了解',false,'按钮文字')} ↗</button></section>`;
  if(type==='projects'){
    const items=collectionItems(element,'projects');const add=editing?'<button class="collection-add project-card" data-editor-action="add-item"><div class="project-image">＋</div><h3>新增作品</h3><p>同时创建一个详情子页面</p></button>':'';
    return `<section class="block-section"><header class="block-head"><div><span class="block-kicker">${direct('description','SELECTED WORK',false,'栏目眉题')}</span><h2>${title('最近的作品')}</h2></div><span class="block-link">${direct('button','点击卡片查看详情',false,'引导文字')} →</span></header><div class="project-grid">${items.map((item,index)=>`<button class="project-card${editing?' item-editable':''}" data-preview-action="project" data-item-id="${item.id}" data-page-id="${item.pageId||''}"><div class="project-image">${imageMarkup(item,index,element,editing)}</div><h3>${directItem(item,'title','作品标题')}</h3><p>${directItem(item,'meta','作品信息')}</p></button>`).join('')}${add}</div></section>`;
  }
  if(type==='blog'){
    const items=collectionItems(element,'blog');const add=editing?'<button class="collection-add article-row" data-editor-action="add-item"><span>＋</span><b>新增文章</b><em>创建详情页</em></button>':'';
    return `<section class="block-section"><header class="block-head"><div><span class="block-kicker">${direct('description','JOURNAL',false,'栏目眉题')}</span><h2>${title('最新文章')}</h2></div><span class="block-link">${direct('button','进入文章',false,'引导文字')} →</span></header><div class="blog-list">${items.map(item=>`<button class="article-row${editing?' item-editable':''}" data-preview-action="article" data-item-id="${item.id}" data-page-id="${item.pageId||''}"><span>${directItem(item,'meta','日期与阅读时长')}</span><b>${directItem(item,'title','文章标题')}</b><em>DETAIL</em></button>`).join('')}${add}</div></section>`;
  }
  if(type==='gallery'){const labels=['01','02','03','04'];return `<section class="block-section"><header class="block-head"><div><span class="block-kicker">${direct('description','MOMENTS',false,'栏目眉题')}</span><h2>${title('活动瞬间')}</h2></div></header><div class="gallery-grid">${labels.map((_,index)=>`<button data-preview-action="gallery" data-index="${index+1}">${directList('galleryLabels',labels,index,'图片标记')}</button>`).join('')}</div></section>`}
  if(type==='stats'){const valueDefaults=['86','24','12'],labelDefaults=['社团成员','开源项目','本学期活动'];return `<section class="stats-row">${[0,1,2].map(index=>`<div class="stat"><b>${directList('title',valueDefaults,index,'数据')}</b><small>${directList('description',labelDefaults,index,'说明')}</small></div>`).join('')}</section>`}
  if(type==='team'){const names=['林小满','陈星野','周一鸣','苏予安'],roles=['设计','开发','内容','运营'],faces=[':)',':D',';)','^^'];const currentNames=raw('memberNames',names.join('|')).split('|'),currentRoles=raw('memberRoles',roles.join('|')).split('|');return `<section class="block-section"><header class="block-head"><div><span class="block-kicker">${direct('description','OUR TEAM',false,'栏目眉题')}</span><h2>${title('认识成员')}</h2></div></header><div class="team-grid">${names.map((_,index)=>`<button class="member" data-preview-action="member" data-title="${esc(currentNames[index]||names[index])}" data-role="${esc(currentRoles[index]||roles[index])}"><div class="member-face">${directList('memberFaces',faces,index,'头像字符')}</div><h3>${directList('memberNames',names,index,'成员姓名')}</h3><p>${directList('memberRoles',roles,index,'成员职责')}</p></button>`).join('')}</div></section>`}
  if(type==='timeline'){const dates=['09.05','09.12','09.20'],names=['新学期招新','AI 入门工作坊','校园 Hackathon'],places=['大学生活动中心','实验楼 302','创新创业中心'];const currentNames=raw('eventNames',names.join('|')).split('|');const dynamicNotice=editing?'<small class="dynamic-inline-note">报名状态仅用于编辑器交互演示，不会带入预览或发布。</small>':'';return `<section class="block-section"><header class="block-head"><div><span class="block-kicker">${direct('description','UPCOMING',false,'栏目眉题')}</span><h2>${title('活动安排')}</h2>${dynamicNotice}</div></header><div class="timeline">${names.map((_,index)=>{const eventName=currentNames[index]||names[index];const joined=editing&&previewDB.registrations.has(eventName);return `<article class="time-row"><span>${directList('eventDates',dates,index,'日期')}</span><div><b>${directList('eventNames',names,index,'活动名称')}</b><p>${directList('eventPlaces',places,index,'活动地点')}</p><button class="event-action" data-preview-action="register" data-event="${esc(eventName)}">${joined?'已报名 ✓':'报名活动 →'}</button></div></article>`}).join('')}</div></section>`}
  if(type==='forum'){
    const demoPosts=editing?previewDB.forumPosts:[];
    const dynamicNotice=editing?'<div class="dynamic-preview-note"><b>动态内容不可编辑</b><span>下面的帖子和回复只用于编辑器交互演示，生成预览或正式发布时不会携带。</span></div>':'';
    const dynamicPosts=demoPosts.map((post,index)=>`<article class="forum-topic forum-topic-demo" aria-disabled="true"><span>${String(index+1).padStart(2,'0')}</span><b>${esc(post.title)}</b><em>${post.replies.length} 回复</em></article>`).join('');
    return `<section class="forum-block"><div class="forum-content"><span class="block-kicker">${direct('eyebrow','社区论坛',false,'栏目眉题')}</span><h2>${title('大家都在聊')}</h2>${settings.description?`<p>${direct('description','',true,'点击编辑论坛说明')}</p>`:''}<button class="b-button" data-preview-action="forum-compose">＋ ${direct('button','发布话题',false,'按钮文字')}</button>${dynamicNotice}<div class="dynamic-preview-data">${dynamicPosts}</div></div></section>`;
  }
  if(type==='account'){const loginLabel=raw('button','登录账号');const panel=previewDB.accountLoggedIn?`<span class="account-avatar">A</span><div><small>当前账号</small><b>admin</b></div><button data-preview-action="account-logout">退出账号</button>`:`<div><small>预览账号固定为</small><b>admin</b></div><button data-preview-action="account-login">${direct('button','登录账号',false,'按钮文字')} →</button>`;const dynamicNotice=editing?'<small class="dynamic-inline-note">登录状态仅为编辑器交互演示，不会随预览或发布保存。</small>':'';return `<section class="account-block"><div class="account-copy"><span class="block-kicker">${direct('eyebrow','MEMBER ACCESS',false,'栏目眉题')}</span><h2>${title('登录你的账号')}</h2><p>${direct('description','登录后即可查看成员内容与参与社区互动。',true,'点击编辑说明')}</p>${dynamicNotice}</div><div class="account-panel" data-login-label="${esc(loginLabel)}">${panel}</div></section>`}
  if(type==='notice')return `<section class="notice-block" style="--notice-bg:${esc(cssColor(settings.background,'var(--page-accent)'))};--notice-fg:${esc(cssColor(settings.color,'#141510'))}"><span>!</span><b>${title('秋季招新开始啦 · 9 月 5 日活动中心见')}</b><button data-preview-action="notice">${direct('button','查看详情',false,'按钮文字')} →</button></section>`;
  if(type==='links'){const labelDefaults=['加入交流群','查看 GitHub','关注公众号'];const urls=raw('description','#group|https://github.com|#wechat').split('|');return `<section class="links-block">${[0,1,2].map(index=>{const label=raw('title',labelDefaults.join('|')).split('|')[index]||'相关链接';return `<button class="link-card" data-preview-action="link" data-title="${esc(label)}" data-url="${esc(urls[index]||'')}"><b>${directList('title',labelDefaults,index,'链接名称')}</b><span>↗</span></button>`}).join('')}</section>`}
  if(type==='cta')return `<section class="cta-block"><h2>${title('一起把下一个好点子做出来。')}</h2><button data-preview-action="join">${editing?direct('button','现在加入',false,'按钮文字')+' ↗':setting('button','现在加入')+' ↗'}</button></section>`;
  if(type==='detail'){
    const page=activePage();const parent=page.parentId?state.pages.find(item=>item.id===page.parentId):null;const body=esc(settings.body||detailBody(settings.detailType||'article',settings.title||page.name)).split(/\n\n+/).map(text=>`<p>${text.replace(/\n/g,'<br>')}</p>`).join('');
    const bodyPlain=settings.body||detailBody(settings.detailType||'article',settings.title||page.name),bodyCss=objectCss(element,'setting:body','text'),bodyContent=richObjectHtml(element,'setting:body',body,bodyPlain);const detailBodyMarkup=editing?`<div class="detail-body direct-edit direct-block editor-object text-object" contenteditable="true" spellcheck="true" data-direct-setting="body" data-direct-multiline="true" data-placeholder="点击这里直接撰写正文"${objectDataAttrs(element,'setting:body','text',true)}>${bodyContent}</div>`:`<div class="detail-body${bodyCss?' published-object':''}"${bodyCss?` style="${esc(bodyCss)}"`:''}>${bodyContent}</div>`;
    const coverKey='setting:image';const cover=settings.image?`<div class="detail-cover ${editing?'editor-object image-object':'published-object published-image'}"${objectDataAttrs(element,coverKey,'image',editing)}><img src="${esc(settings.image)}" alt="${esc(settings.title||page.name)}"></div>`:(!editing&&buildingPreviewHtml?'<div class="detail-cover detail-cover-empty"><small class="image-hint">请使用AI助手编辑图片</small></div>':'');
    return `<article class="detail-block">${parent?`<button class="detail-back" data-preview-action="navigate" data-page-id="${parent.id}">← 返回 ${esc(parent.name)}</button>`:''}<span class="block-kicker">${direct('eyebrow',settings.detailType==='project'?'PROJECT DETAIL':'ARTICLE DETAIL',false,'详情类型')}</span><h1 style="--detail-title-size:${esc(cssSize(settings.titleSize,'clamp(48px,8vw,105px)'))}">${title(page.name)}</h1><p class="detail-lead">${direct('description','在这里填写详情摘要。',true,'点击编辑摘要')}</p>${cover}${detailBodyMarkup}</article>`;
  }
  if(type==='profile'){const tags=['编程','摄影','篮球'],labels=['学校','年级','坐标','邮箱'],values=['示例中学','高二','杭州','hi@example.com'];return `<section class="profile-block"><div class="profile-side"><div class="profile-avatar">${direct('avatar','林',false,'头像字符')}</div><h2>${title('林小满')}</h2><p class="profile-tagline">${direct('tagline','高二学生 · 喜欢用代码解决小问题',false,'一句话介绍')}</p><div class="profile-tags">${tags.map((_,index)=>`<span>${directList('tags',tags,index,'标签')}</span>`).join('')}</div></div><div class="profile-main"><span class="block-kicker">${direct('description','ABOUT ME',false,'栏目眉题')}</span><p class="profile-bio">${direct('bio','你好，我是林小满。白天上课，晚上写点小程序；最近在用 AI 帮社团做招新网站。我相信工具能放大好奇心，也相信把作品做出来给别人用，是最好的学习方式。',true,'自我介绍')}</p><dl class="profile-info">${labels.map((_,index)=>`<div><dt>${directList('infoLabels',labels,index,'项目')}</dt><dd>${directList('infoValues',values,index,'内容')}</dd></div>`).join('')}</dl></div></section>`}
  if(type==='schedule'){const days=['周一','周二','周三','周四','周五'],times=['08:00','10:00','14:00','16:00'],cellDefaults=['语文','数学','英语','物理','体育','化学','生物','历史','地理','自习','数学','英语','政治','音乐','社团活动','物理','化学','美术','信息技术','班会'];return `<section class="block-section schedule-section"><header class="block-head"><div><span class="block-kicker">${direct('description','MY TIMETABLE',false,'栏目眉题')}</span><h2>${title('本学期课程表')}</h2></div><span class="block-link">${direct('button','点击课程名可直接修改',false,'提示文字')}</span></header><div class="schedule-grid"><span class="schedule-corner">TIME</span>${days.map((_,index)=>`<b class="schedule-day">${directList('days',days,index,'星期')}</b>`).join('')}${times.map((time,row)=>`<span class="schedule-time">${directList('times',times,row,'时间')}</span>${days.map((_,col)=>`<i class="schedule-cell">${directList('cells',cellDefaults,row*5+col,'课程')}</i>`).join('')}`).join('')}</div></section>`}
  if(type==='countdown')return `<section class="countdown-block" data-countdown data-target="${setting('target','2027-06-07 09:00')}"><span class="block-kicker">${direct('description','COUNTDOWN',false,'栏目眉题')}</span><h2>${title('距离高考')}</h2><div class="cd-grid">${[['days','天'],['hours','时'],['mins','分'],['secs','秒']].map(unit=>`<div class="cd-cell"><b data-cd-${unit[0]}>--</b><small>${unit[1]}</small></div>`).join('')}</div><p class="cd-target">目标时间：${direct('target','2027-06-07 09:00',false,'格式 2027-06-07 09:00')}</p></section>`;
  if(type==='skills'){const names=['Python / 编程','摄影与剪辑','英语演讲','篮球'],percents=['85','70','60','75'];const currentPercents=raw('percents',percents.join('|')).split('|');return `<section class="block-section skills-section"><header class="block-head"><div><span class="block-kicker">${direct('description','MY SKILLS',false,'栏目眉题')}</span><h2>${title('我会这些')}</h2></div></header><div class="skill-list">${names.map((_,index)=>{const pct=Math.max(0,Math.min(100,Number(currentPercents[index])||0));return `<div class="skill-row"><b>${directList('skillNames',names,index,'技能名称')}</b><div class="skill-track"><i style="width:${pct}%"></i></div><span>${directList('percents',percents,index,'0-100')}%</span></div>`}).join('')}</div></section>`}
  if(type==='awards'){const years=['2026','2025','2025'],names=['信息学奥赛 · 省级二等奖','校英语演讲比赛 · 一等奖','青少年科技创新大赛 · 市级三等奖'],notes=['CSP-J/S 提高组，全省前 15%','从 120 名选手中晋级，演讲主题“AI 与我”','作品：基于图像识别的图书整理机器人'];return `<section class="block-section awards-section"><header class="block-head"><div><span class="block-kicker">${direct('description','HONORS',false,'栏目眉题')}</span><h2>${title('荣誉与奖项')}</h2></div></header><div class="award-list">${years.map((_,index)=>`<article class="award-row"><span class="award-year">${directList('awardYears',years,index,'年份')}</span><div class="award-main"><b>${directList('awardNames',names,index,'奖项名称')}</b><p>${directList('awardNotes',notes,index,'补充说明')}</p></div><i class="award-medal">★</i></article>`).join('')}</div></section>`}
  if(type==='departments'){const names=['技术部','设计部','活动部'],duties=['开发社团工具与网站，带新成员入门编程','负责海报、周边与每场活动的视觉','策划工作坊、Hackathon 与社团团建'],counts=['12 人','8 人','10 人'];return `<section class="block-section depts-section"><header class="block-head"><div><span class="block-kicker">${direct('description','DEPARTMENTS',false,'栏目眉题')}</span><h2>${title('部门介绍')}</h2></div><span class="block-link">${direct('button','总有一款适合你',false,'引导文字')}</span></header><div class="dept-list">${names.map((_,index)=>`<article class="dept-row"><span class="dept-index">0${index+1}</span><div class="dept-main"><b>${directList('deptNames',names,index,'部门名称')}</b><p>${directList('deptDuties',duties,index,'部门职责')}</p></div><span class="dept-count">${directList('deptCounts',counts,index,'人数')}</span></article>`).join('')}</div></section>`}
  if(type==='steps'){const names=['线上报名','见面会','部门面试','正式入社'],descs=['填写报名表，告诉我们你的兴趣和想做的事','和现任成员面对面聊聊社团日常与项目','聊聊你想尝试的方向，没有标准答案','领取新人礼包，参加第一次共创活动'],dates=['9.1 – 9.10','9.12','9.15 – 9.16','9.20'];return `<section class="block-section steps-section"><header class="block-head"><div><span class="block-kicker">${direct('description','HOW TO JOIN',false,'栏目眉题')}</span><h2>${title('招新流程')}</h2></div></header><div class="step-grid">${names.map((_,index)=>`<article class="step-card"><span class="step-num">0${index+1}</span><b>${directList('stepNames',names,index,'环节名称')}</b><p>${directList('stepDescs',descs,index,'环节说明')}</p><small>${directList('stepDates',dates,index,'时间')}</small></article>`).join('')}</div></section>`}
  if(type==='faq'){const questions=['没有基础可以加入吗？','每周需要投入多少时间？','可以中途换部门吗？','社团收费吗？'],answers=['完全可以。大部分活动都从零基础开始设计，老成员会一对一带新，一个学期就能独立完成小项目。','核心活动每周 1–2 小时，项目冲刺期自愿加时，学业优先。','可以，每学期初开放一次部门调整，也鼓励跨部门参与项目。','不收费。活动物料与场地由学校社团经费支持。'];return `<section class="block-section faq-section"><header class="block-head"><div><span class="block-kicker">${direct('description','FAQ',false,'栏目眉题')}</span><h2>${title('常见问题')}</h2></div></header><div class="faq-list">${questions.map((_,index)=>`<details class="faq-item"${index===0?' open':''}><summary>${directList('faqQuestions',questions,index,'问题')}<i>+</i></summary><p>${directList('faqAnswers',answers,index,'回答')}</p></details>`).join('')}</div></section>`}
  if(type==='downloads'){const names=['2026 秋季招新简章.pdf','社团章程与管理制度.docx','历届活动照片包.zip'],metas=['2.4 MB · 09.01 更新','186 KB · 08.20 更新','512 MB · 07.15 更新'],tags=['PDF','DOC','ZIP'];const currentNames=raw('fileNames',names.join('|')).split('|');const urls=raw('urls','#|#|#').split('|');return `<section class="block-section downloads-section"><header class="block-head"><div><span class="block-kicker">${direct('description','RESOURCES',false,'栏目眉题')}</span><h2>${title('资料下载')}</h2></div></header><div class="download-list">${names.map((_,index)=>`<button class="download-row" data-preview-action="link" data-title="${esc(currentNames[index]||names[index])}" data-url="${esc(urls[index]||'')}"><i class="dl-tag">${directList('fileTags',tags,index,'类型')}</i><div class="dl-main"><b>${directList('fileNames',names,index,'文件名称')}</b><p>${directList('fileMetas',metas,index,'大小与日期')}</p></div><span class="dl-arrow">↓</span></button>`).join('')}</div></section>`}
  if(type==='poll'){const options=['电竞友谊赛','露天电影夜','桌游马拉松'],votes=['18','24','9'];return `<section class="block-section poll-section"><header class="block-head"><div><span class="block-kicker">${direct('description','VOTE',false,'栏目眉题')}</span><h2>${title('社团之夜办什么？')}</h2></div><span class="block-link">${direct('button','每人一票，实时统计',false,'提示文字')}</span></header><div class="poll-box" data-poll><div class="poll-options">${options.map((_,index)=>`<button type="button" class="poll-option" data-poll-option="${index}"><b>${directList('pollOptions',options,index,'选项')}</b><i class="poll-track"><i class="poll-fill"></i></i><span class="poll-num" data-poll-num>${directList('pollVotes',votes,index,'票数')}</span></button>`).join('')}</div><p class="poll-hint">${direct('hint','点击选项即可投票，再点一次取消。',false,'投票提示')}</p></div></section>`}
  return `<footer class="b-footer"><div><b>${direct('title',state.siteName||'未命名网站',false,'点击编辑页脚名称')}</b>${linkedPages()}</div><small>${direct('description','© 2026 · 由 AIchemyHatchery 搭建',false,'点击编辑版权文字')}</small></footer>`;
}

function initTemplateWidgets(rootNode){
  if(!rootNode)return;
  rootNode.querySelectorAll('[data-countdown]').forEach(block=>{
    if(block.dataset.cdReady)return;block.dataset.cdReady='1';
    const cells={days:block.querySelector('[data-cd-days]'),hours:block.querySelector('[data-cd-hours]'),mins:block.querySelector('[data-cd-mins]'),secs:block.querySelector('[data-cd-secs]')};
    const pad=value=>String(Math.max(0,value)).padStart(2,'0');
    const tick=()=>{
      const raw=String(block.dataset.target||'').trim().replace(/\//g,'-').replace(' ','T');
      const target=new Date(/^\d{4}-\d{2}-\d{2}$/.test(raw)?`${raw}T00:00`:raw).getTime();
      if(!Number.isFinite(target)){if(cells.days)cells.days.textContent='—';return}
      const diff=target-Date.now();const total=Math.max(0,Math.floor(diff/1000));
      if(cells.days)cells.days.textContent=String(Math.floor(total/86400));
      if(cells.hours)cells.hours.textContent=pad(Math.floor(total%86400/3600));
      if(cells.mins)cells.mins.textContent=pad(Math.floor(total%3600/60));
      if(cells.secs)cells.secs.textContent=pad(total%60);
      block.classList.toggle('cd-done',diff<=0);
    };
    tick();setInterval(tick,1000);
  });
  rootNode.querySelectorAll('[data-poll]').forEach(box=>{
    if(box.dataset.pollReady)return;box.dataset.pollReady='1';
    const question=(box.closest('section')?.querySelector('h2')?.textContent||'poll').trim();
    const key=`hatchery:poll:${question}`;
    const options=[...box.querySelectorAll('[data-poll-option]')];
    const seeds=options.map(option=>Number(option.querySelector('[data-poll-num]')?.textContent)||0);
    let saved=null;try{saved=JSON.parse(localStorage.getItem(key)||'null')}catch{}
    const deltas=options.map((_,index)=>Number(saved?.d?.[index])||0);
    let voted=Number.isInteger(saved?.v)?saved.v:null;
    const persist=()=>{try{localStorage.setItem(key,JSON.stringify({v:voted,d:deltas}))}catch{}};
    const render=()=>{
      const counts=seeds.map((seed,index)=>seed+deltas[index]);
      const total=counts.reduce((sum,value)=>sum+value,0)||1;
      options.forEach((option,index)=>{
        const num=option.querySelector('[data-poll-num]'),fill=option.querySelector('.poll-fill');
        if(num&&num.textContent!==String(counts[index]))num.textContent=String(counts[index]);
        if(fill)fill.style.width=`${Math.round(counts[index]/total*100)}%`;
        option.classList.toggle('voted',voted===index);
      });
      box.classList.toggle('has-voted',voted!==null);
    };
    options.forEach((option,index)=>option.addEventListener('click',event=>{
      event.preventDefault();event.stopPropagation();
      if(voted===index){deltas[index]=Math.max(0,deltas[index]-1);voted=null}
      else{if(voted!==null)deltas[voted]=Math.max(0,deltas[voted]-1);deltas[index]+=1;voted=index}
      persist();render();
    }));
    render();
  });
}
function editDefaults(element){
  const defaults={nav:{title:state.siteName,description:'',button:''},hero:{title:'把想法，\n变成真正的作品。',description:state.description,button:'开始了解',titleSize:'clamp(48px,7vw,102px)'},projects:{title:'最近的作品',description:'SELECTED WORK',button:'点击卡片查看详情'},blog:{title:'最新文章',description:'JOURNAL',button:'进入文章'},gallery:{title:'活动瞬间',description:'MOMENTS',button:''},stats:{title:'86|24|12',description:'社团成员|开源项目|本学期活动',button:''},team:{title:'认识成员',description:'OUR TEAM',button:''},timeline:{title:'活动安排',description:'UPCOMING',button:''},profile:{title:'林小满',description:'ABOUT ME',button:'',avatar:'林',tagline:'高二学生 · 喜欢用代码解决小问题',bio:'你好，我是林小满。白天上课，晚上写点小程序；最近在用 AI 帮社团做招新网站。我相信工具能放大好奇心，也相信把作品做出来给别人用，是最好的学习方式。'},schedule:{title:'本学期课程表',description:'MY TIMETABLE',button:'点击课程名可直接修改'},countdown:{title:'距离高考',description:'COUNTDOWN',target:'2027-06-07 09:00',button:''},skills:{title:'我会这些',description:'MY SKILLS',button:''},awards:{title:'荣誉与奖项',description:'HONORS',button:''},departments:{title:'部门介绍',description:'DEPARTMENTS',button:'总有一款适合你'},steps:{title:'招新流程',description:'HOW TO JOIN',button:''},faq:{title:'常见问题',description:'FAQ',button:''},downloads:{title:'资料下载',description:'RESOURCES',button:''},poll:{title:'社团之夜办什么？',description:'VOTE',button:'每人一票，实时统计',hint:'点击选项即可投票，再点一次取消。'},forum:{title:'大家都在聊',description:'',button:'发布话题'},account:{title:'登录你的账号',description:'登录后即可查看成员内容与参与社区互动。',button:'登录账号'},notice:{title:'秋季招新开始啦 · 9 月 5 日活动中心见',description:'',button:'查看详情',background:'var(--page-accent)',color:'#141510'},links:{title:'加入交流群|查看 GitHub|关注公众号',description:'#group|https://github.com|#wechat',button:''},cta:{title:'一起把下一个好点子做出来。',description:'',button:'现在加入'},footer:{title:state.siteName,description:'© 2026 · 由 AIchemyHatchery 搭建',button:''},detail:{title:activePage().name,description:'在这里填写详情摘要。',body:'在这里填写详情正文。',button:''}};
  return {...defaults[element.type],...(element.settings||{})};
}
function inlineEditorMarkup(element){
  const values=editDefaults(element);const complex=element.type==='projects'||element.type==='blog';const currentItem=complex?element.items?.find(item=>item.id===editingItemId):null;
  const navChooser=element.type==='nav'?`<div class="inline-page-chooser"><b>显示在导航中的页面</b>${state.pages.map(page=>`<label style="--depth:${pageAncestors(page).length-1}"><input type="checkbox" data-nav-page="${page.id}" ${selectedNavPages(values).some(item=>item.id===page.id)?'checked':''}><span>${esc(page.name)}</span><small>/${esc(pageFullPath(page))}</small></label>`).join('')}</div>`:'';
  const instanceStyleFields=element.type==='hero'?`<label>当前大字字号<input data-inline-setting="titleSize" value="${esc(values.titleSize)}" placeholder="例如 clamp(38px,5.5vw,78px)"></label>`:element.type==='notice'?`<label>当前公告底色<input data-inline-setting="background" value="${esc(values.background)}" placeholder="var(--page-bg) 或 #ffffff"></label><label>当前公告文字色<input data-inline-setting="color" value="${esc(values.color)}" placeholder="var(--page-fg) 或 #111111"></label>`:'';
  const itemEditor=currentItem?`<div class="inline-item-editor"><header><div><b>${esc(currentItem.title||'未命名条目')}</b><small>标题和辅助信息直接在卡片中输入</small></div><button data-editor-action="close-item">返回</button></header><label>详情页摘要<textarea data-item-field="summary" rows="2">${esc(currentItem.summary||'')}</textarea></label><label>图片地址<input data-item-field="image" value="${esc(currentItem.image||'')}" placeholder="https://… 或上传本地图片"></label><label class="inline-upload">上传图片<input type="file" accept="image/*" data-item-upload></label>${currentItem.pageId?`<button class="inline-secondary" data-editor-action="open-detail" data-page-id="${currentItem.pageId}">打开详情页编辑</button>`:''}<button class="inline-danger" data-editor-action="delete-item">删除这个条目及详情页</button></div>`:'';
  const detailMedia=element.type==='detail'?`<div class="inline-fields"><label>封面图片地址<input data-inline-setting="image" value="${esc(values.image||'')}" placeholder="https://…；正文直接在页面中修改"></label></div>`:'';
  const linkSettings=element.type==='links'?`<div class="inline-fields">${(values.description||'#group|https://github.com|#wechat').split('|').map((url,index)=>`<label>链接 ${index+1} 地址<input data-pipe-setting="description" data-pipe-index="${index}" value="${esc(url)}" placeholder="https://… 或 #group"></label>`).join('')}</div>`:'';
  const auxiliary=instanceStyleFields?`<div class="inline-fields">${instanceStyleFields}</div>`:'';
  const extra=auxiliary+navChooser+linkSettings+detailMedia;
  const guidance=element.type==='forum'?'<p class="inline-tip"><b>论坛静态标题可以直接修改。</b><br>帖子、回复和登录状态属于动态数据，不提供编辑，也不会带入预览或发布。</p>':complex?'<p class="inline-tip">标题和辅助信息直接输入；点击卡片打开图片与详情设置，点击“＋”新增条目。</p>':extra?'':'<p class="inline-tip">该模块没有额外设置，全部文字都可直接在页面中输入。</p>';
  return `<aside class="canvas-inspector" data-inline-editor><header><div><small>MODULE SETTINGS</small><b>${elementCatalog[element.type].name} · 页面内设置</b></div><button data-editor-action="close-inspector" aria-label="关闭设置">×</button></header>${extra}${guidance}${itemEditor}</aside>`;
}
function elementMarkup(element){const inspectorOpen=editingElementId===element.id;const height=numberValue(element.layoutHeight);const heightStyle=height>0?` style="height:${Math.round(height)}px"`:'';return `<section class="page-element editing${inspectorOpen?' inspector-open':''}" data-element-id="${element.id}" data-element-type="${element.type}"${heightStyle}><div class="element-content-clip">${blockContent(element.type,element,true)}</div>${inspectorOpen?inlineEditorMarkup(element):''}</section>`}
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
function scheduleDraftSave(){if(!currentConsoleUser)return;setSaveState('保存中…',true);clearTimeout(draftSaveTimer);draftSaveTimer=setTimeout(async()=>{try{const snapshot=JSON.stringify(state);await persistDraftSnapshot(snapshot);const time=new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});setSaveState(`已同步 · ${time}`,false)}catch{setSaveState('同步失败',false)}},350);scheduleHistoryCapture();schedulePreviewPush()}
// 预览实时化：编辑防抖后持续把整站预览负载推到服务端，预览按钮与已打开页面始终是当前内容
let previewPushTimer=null,previewPushInFlight=false,previewPushPending=false,previewPushHash='';
function previewPayloadHash(text){let hash=5381;for(let i=0;i<text.length;i++)hash=((hash<<5)+hash+text.charCodeAt(i))>>>0;return hash.toString(36)}
function schedulePreviewPush(){if(!currentConsoleUser?.previewId)return;clearTimeout(previewPushTimer);previewPushTimer=setTimeout(()=>void pushPreviewSnapshot(),1500)}
async function pushPreviewSnapshot(allowCreate=false){
  if(!currentConsoleUser)return null;
  if(!allowCreate&&!currentConsoleUser.previewId)return null;// 没用过预览的账号不主动创建，首次点击按钮时再生成
  if(previewPushInFlight){previewPushPending=true;return null}
  let payloadObj=null,body='';
  try{payloadObj=buildPublishPayload(true);body=JSON.stringify(payloadObj)}catch{return null}
  const hash=previewPayloadHash(body);
  if(hash===previewPushHash)return currentConsoleUser.previewId?{previewId:currentConsoleUser.previewId,url:`/preview/${currentConsoleUser.previewId}`}:null;// 内容没变就不重复写库、不打扰已打开的页面
  previewPushInFlight=true;
  try{
    const payload=await consoleRequest('/api/preview',{method:'POST',body:payloadObj});
    previewPushHash=hash;
    const firstTime=!currentConsoleUser.previewId;
    currentConsoleUser.previewId=payload.previewId;
    if(firstTime)updateConsoleAccount();
    notifyPreviewTabsReload(payload.previewId);
    return payload;
  }catch(error){
    if(error.status!==401)previewPushHash='';// 下次编辑重试
    return null;
  }finally{
    previewPushInFlight=false;
    if(previewPushPending){previewPushPending=false;schedulePreviewPush()}
  }
}
function notifyPreviewTabsReload(previewId){
  const previewPath=`/preview/${previewId}`;
  try{const channel=new BroadcastChannel('alchemyhatchery-live-preview');channel.postMessage({type:'reload',reason:'预览内容已更新',previewPath,at:Date.now()});channel.close()}catch{}
  try{localStorage.setItem('alchemyhatchery:preview-reload',JSON.stringify({previewPath,at:Date.now()}))}catch{}
}
function restoreHistory(index){if(index<0||index>=historyStack.length)return;const message=index<historyIndex?'已撤销':'已重做';historyIndex=index;const snapshot=JSON.parse(historyStack[index]);Object.keys(state).forEach(key=>delete state[key]);Object.assign(state,snapshot);editingElementId=null;editingItemId=null;renderPages();renderCanvas();updateHistoryButtons();showToast(message)}
function undoState(){captureHistoryNow();if(historyIndex>0)restoreHistory(historyIndex-1)}
function redoState(){if(historyIndex<historyStack.length-1)restoreHistory(historyIndex+1)}
function isolatePageCode(code){
  // 子网站样式隔离：blob 里的 <style> 全局生效会污染控制台，统一套上 @scope 作用域
  if(!code)return '';
  const scoped=code.includes('@scope')?code:code.replace(/<style(\s[^>]*)?>([\s\S]*?)<\/style>/gi,(match,attrs,body)=>`<style${attrs||''}>@scope (.pg-scope) {\n${body}\n}</style>`);
  return `<div class="pg-scope">${scoped}</div>`;
}
function ensurePageCode(page){
  // 整页代码模式：page.code 是唯一渲染来源。旧草稿只有模块元素时，一次性渲染成代码并清空元素
  if(!page||typeof page.code==='string')return;
  const previousPage=state.activePageId;state.activePageId=page.id;
  try{page.code=(page.elements||[]).map(element=>blockContent(element.type,element,false)).join('')}catch{page.code=''}
  page.elements=[];page.codeGeneratedFromElements=true;
  state.activePageId=previousPage;
}
function ensureAllPagesCode(){const previousPage=state.activePageId;state.pages.forEach(page=>ensurePageCode(page));state.activePageId=previousPage}
function renderCanvas(){
  if(isComposingText){deferredCanvasRender=true;return}captureTextSelectionBookmark();const page=activePage();if(!page)return;
  const canvas=document.querySelector('#siteCanvas');canvas.className=`site-canvas theme-${state.theme}`;canvas.dataset.previewDevice=previewDevice;canvas.style.setProperty('--page-bg',state.background||'#ffffff');canvas.style.setProperty('--canvas-width',previewDevice==='desktop'?`${state.contentWidth||100}%`:'390px');
  const legacyImages=(page.elements||[]).filter(element=>element.type==='image');if(legacyImages.length){legacyImages.forEach((element,index)=>pageObjects(page).push({id:uid('obj'),kind:'image',settings:{image:element.settings?.image||'',alt:element.settings?.alt||'插入的图片'},objectStyles:{image:{width:element.objectStyles?.image?.width||360,height:element.objectStyles?.image?.height||230,objectFit:element.objectStyles?.image?.objectFit||'contain',x:element.objectStyles?.image?.x||60+index*24,y:element.objectStyles?.image?.y||70+index*24,rotation:element.objectStyles?.image?.rotation||0}}}));page.elements=page.elements.filter(element=>element.type!=='image')}
  ensurePageCode(page);
  const content=document.querySelector('#canvasContent');const flow=page.code.trim()||pageObjects(page).length?page.code:`<div class="empty-canvas" data-empty-drop><div><i>＋</i><b>这是一个空白页面</b><small>在左侧告诉 AI 你想做什么，让它帮你搭建</small></div></div>`;content.innerHTML=isolatePageCode(flow)+floatingObjectLayer(page,true);
  refreshMobileFontScaling(canvas);bindCanvasEvents();initTemplateWidgets(content);enforceElementHeightLimits();ensureObjectEditorChrome();restoreSelectedObject();restoreTextSelectionBookmark();const selected=editingElementId?getElementById(editingElementId):null;const hasContent=page.code.trim().length||pageObjects(page).length;document.querySelector('#selectionState').textContent=selected?`已打开设置：${elementCatalog[selected.type]?.name||selected.type}`:selectedObjectRef?(selectedObjectRef.kind==='text'?'可直接输入文字':'拖动边框移动 · 拖动控制点缩放'):hasContent?'页面为整页代码，由 AI 直接修改':'空白页面';document.querySelectorAll('[data-preview-device]').forEach(button=>button.classList.toggle('active',button.dataset.previewDevice===previewDevice));scheduleDraftSave();updatePreviewVisibility();
}

function renderPages(){
  document.querySelector('#currentBreadcrumb').textContent=pageAncestors(activePage()).map(page=>page.name).join(' / ');
}
function selectPage(id){if(!state.pages.some(page=>page.id===id))return;state.activePageId=id;editingElementId=null;editingItemId=null;renderPages();renderCanvas();const canvas=document.querySelector('#siteCanvas');if(canvas){canvas.classList.remove('page-enter');void canvas.offsetWidth;canvas.classList.add('page-enter')}document.querySelector('#canvasScroll').scrollTop=0}
function addPage(parentId=null){const n=state.pages.length+1;const page={id:uid('page'),name:parentId?`子页面 ${n}`:`页面 ${n}`,path:`page-${n}`,parentId:parentId||null,kind:'page',code:'',elements:[],objects:[]};state.pages.push(page);state.activePageId=page.id;editingElementId=null;renderPages();renderCanvas();showToast(`已创建空白页面「${page.name}」`)}
function uniquePagePath(base,parentId){let path=base||'page',suffix=2;const used=value=>state.pages.some(page=>page.parentId===parentId&&page.path===value);while(used(path))path=`${base||'page'}-${suffix++}`;return path}
function duplicatePage(id){
  const source=state.pages.find(page=>page.id===id);if(!source)return;const subtree=[];const collect=pageId=>{const page=state.pages.find(item=>item.id===pageId);if(!page)return;subtree.push(page);state.pages.filter(item=>item.parentId===pageId).forEach(child=>collect(child.id))};collect(id);
  const pageMap=new Map(subtree.map(page=>[page.id,uid('page')]));const clones=subtree.map(page=>{const cloned=JSON.parse(JSON.stringify(page));cloned.id=pageMap.get(page.id);cloned.parentId=page.id===id?page.parentId:pageMap.get(page.parentId);if(page.id===id){cloned.name=`${page.name} 副本`;cloned.path=uniquePagePath(`${page.path}-copy`,cloned.parentId)}cloned.elements=(cloned.elements||[]).map(element=>{element.id=uid('el');if(Array.isArray(element.settings?.pageIds))element.settings.pageIds=element.settings.pageIds.map(pageId=>pageMap.get(pageId)||pageId);if(Array.isArray(element.items))element.items=element.items.map(item=>({...item,id:uid('item'),pageId:pageMap.get(item.pageId)||item.pageId}));return element});return cloned});
  state.pages.push(...clones);state.activePageId=pageMap.get(id);editingElementId=null;editingItemId=null;renderPages();renderCanvas();showToast(`已复制「${source.name}」及其子页面`)
}
function deletePage(id){if(id===state.pages[0].id){showToast('首页不能删除');return}removePageCascade(id);if(!state.pages.some(page=>page.id===state.activePageId))state.activePageId=state.pages[0].id;editingElementId=null;renderPages();renderCanvas();showToast('页面及其子页面已删除')}
function addElement(type,index=activePage().elements.length){if(anyAiRunActive()){showToast('AI 正在修改网站，完成后再添加模块');return}const page=activePage();if(typeof page.code==='string'){showToast('页面为整页代码模式：请通过左侧「浏览模板」让 AI 嵌入模块');return}let accountAdded=false;if(type==='forum'&&!page.elements.some(item=>item.type==='account')){page.elements.splice(index,0,createElement('account',page));index+=1;accountAdded=true}const element=createElement(type,page);page.elements.splice(index,0,element);renderPages();renderCanvas();showToast(accountAdded?'已添加「账号登录」和「论坛板块」；论坛必须登录后互动':`已添加「${elementCatalog[type].name}」`)}

function clearDropHints(){document.querySelectorAll('.insert-zone').forEach(zone=>zone.classList.remove('active'));document.querySelector('[data-empty-drop]')?.classList.remove('drag-active')}
function findDropIndex(clientY){const elements=[...document.querySelectorAll('.page-element')];for(let index=0;index<elements.length;index++){const rect=elements[index].getBoundingClientRect();if(clientY<rect.top+rect.height/2)return index}return elements.length}
function showDropIndex(index){clearDropHints();const zone=document.querySelector(`[data-insert-index="${index}"]`);if(zone)zone.classList.add('active');else document.querySelector('[data-empty-drop]')?.classList.add('drag-active')}
function handleDrop(index){if(!dragPayload)return;const page=activePage();if(dragPayload.source==='palette')addElement(dragPayload.type,index);else{const oldIndex=page.elements.findIndex(item=>item.id===dragPayload.id);if(oldIndex>=0){const [moved]=page.elements.splice(oldIndex,1);const target=oldIndex<index?index-1:index;page.elements.splice(Math.max(0,target),0,moved);renderCanvas()}}dragPayload=null;document.body.classList.remove('is-dragging');clearDropHints()}

function getElementById(id){return activePage().elements.find(item=>item.id===id)}
function syncItemDetail(item){const page=state.pages.find(entry=>entry.id===item.pageId);if(!page)return;page.name=item.title||'未命名详情';const detail=page.elements.find(element=>element.type==='detail');if(detail){detail.settings.title=item.title;detail.settings.description=item.summary||item.meta;detail.settings.image=item.image||''}renderPages()}
function addCollectionItem(element){const item=makeCollectionItem(element.type,element.items.length);element.items.push(item);createDetailPage(activePage().id,element.type==='projects'?'project':'article',item);editingItemId=item.id;renderPages();renderCanvas()}
function deleteCollectionItem(element,itemId){const index=element.items.findIndex(item=>item.id===itemId);if(index<0)return;const [item]=element.items.splice(index,1);if(item.pageId)removePageCascade(item.pageId);editingItemId=null;renderPages();renderCanvas();showToast('条目和对应详情页已删除')}
function openElementEditor(id){const element=getElementById(id);if(!element)return;editingElementId=editingElementId===id?null:id;editingItemId=null;renderCanvas();if(editingElementId)showToast('已打开页面内设置；文字始终可以直接输入')}
function deleteElement(id){const page=activePage();const element=page.elements.find(item=>item.id===id);if(element?.type==='account'&&page.elements.some(item=>item.type==='forum')){showToast('当前页面有论坛，不能单独删除账号登录模块');return}if(element)cleanupElementDetails([element]);page.elements=page.elements.filter(item=>item.id!==id);editingElementId=null;renderPages();renderCanvas()}
function moveElementUp(id){const page=activePage();const index=page.elements.findIndex(item=>item.id===id);if(index>0)[page.elements[index-1],page.elements[index]]=[page.elements[index],page.elements[index-1]];else if(index===0&&page.elements.length>1)page.elements.push(page.elements.shift());renderCanvas()}

function directText(node){return String(node.innerText??node.textContent??'').replace(/\r/g,'').replace(/\u00a0/g,' ')}
function elementForEditorNode(node){return getElementById(node.closest('.page-element')?.dataset.elementId)}
function updatePipeSetting(element,field,index,value,defaults=''){
  const values=String(element.settings?.[field]??defaults).split('|');while(values.length<=index)values.push('');values[index]=value;element.settings={...(element.settings||{}),[field]:values.join('|')};
}
function syncDetailOwner(field,value){
  const page=activePage();if(page.kind!=='detail')return;state.pages.forEach(owner=>owner.elements.forEach(element=>(element.items||[]).forEach(item=>{if(item.pageId!==page.id)return;if(field==='title')item.title=value;if(field==='description')item.summary=value;if(field==='image')item.image=value})));renderPages();
}
function textObjectForRange(range){
  if(!range)return null;const elementFor=node=>node?.nodeType===Node.ELEMENT_NODE?node:node?.parentElement;const start=elementFor(range.startContainer)?.closest?.('.text-object[contenteditable="true"]'),end=elementFor(range.endContainer)?.closest?.('.text-object[contenteditable="true"]');return start&&start===end?start:null;
}
function logicalTextLength(node){if(node.nodeType===Node.TEXT_NODE)return node.textContent?.length||0;if(node.nodeType===Node.ELEMENT_NODE&&node.tagName==='BR')return 1;return [...node.childNodes].reduce((total,child)=>total+logicalTextLength(child),0)}
function boundaryTextOffset(root,container,offset){try{const probe=document.createRange();probe.selectNodeContents(root);probe.setEnd(container,offset);return logicalTextLength(probe.cloneContents())}catch{return 0}}
function boundaryFromTextOffset(root,target){
  let remaining=Math.max(0,target);const walker=document.createTreeWalker(root,NodeFilter.SHOW_ELEMENT|NodeFilter.SHOW_TEXT);let current;while((current=walker.nextNode())){if(current.nodeType===Node.TEXT_NODE){const length=current.textContent?.length||0;if(remaining<=length)return {container:current,offset:remaining};remaining-=length}else if(current.tagName==='BR'){const parent=current.parentNode,index=[...parent.childNodes].indexOf(current);if(remaining<=1)return {container:parent,offset:index+(remaining?1:0)};remaining-=1}}
  return {container:root,offset:root.childNodes.length};
}
function makeTextBookmark(node,range){if(!node||!range||textObjectForRange(range)!==node||!selectedObjectRef?.elementId)return null;return {pageId:state.activePageId,elementId:selectedObjectRef.elementId,key:selectedObjectRef.key,start:boundaryTextOffset(node,range.startContainer,range.startOffset),end:boundaryTextOffset(node,range.endContainer,range.endOffset)}}
function restoreRangeOffsets(node,bookmark){if(!node||!bookmark)return null;const start=boundaryFromTextOffset(node,bookmark.start),end=boundaryFromTextOffset(node,bookmark.end),range=document.createRange();try{range.setStart(start.container,start.offset);range.setEnd(end.container,end.offset)}catch{return null}const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);activeTextRange=range.cloneRange();return range}
function captureTextSelectionBookmark(){const node=selectedObjectNode(),range=currentTextRange(false);if(node&&range){const bookmark=makeTextBookmark(node,range);if(bookmark)activeTextBookmark=bookmark}}
function restoreTextSelectionBookmark(){if(!activeTextBookmark||activeTextBookmark.pageId!==state.activePageId||activeTextBookmark.elementId!==selectedObjectRef?.elementId||activeTextBookmark.key!==selectedObjectRef?.key)return;const node=selectedObjectNode();if(node)restoreRangeOffsets(node,activeTextBookmark)}
function rememberTextSelection(){
  const selection=window.getSelection();if(!selection?.rangeCount)return;const range=selection.getRangeAt(0),node=textObjectForRange(range);if(!node)return;if(selectedObjectNode()!==node)selectEditorObject(node);activeTextRange=range.cloneRange();activeTextBookmark=makeTextBookmark(node,range);
}
function currentTextRange(requireText=true){
  const node=selectedObjectNode();if(!node||selectedObjectRef?.kind!=='text')return null;const selection=window.getSelection();if(selection?.rangeCount){const live=selection.getRangeAt(0);if(textObjectForRange(live)===node)activeTextRange=live.cloneRange()}
  if(!activeTextRange||textObjectForRange(activeTextRange)!==node||!activeTextRange.startContainer.isConnected)return null;if(requireText&&(activeTextRange.collapsed||!activeTextRange.toString()))return null;return activeTextRange.cloneRange();
}
function selectedTextStyleNode(node=selectedObjectNode()){
  const range=currentTextRange(false);let target=range?.startContainer;if(target?.nodeType!==Node.ELEMENT_NODE)target=target?.parentElement;if(!target||!node?.contains(target))target=node;return target;
}
function selectedTextComputedStyle(node=selectedObjectNode()){return getComputedStyle(selectedTextStyleNode(node))}
function persistRichTextNode(node){
  const element=elementForEditorNode(node),key=node?.dataset.objectKey;if(!element||!key)return;element.richText=element.richText||{};element.richText[key]={html:sanitizeRichHtml(node.innerHTML,key==='setting:body'),text:directText(node)};
}
function finishTextFormat(node){
  const selection=window.getSelection(),live=selection?.rangeCount&&textObjectForRange(selection.getRangeAt(0))===node?selection.getRangeAt(0):activeTextRange,bookmark=makeTextBookmark(node,live),safe=sanitizeRichHtml(node.innerHTML,node.dataset.objectKey==='setting:body');if(node.innerHTML!==safe){node.innerHTML=safe;if(bookmark)restoreRangeOffsets(node,bookmark)}else if(live)activeTextRange=live.cloneRange();persistRichTextNode(node);activeTextBookmark=bookmark;node.dispatchEvent(new Event('input',{bubbles:true}));refreshMobileFontScaling();positionObjectSelection();scheduleHistoryCapture();scheduleDraftSave();
}
function restoreTextRange(){
  const node=selectedObjectNode(),range=currentTextRange();if(!node||!range){showToast('请先在文字框中拖选要修改的文字');return null}node.focus({preventScroll:true});const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);return {node,range};
}
function applyTextCommand(command,value=null){
  const target=restoreTextRange();if(!target)return;captureHistoryNow();document.execCommand('styleWithCSS',false,true);document.execCommand(command,false,value);finishTextFormat(target.node);
}
function applyTextSelectionStyle(styles){
  const target=restoreTextRange();if(!target)return;captureHistoryNow();const fragment=target.range.extractContents(),wrapper=document.createElement(fragment.querySelector?.('p,div')?'div':'span');Object.assign(wrapper.style,styles);wrapper.append(fragment);target.range.insertNode(wrapper);const selection=window.getSelection(),next=document.createRange();next.selectNodeContents(wrapper);selection.removeAllRanges();selection.addRange(next);activeTextRange=next.cloneRange();finishTextFormat(target.node);
}
function applyParagraphFormat(field,value){
  const target=restoreTextRange();if(!target)return;captureHistoryNow();if(target.node.classList.contains('detail-body')){const blocks=[...target.node.querySelectorAll(':scope > p,:scope > div')].filter(block=>{try{return target.range.intersectsNode(block)}catch{return false}});if(blocks.length){blocks.forEach(block=>block.style[field]=value);finishTextFormat(target.node);return}}const record=selectedObjectRecord();if(!record)return;paragraphStyle(record.element,selectedObjectRef.key)[field]=value;applyCurrentObjectStyle();scheduleHistoryCapture();scheduleDraftSave();
}
function applySelectedTextFormat(field,value){
  if(field==='fontFamily'){applyTextCommand('fontName',value);return}if(field==='color'){applyTextCommand('foreColor',value);return}if(field==='backgroundColor'){applyTextCommand('hiliteColor',value);return}if(field==='fontSize'){applyTextSelectionStyle({fontSize:`${value}px`});return}if(field==='lineHeight'||field==='textAlign'){applyParagraphFormat(field,value);return}applyTextSelectionStyle({[field]:value});
}

function ensureObjectEditorChrome(){
  const canvas=document.querySelector('#siteCanvas');if(!canvas||document.querySelector('#objectSelection'))return;
  canvas.insertAdjacentHTML('beforeend',`<div class="alignment-guide vertical" id="guideVertical"></div><div class="alignment-guide horizontal" id="guideHorizontal"></div><div class="snap-badge" id="snapBadge">SNAP</div><div class="object-selection" id="objectSelection" hidden>${['top','right','bottom','left'].map(edge=>`<i class="object-move-edge" data-edge="${edge}" aria-hidden="true"></i>`).join('')}${['nw','n','ne','e','se','s','sw','w'].map(handle=>`<i class="resize-handle" data-handle="${handle}"></i>`).join('')}</div>`);
  document.querySelectorAll('#objectSelection .object-move-edge').forEach(edge=>edge.addEventListener('pointerdown',event=>startObjectPointer(event,'move')));
  document.querySelectorAll('#objectSelection .resize-handle').forEach(handle=>handle.addEventListener('pointerdown',event=>startObjectPointer(event,handle.dataset.handle)));
}
function selectedObjectNode(){
  if(!selectedObjectRef)return null;if(selectedObjectRef.pageObjectId)return document.querySelector(`[data-page-object-id="${CSS.escape(selectedObjectRef.pageObjectId)}"]`);return [...document.querySelectorAll('[data-editor-object]')].find(node=>node.dataset.objectKey===selectedObjectRef.key&&node.closest('.page-element')?.dataset.elementId===selectedObjectRef.elementId)||null;
}
function selectedObjectRecord(){
  if(!selectedObjectRef)return null;if(selectedObjectRef.pageObjectId){const object=pageObjects().find(item=>item.id===selectedObjectRef.pageObjectId);return object?{element:object,style:objectStyle(object,'image'),pageObject:object}:null}const element=getElementById(selectedObjectRef.elementId);return element?{element,style:objectStyle(element,selectedObjectRef.key)}:null;
}
function clearGuides(){document.querySelector('#guideVertical')?.classList.remove('visible');document.querySelector('#guideHorizontal')?.classList.remove('visible');document.querySelector('#snapBadge')?.classList.remove('visible')}
function clearObjectSelection(){
  selectedObjectRef=null;activeTextRange=null;activeTextBookmark=null;document.querySelectorAll('.object-selected').forEach(node=>node.classList.remove('object-selected'));const overlay=document.querySelector('#objectSelection');if(overlay)overlay.hidden=true;clearGuides();
}
function positionObjectSelection(){
  const node=selectedObjectNode(),overlay=document.querySelector('#objectSelection'),canvas=document.querySelector('#siteCanvas');if(!node||!overlay||!canvas){if(overlay)overlay.hidden=true;return}
  const rect=node.getBoundingClientRect(),canvasRect=canvas.getBoundingClientRect();overlay.hidden=false;overlay.dataset.kind=selectedObjectRef?.kind||'';overlay.style.left=`${rect.left-canvasRect.left}px`;overlay.style.top=`${rect.top-canvasRect.top}px`;overlay.style.width=`${rect.width}px`;overlay.style.height=`${rect.height}px`;
}
function selectEditorObject(node){
  if(!node?.dataset.objectKey)return;const previous=selectedObjectNode(),pageObjectId=node.dataset.pageObjectId,block=node.closest('.page-element');if(!pageObjectId&&!block)return;selectedObjectRef=pageObjectId?{pageObjectId,key:'image',kind:'image'}:{elementId:block.dataset.elementId,key:node.dataset.objectKey,kind:node.dataset.objectKind||'text'};if(previous!==node||selectedObjectRef.kind!=='text'){activeTextRange=null;activeTextBookmark=null}else if(activeTextRange&&!activeTextRange.startContainer.isConnected)activeTextRange=null;
  document.querySelectorAll('.object-selected').forEach(item=>item.classList.toggle('object-selected',item===node));ensureObjectEditorChrome();positionObjectSelection();document.querySelector('#selectionState').textContent=selectedObjectRef.kind==='text'?'可直接输入文字':'拖动边框移动 · 拖动控制点缩放';
}
function restoreSelectedObject(){
  if(!selectedObjectRef)return;const node=selectedObjectNode();if(node)selectEditorObject(node);else clearObjectSelection();
}
function canvasObjectRect(node,canvasRect){const rect=node.getBoundingClientRect();return {left:rect.left-canvasRect.left,top:rect.top-canvasRect.top,width:rect.width,height:rect.height,right:rect.right-canvasRect.left,bottom:rect.bottom-canvasRect.top,centerX:rect.left-canvasRect.left+rect.width/2,centerY:rect.top-canvasRect.top+rect.height/2}}
function snapTargets(axis,node,canvasRect){
  const canvas=document.querySelector('#siteCanvas');const values=axis==='x'?[0,canvasRect.width/2,canvasRect.width]:[0,canvas.scrollHeight/2,canvas.scrollHeight];
  document.querySelectorAll('[data-editor-object]').forEach(other=>{if(other===node)return;const rect=canvasObjectRect(other,canvasRect);values.push(...(axis==='x'?[rect.left,rect.centerX,rect.right]:[rect.top,rect.centerY,rect.bottom]))});return values;
}
function nearestSnap(points,targets){let best=null;for(const point of points)for(const target of targets){const delta=target-point;if(Math.abs(delta)<=SNAP_DISTANCE&&(!best||Math.abs(delta)<Math.abs(best.delta)))best={delta,target}}return best}
function showGuides(vertical,horizontal,left,top){
  const v=document.querySelector('#guideVertical'),h=document.querySelector('#guideHorizontal'),badge=document.querySelector('#snapBadge');if(vertical!=null){v.style.left=`${vertical}px`;v.classList.add('visible')}else v.classList.remove('visible');if(horizontal!=null){h.style.top=`${horizontal}px`;h.classList.add('visible')}else h.classList.remove('visible');if(vertical!=null||horizontal!=null){badge.style.left=`${Math.max(4,(vertical??left)+6)}px`;badge.style.top=`${Math.max(4,(horizontal??top)+6)}px`;badge.classList.add('visible')}else badge.classList.remove('visible');
}
function startObjectPointer(event,handle){
  if(selectedObjectRef?.kind==='text')return;const node=selectedObjectNode(),record=selectedObjectRecord(),canvas=document.querySelector('#siteCanvas');if(!node||!record||!canvas)return;event.preventDefault();event.stopPropagation();captureHistoryNow();
  const canvasRect=canvas.getBoundingClientRect(),rect=canvasObjectRect(node,canvasRect),style=record.style;objectPointerSession={pointerId:event.pointerId,handle,node,record,canvasRect,startX:event.clientX,startY:event.clientY,rect,startStyle:{x:numberValue(style.x),y:numberValue(style.y),width:numberValue(style.width,rect.width),height:numberValue(style.height,rect.height),rotation:numberValue(style.rotation)},targetsX:snapTargets('x',node,canvasRect),targetsY:snapTargets('y',node,canvasRect),ratio:rect.width/Math.max(1,rect.height)};
  event.currentTarget.setPointerCapture?.(event.pointerId);document.body.classList.add(handle==='move'?'canvas-editing-object':'canvas-resizing-object');document.addEventListener('pointermove',moveObjectPointer);document.addEventListener('pointerup',endObjectPointer,{once:true});document.addEventListener('pointercancel',endObjectPointer,{once:true});
}
function moveObjectPointer(event){
  const s=objectPointerSession;if(!s)return;const dx=event.clientX-s.startX,dy=event.clientY-s.startY,style=s.record.style;let x=s.startStyle.x,y=s.startStyle.y,width=s.startStyle.width,height=s.startStyle.height;let guideX=null,guideY=null;
  if(s.handle==='move'){
    const proposed={left:s.rect.left+dx,top:s.rect.top+dy};const snapX=nearestSnap([proposed.left,proposed.left+s.rect.width/2,proposed.left+s.rect.width],s.targetsX),snapY=nearestSnap([proposed.top,proposed.top+s.rect.height/2,proposed.top+s.rect.height],s.targetsY);x+=dx+(snapX?.delta||0);y+=dy+(snapY?.delta||0);guideX=snapX?.target??null;guideY=snapY?.target??null;
  }else{
    const west=s.handle.includes('w'),east=s.handle.includes('e'),north=s.handle.includes('n'),south=s.handle.includes('s');if(east)width=Math.max(36,s.startStyle.width+dx);if(south)height=Math.max(22,s.startStyle.height+dy);if(west){width=Math.max(36,s.startStyle.width-dx);x=s.startStyle.x+(s.startStyle.width-width)}if(north){height=Math.max(22,s.startStyle.height-dy);y=s.startStyle.y+(s.startStyle.height-height)}
    if(selectedObjectRef?.kind==='image'&&event.shiftKey&&(west||east)&&(north||south)){height=width/s.ratio;if(north)y=s.startStyle.y+(s.startStyle.height-height)}
    const left=s.rect.left+(x-s.startStyle.x),top=s.rect.top+(y-s.startStyle.y),right=left+width,bottom=top+height;if(east||west){const edge=west?left:right;const snap=nearestSnap([edge],s.targetsX);if(snap){guideX=snap.target;if(west){const delta=snap.delta;x+=delta;width-=delta}else width+=snap.delta}}if(north||south){const edge=north?top:bottom;const snap=nearestSnap([edge],s.targetsY);if(snap){guideY=snap.target;if(north){const delta=snap.delta;y+=delta;height-=delta}else height+=snap.delta}}
  }
  Object.assign(style,{x:Math.round(x),y:Math.round(y),width:Math.round(width),height:Math.round(height)});applyCurrentObjectStyle();showGuides(guideX,guideY,s.rect.left,s.rect.top);
}
function endObjectPointer(){
  if(!objectPointerSession)return;objectPointerSession=null;document.body.classList.remove('canvas-editing-object','canvas-resizing-object');document.removeEventListener('pointermove',moveObjectPointer);clearGuides();positionObjectSelection();captureHistoryNow();scheduleDraftSave();
}
function elementContentBounds(block){
  const content=block.querySelector(':scope > .element-content-clip'),blockRect=block.getBoundingClientRect();if(!content)return {top:blockRect.top+12,bottom:blockRect.top+36};let contentTop=Infinity,contentBottom=-Infinity;
  content.querySelectorAll('*').forEach(node=>{const style=getComputedStyle(node);if(style.display==='none'||style.visibility==='hidden')return;const hasOwnContent=node.matches('button,input,textarea,img,video,canvas,svg,[contenteditable]')||[...node.childNodes].some(child=>child.nodeType===Node.TEXT_NODE&&child.textContent.trim());if(!hasOwnContent)return;const rect=node.getBoundingClientRect();if(rect.width<=0||rect.height<=0)return;contentTop=Math.min(contentTop,rect.top);contentBottom=Math.max(contentBottom,rect.bottom)});
  return Number.isFinite(contentTop)?{top:contentTop,bottom:contentBottom}:{top:blockRect.top+12,bottom:blockRect.top+36};
}
function elementMinimumHeight(block){const blockRect=block.getBoundingClientRect(),bounds=elementContentBounds(block);return Math.max(48,Math.ceil(bounds.bottom-blockRect.top+12))}
function enforceElementHeightLimits(){document.querySelectorAll('.page-element[style*="height"]').forEach(block=>{const element=getElementById(block.dataset.elementId);if(!element)return;const minimum=elementMinimumHeight(block);if(numberValue(element.layoutHeight)>=minimum)return;element.layoutHeight=minimum;block.style.height=`${minimum}px`})}
function applyCurrentObjectStyle(){const node=selectedObjectNode(),record=selectedObjectRecord();if(!node||!record)return;node.setAttribute('style',objectCss(record.element,selectedObjectRef.key,selectedObjectRef.kind));refreshMobileFontScaling();positionObjectSelection()}
function applyObjectFormat(field,value){if(selectedObjectRef?.kind==='text'){applySelectedTextFormat(field,value);return}const record=selectedObjectRecord();if(!record)return;record.style[field]=value;applyCurrentObjectStyle();scheduleHistoryCapture();scheduleDraftSave()}
function rgbToHex(color){const match=String(color).match(/\d+(?:\.\d+)?/g);if(!match||match.length<3)return '#151610';return `#${match.slice(0,3).map(value=>Math.max(0,Math.min(255,Math.round(Number(value)))).toString(16).padStart(2,'0')).join('')}`}
// 画布对象选择框的跟随定位与图片替换（顶部文字编辑栏已移除，保留这些基础绑定）
function bindCanvasObjectChrome(){
  document.querySelector('#replaceImageInput').addEventListener('change',replaceSelectedImageFromFile);
  window.addEventListener('resize',()=>{positionObjectSelection();clearGuides()});document.querySelector('#canvasScroll').addEventListener('scroll',positionObjectSelection,{passive:true});
}
function replaceSelectedImage(dataUrl,fileName){
  const record=selectedObjectRecord();if(!record||selectedObjectRef.kind!=='image')return false;const key=selectedObjectRef.key;
  if(record.pageObject){record.pageObject.settings={...(record.pageObject.settings||{}),image:dataUrl,alt:fileName};return true}
  const itemMatch=key.match(/^item:([^:]+):image$/);if(itemMatch){const item=record.element.items?.find(entry=>entry.id===itemMatch[1]);if(!item)return false;item.image=dataUrl;syncItemDetail(item);return true}
  if(key==='setting:image'){record.element.settings={...(record.element.settings||{}),image:dataUrl};syncDetailOwner('image',dataUrl);return true}
  return false;
}
function deleteSelectedObject(){
  const record=selectedObjectRecord();if(!record)return;const key=selectedObjectRef.key;
  if(selectedObjectRef.kind==='image'){
    captureHistoryNow();
    if(record.pageObject){activePage().objects=pageObjects().filter(object=>object.id!==record.pageObject.id);clearObjectSelection();renderCanvas()}
    else {const itemMatch=key.match(/^item:([^:]+):image$/);if(itemMatch){const item=record.element.items?.find(entry=>entry.id===itemMatch[1]);if(item){item.image='';syncItemDetail(item)}}else if(key==='setting:image'){record.element.settings={...(record.element.settings||{}),image:''};syncDetailOwner('image','')}clearObjectSelection();renderCanvas()}
    captureHistoryNow();scheduleDraftSave();showToast('图片已删除');return;
  }
  delete record.element.objectStyles?.[key];applyCurrentObjectStyle();scheduleHistoryCapture();scheduleDraftSave();showToast('已重置文字框位置与尺寸');
}
function replaceSelectedImageFromFile(event){
  const file=event.target.files?.[0];event.target.value='';if(!file)return;if(file.size>8*1024*1024){showToast('图片请控制在 8 MB 以内');return}const reader=new FileReader();reader.onload=()=>{captureHistoryNow();if(!replaceSelectedImage(String(reader.result),file.name))return;renderCanvas();captureHistoryNow();scheduleDraftSave();showToast('图片已替换，位置与尺寸保持不变')};reader.readAsDataURL(file);
}

function bindCanvasEvents(){
  const content=document.querySelector('#canvasContent');content.addEventListener('dragover',event=>{event.preventDefault();showDropIndex(findDropIndex(event.clientY));if(event.dataTransfer)event.dataTransfer.dropEffect=dragPayload?.source==='canvas'?'move':'copy'});content.addEventListener('drop',event=>{event.preventDefault();handleDrop(findDropIndex(event.clientY))});content.addEventListener('dragleave',event=>{if(!content.contains(event.relatedTarget))clearDropHints()});
  content.addEventListener('pointerdown',event=>{if(!event.target.closest('[data-editor-object],.block-tools,[data-inline-editor]'))clearObjectSelection()});
  document.querySelectorAll('[data-editor-object]').forEach(node=>{
    node.addEventListener('pointerdown',()=>selectEditorObject(node));node.addEventListener('focus',()=>selectEditorObject(node));node.addEventListener('click',event=>{selectEditorObject(node);if(node.dataset.objectKind==='image'){event.preventDefault();event.stopPropagation()}});node.addEventListener('dblclick',event=>{if(node.dataset.objectKind==='image'){event.preventDefault();event.stopPropagation();document.querySelector('#replaceImageInput').click()}});
    if(node.dataset.objectKind==='text'){node.addEventListener('input',()=>persistRichTextNode(node));node.addEventListener('keyup',rememberTextSelection);node.addEventListener('pointerup',()=>requestAnimationFrame(rememberTextSelection));node.addEventListener('compositionstart',()=>{isComposingText=true});node.addEventListener('compositionend',()=>{isComposingText=false;persistRichTextNode(node);if(deferredCanvasRender){deferredCanvasRender=false;requestAnimationFrame(renderCanvas)}})}
  });
  document.querySelectorAll('.page-element').forEach(block=>{
    block.addEventListener('dragstart',event=>{if(!event.target.closest('.drag-handle')){event.preventDefault();return}dragPayload={source:'canvas',id:block.dataset.elementId};block.classList.add('dragging');document.body.classList.add('is-dragging');event.dataTransfer.setData('text/plain',block.dataset.elementId);event.dataTransfer.effectAllowed='move'});
    block.addEventListener('dragend',()=>{block.classList.remove('dragging');dragPayload=null;document.body.classList.remove('is-dragging');clearDropHints()});block.addEventListener('contextmenu',event=>{if(event.target.closest('input,textarea,label,[contenteditable]'))return;event.preventDefault();showElementContext(event.clientX,event.clientY,block.dataset.elementId)});
  });
  document.querySelectorAll('[data-direct-setting]').forEach(node=>{
    node.addEventListener('click',event=>event.stopPropagation());
    node.addEventListener('input',()=>{const element=elementForEditorNode(node);if(!element)return;const field=node.dataset.directSetting;const value=directText(node);element.settings={...(element.settings||{}),[field]:value};if(element.type==='detail'){if(field==='title'){activePage().name=value||'未命名详情';renderPages()}syncDetailOwner(field,value)}positionObjectSelection();scheduleDraftSave()});
    node.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();node.blur()}else if(event.key==='Enter'&&node.dataset.directMultiline!=='true'&&!event.shiftKey){event.preventDefault();node.blur()}});
    node.addEventListener('paste',event=>{event.preventDefault();document.execCommand('insertText',false,event.clipboardData?.getData('text/plain')||'')});
  });
  document.querySelectorAll('[data-direct-list-setting]').forEach(node=>{node.addEventListener('click',event=>event.stopPropagation());node.addEventListener('input',()=>{const element=elementForEditorNode(node);if(!element)return;updatePipeSetting(element,node.dataset.directListSetting,Number(node.dataset.directIndex),directText(node),node.dataset.directDefaults||'');positionObjectSelection();scheduleDraftSave()});node.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();node.blur()}});node.addEventListener('paste',event=>{event.preventDefault();document.execCommand('insertText',false,event.clipboardData?.getData('text/plain')||'')})});
  document.querySelectorAll('[data-direct-item-id]').forEach(node=>{node.addEventListener('click',event=>event.stopPropagation());node.addEventListener('input',()=>{const element=elementForEditorNode(node);const item=element?.items?.find(entry=>entry.id===node.dataset.directItemId);if(!item)return;item[node.dataset.directItemField]=directText(node);syncItemDetail(item);positionObjectSelection();scheduleDraftSave()});node.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();node.blur()}});node.addEventListener('paste',event=>{event.preventDefault();document.execCommand('insertText',false,event.clipboardData?.getData('text/plain')||'')})});
  document.querySelectorAll('[data-direct-page-name]').forEach(node=>{node.addEventListener('click',event=>event.stopPropagation());node.addEventListener('input',()=>{const page=state.pages.find(item=>item.id===node.dataset.directPageName);if(!page)return;page.name=directText(node)||'未命名页面';renderPages();scheduleDraftSave()});node.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();node.blur()}})});
  document.querySelectorAll('[data-inline-setting]').forEach(input=>{input.addEventListener('input',()=>{const element=elementForEditorNode(input);if(!element)return;element.settings={...(element.settings||{}),[input.dataset.inlineSetting]:input.value};if(input.dataset.inlineSetting==='titleSize')document.querySelector(`[data-element-id="${element.id}"] .b-hero`)?.style.setProperty('--hero-title-size',cssSize(input.value,'clamp(48px,7vw,102px)'));if(input.dataset.inlineSetting==='background')document.querySelector(`[data-element-id="${element.id}"] .notice-block`)?.style.setProperty('--notice-bg',cssColor(input.value,'var(--page-accent)'));if(input.dataset.inlineSetting==='color')document.querySelector(`[data-element-id="${element.id}"] .notice-block`)?.style.setProperty('--notice-fg',cssColor(input.value,'#141510'));syncDetailOwner(input.dataset.inlineSetting,input.value);refreshMobileFontScaling();scheduleDraftSave()});if(input.dataset.inlineSetting==='image')input.addEventListener('change',renderCanvas)});
  document.querySelectorAll('[data-pipe-setting]').forEach(input=>input.addEventListener('input',()=>{const element=elementForEditorNode(input);if(!element)return;updatePipeSetting(element,input.dataset.pipeSetting,Number(input.dataset.pipeIndex),input.value);scheduleDraftSave()}));
  document.querySelectorAll('[data-nav-page]').forEach(input=>input.addEventListener('change',()=>{const element=elementForEditorNode(input);if(!element)return;const panel=input.closest('[data-inline-editor]');const selected=[...panel.querySelectorAll('[data-nav-page]:checked')].map(item=>item.dataset.navPage);element.settings={...(element.settings||{}),pageIds:selected};renderCanvas()}));
  document.querySelectorAll('[data-item-field]').forEach(input=>input.addEventListener('input',()=>{const element=elementForEditorNode(input);const item=element?.items?.find(entry=>entry.id===editingItemId);if(!item)return;item[input.dataset.itemField]=input.value;syncItemDetail(item);scheduleDraftSave()}));
  document.querySelector('[data-item-upload]')?.addEventListener('change',event=>{const file=event.target.files?.[0];const element=elementForEditorNode(event.target);const item=element?.items?.find(entry=>entry.id===editingItemId);if(!file||!item)return;if(file.size>8*1024*1024){showToast('图片请控制在 8 MB 以内');return}const reader=new FileReader();reader.onload=()=>{item.image=String(reader.result);syncItemDetail(item);renderCanvas()};reader.readAsDataURL(file)});
  document.querySelectorAll('[data-editor-action]').forEach(button=>button.addEventListener('click',event=>{event.stopPropagation();const element=elementForEditorNode(button);const action=button.dataset.editorAction;if(action==='close-inspector'){editingElementId=null;editingItemId=null;renderCanvas()}else if(action==='add-item'&&element){editingElementId=element.id;addCollectionItem(element)}else if(action==='close-item'){editingItemId=null;renderCanvas()}else if(action==='open-detail'&&button.dataset.pageId)selectPage(button.dataset.pageId);else if(action==='delete-item'&&element)deleteCollectionItem(element,editingItemId)}));
}

// 平滑显隐：入场交给 motion.css（解除 hidden 时自动播放），退场先挂 .is-closing 播完再隐藏
const uiMotionOK=()=>!window.matchMedia('(prefers-reduced-motion: reduce)').matches;
function uiShow(node){if(!node)return;clearTimeout(node._uiHideTimer);const wasClosing=node.classList.contains('is-closing');node.classList.remove('is-closing');node.hidden=false;if(wasClosing)uiReplay(node)}
function uiReplay(node){if(!node)return;node.style.animation='none';void node.offsetWidth;node.style.animation=''}
function uiHide(node,onDone){if(!node)return;if(node.hidden){clearTimeout(node._uiHideTimer);onDone&&onDone();return}if(node.classList.contains('is-closing'))return;if(!uiMotionOK()){node.hidden=true;onDone&&onDone();return}node.classList.add('is-closing');const duration=Math.max(120,(parseFloat(getComputedStyle(node).animationDuration)||0)*1000);node._uiHideTimer=setTimeout(()=>{node.classList.remove('is-closing');node.hidden=true;onDone&&onDone()},duration+40)}
function showToast(message){const toast=document.querySelector('#toast');toast.querySelector('span').textContent=message;const wasVisible=!toast.hidden&&!toast.classList.contains('is-closing');uiShow(toast);if(wasVisible)uiReplay(toast);clearTimeout(showToast.timer);showToast.timer=setTimeout(()=>uiHide(toast),2600)}
function openModal(title,html){uiShow(document.querySelector('#previewModal'));const body=document.querySelector('#modalBody');body.innerHTML=`<h2 id="modalTitle">${esc(title)}</h2>${html}`;body.classList.remove('modal-swap');void body.offsetWidth;body.classList.add('modal-swap')}
function closeModal(){uiHide(document.querySelector('#previewModal'),()=>{document.querySelector('#modalBody').innerHTML=''})}
function showElementContext(x,y,id){contextElementId=id;const menu=document.querySelector('#elementContextMenu');uiShow(menu);menu.style.left=`${Math.min(x,window.innerWidth-155)}px`;menu.style.top=`${Math.min(y,window.innerHeight-135)}px`}
function hideElementContext(){const menu=document.querySelector('#elementContextMenu');if(menu&&!menu.hidden)uiHide(menu);contextElementId=null}
function openThread(post){openModal(post.title,`<p>${esc(post.author)} · 刚刚</p><p>${esc(post.body)}</p><div class="reply-list">${post.replies.length?post.replies.map(reply=>`<div class="reply"><b>${esc(reply.author)}</b><p>${esc(reply.body)}</p></div>`).join(''):'<div class="reply"><p>还没有回复，来坐第一排。</p></div>'}</div><form class="runtime-form" data-runtime-form="reply" data-post-id="${post.id}"><input name="author" placeholder="你的昵称" required><textarea name="body" rows="3" placeholder="写下回复……" required></textarea><button>发布回复</button></form>`)}

document.querySelector('#siteCanvas').addEventListener('click',event=>{
  if(event.target.closest('[contenteditable]'))return;
  const target=event.target.closest('[data-preview-action]');if(!target)return;const action=target.dataset.previewAction;
  if(action==='navigate'){selectPage(target.dataset.pageId);return}
  if(action==='project'||action==='article'){const block=target.closest('.page-element');if(block){editingElementId=block.dataset.elementId;editingItemId=target.dataset.itemId;renderCanvas()}return}
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

document.querySelectorAll('[data-theme]').forEach(button=>button.addEventListener('click',()=>{state.theme=button.dataset.theme;document.querySelectorAll('[data-theme]').forEach(item=>{const active=item===button;item.classList.toggle('active',active);item.setAttribute('aria-checked',String(active))});renderCanvas()}));
document.querySelectorAll('[data-bg]').forEach(button=>button.addEventListener('click',()=>{state.background=button.dataset.bg;document.querySelectorAll('[data-bg]').forEach(item=>item.classList.toggle('active',item===button));renderCanvas()}));
const contentWidthInput=document.querySelector('#contentWidth');const widthValueLabel=document.querySelector('#widthValue');contentWidthInput?.addEventListener('input',event=>{state.contentWidth=Number(event.target.value);if(widthValueLabel)widthValueLabel.textContent=`${state.contentWidth}%`;renderCanvas()});
document.querySelector('#clearPageBtn').addEventListener('click',()=>{const page=activePage();if(!page.code.trim()&&!pageObjects().length&&!page.elements.length){showToast('当前页面已经是空白的');return}cleanupElementDetails(page.elements);page.elements=[];page.code='';page.objects=[];editingElementId=null;clearObjectSelection();renderPages();renderCanvas();showToast('当前页面已清空')});document.querySelector('#toast button').addEventListener('click',()=>uiHide(document.querySelector('#toast')));
document.querySelectorAll('.ai-examples button').forEach(button=>button.addEventListener('click',()=>{const prompt=document.querySelector('#aiPrompt');prompt.value=button.textContent;prompt.focus()}));
const AI_ATTACHMENT_LIMIT=4,AI_ATTACHMENT_TOTAL_LIMIT=8*1024*1024,AI_IMAGE_LIMIT=4*1024*1024,AI_TEXT_LIMIT=512*1024;
const AI_TEXT_EXTENSIONS=new Set(['txt','md','json','csv','html','css','js','mjs','ts','tsx','jsx','py','yml','yaml','xml','svg']);
let aiAttachments=[];
const formatAiFileSize=size=>size<1024?`${size} B`:size<1024*1024?`${Math.ceil(size/1024)} KB`:`${(size/1024/1024).toFixed(1)} MB`;
const aiFileExtension=name=>String(name).split('.').pop().toLowerCase();
function renderAiAttachments(){updateAiContextRing();
  const strip=document.querySelector('#aiAttachmentStrip');strip.hidden=!aiAttachments.length;strip.innerHTML=aiAttachments.map(item=>`<article class="ai-attachment-chip">${item.kind==='image'?`<img src="${esc(item.content)}" alt="">`:`<i>${esc(aiFileExtension(item.name).slice(0,4).toUpperCase()||'FILE')}</i>`}<span><b>${esc(item.name)}</b><small>${formatAiFileSize(item.size)}</small></span><button type="button" data-ai-remove-attachment="${esc(item.id)}" aria-label="移除 ${esc(item.name)}">×</button></article>`).join('');document.querySelector('#aiAttachBtn').title=aiAttachments.length?`已添加 ${aiAttachments.length}/${AI_ATTACHMENT_LIMIT} 个附件 · ${formatAiFileSize(aiAttachments.reduce((sum,item)=>sum+item.size,0))}`:'添加图片或文件';
}
async function addAiAttachments(files){
  for(const file of files){
    if(aiAttachments.length>=AI_ATTACHMENT_LIMIT){showToast(`一次最多添加 ${AI_ATTACHMENT_LIMIT} 个附件`);break}
    const extension=aiFileExtension(file.name);const isImage=file.type.startsWith('image/')&&file.type!=='image/svg+xml';const isText=file.type.startsWith('text/')||AI_TEXT_EXTENSIONS.has(extension);
    if(!isImage&&!isText){showToast(`不支持 ${file.name} 的文件格式`);continue}
    const limit=isImage?AI_IMAGE_LIMIT:AI_TEXT_LIMIT;if(file.size>limit){showToast(`${file.name} 超过 ${formatAiFileSize(limit)} 限制`);continue}
    if(aiAttachments.reduce((sum,item)=>sum+item.size,0)+file.size>AI_ATTACHMENT_TOTAL_LIMIT){showToast('附件总大小不能超过 8 MB');break}
    const content=isImage?await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result));reader.onerror=()=>reject(new Error('图片读取失败'));reader.readAsDataURL(file)}):await file.text();
    aiAttachments.push({id:uid('attachment'),name:file.name,type:file.type||(isImage?'image/jpeg':'text/plain'),size:file.size,kind:isImage?'image':'text',content});
  }
  renderAiAttachments();scrollAiConversation();
}
document.querySelector('#aiAttachBtn').addEventListener('click',()=>document.querySelector('#aiFileInput').click());
document.querySelector('#aiFileInput').addEventListener('change',async event=>{try{await addAiAttachments([...event.target.files])}catch(error){showToast(error.message)}finally{event.target.value=''}});
document.querySelector('#aiAttachmentStrip').addEventListener('click',event=>{const button=event.target.closest('[data-ai-remove-attachment]');if(!button)return;aiAttachments=aiAttachments.filter(item=>item.id!==button.dataset.aiRemoveAttachment);renderAiAttachments()});
const aiComposer=document.querySelector('#aiComposer');aiComposer.addEventListener('dragover',event=>{if([...(event.dataTransfer?.types||[])].includes('Files')){event.preventDefault();aiComposer.classList.add('is-file-over')}});aiComposer.addEventListener('dragleave',()=>aiComposer.classList.remove('is-file-over'));aiComposer.addEventListener('drop',async event=>{if(!event.dataTransfer?.files?.length)return;event.preventDefault();aiComposer.classList.remove('is-file-over');try{await addAiAttachments([...event.dataTransfer.files])}catch(error){showToast(error.message)}});
document.querySelector('#aiPrompt').addEventListener('input',event=>{event.target.style.height='auto';event.target.style.height=`${Math.min(event.target.scrollHeight,180)}px`;updateAiContextRing()});
function aiContextUsage(){
  // 粗估本轮会进模型的上下文体量：站点快照 + 已输入 prompt + 对话历史 + 附件文本
  let chars=0;try{chars+=JSON.stringify(aiSafeSnapshot()).length}catch{}
  chars+=(document.querySelector('#aiPrompt')?.value||'').length;
  chars+=aiChatHistoryForContext().reduce((sum,item)=>sum+(item.text||'').length,0);
  chars+=aiAttachments.reduce((sum,item)=>sum+(item.kind==='text'?(item.content||'').length:4000),0);
  return Math.min(1,chars/120000);
}
function updateAiContextRing(){
  const ring=document.querySelector('#aiContextRing');if(!ring)return;
  const used=aiContextUsage();
  const circumference=69.12;
  ring.style.strokeDashoffset=String(circumference*(1-Math.max(used,0.02)));
  // 参照各 harness 的做法：环形指示常驻显示在发送钮旁边，随上下文用量填充，快满时变红
  ring.style.stroke=used>=0.8?'#f85149':'#58a6ff';
  const wrap=document.querySelector('#aiContextRingWrap');
  if(wrap){wrap.classList.add('is-visible');wrap.setAttribute('title',`本轮上下文约占 ${Math.round(used*100)}%（站点快照 + 对话历史 + 附件）`)}
}
document.querySelector('#aiPrompt').addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();document.querySelector('#aiAdjustBtn').click()}});
document.querySelector('#aiChatMessages').addEventListener('click',event=>{const execute=event.target.closest('[data-ai-plan-execute]');if(!execute)return;if(!aiServiceConfigured){showToast('AI 尚未配置：请管理员在「管理后台 → AI 服务配置」中添加 Kimi API Key');return}const plan=execute.closest('.ai-plan-box')?.querySelector('p')?.textContent.trim();if(!plan)return;void startAiRun(`请严格按照以下已确认的计划修改网站（本次为完全访问模式，直接执行，不要再只出计划）：\n${plan}`,[],[],[],{mode:'full',displayText:'按此计划执行'});execute.disabled=true});
document.querySelector('#undoBtn').addEventListener('click',undoState);document.querySelector('#redoBtn').addEventListener('click',redoState);
document.querySelectorAll('[data-preview-device]').forEach(button=>button.addEventListener('click',()=>{previewDevice=button.dataset.previewDevice;sessionStorage.setItem('alchemyhatchery:preview-device',previewDevice);renderCanvas();showToast(`已切换到${button.textContent}预览`)}));
document.addEventListener('keydown',event=>{const editingText=event.target.closest?.('input,textarea,[contenteditable]');if(editingText)return;if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='z'){event.preventDefault();if(event.shiftKey)redoState();else undoState()}else if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='y'){event.preventDefault();redoState()}else if(event.key==='Escape'&&editingElementId)openElementEditor(editingElementId)});

let lastAiUndo=null; // {proposalId,state,sourceApplied,sessionId}：按钮只在发起会话里显示，但撤销的是全局网站状态
// AI 任务注册表：每个任务绑定一个会话，进度按会话路由渲染；不同会话的任务并行，互不串扰
const aiRuns=new Map();
const aiSessionQueues=new Map();
const aiSessionHtmlCache=new Map();
const aiSessionDirty=new Set();
const aiSessionTitles=new Map();
const aiSessionDeleted=new Set();
let aiRunLocalSeq=0;
let aiServiceConfigured=true;
function activeAiRuns(){return [...aiRuns.values()].filter(run=>run.status==='running')}
function anyAiRunActive(){return activeAiRuns().length>0}
function activeRunForSession(sessionId){return activeAiRuns().find(run=>run.sessionId===sessionId)||null}
function updateAiBusy(){document.body.classList.toggle('ai-busy',anyAiRunActive());document.body.classList.toggle('ai-session-busy',!!activeRunForSession(aiChatSessionId))}
function aiRunShownText(prompt,chosenPresets=[],displayText=''){return displayText||(chosenPresets.length?`使用这些模块制作：${chosenPresets.map(type=>elementCatalog[type]?.name||type).join('、')}`:prompt)}
function newAiSessionId(){
  if(crypto.randomUUID)return crypto.randomUUID().toLowerCase();
  return '10000000-1000-4000-8000-100000000000'.replace(/[018]/g,c=>(+c^crypto.getRandomValues(new Uint8Array(1))[0]&15>>+c/4).toString(16));
}
function ensureAiSessionId(){
  if(aiChatSessionId)return aiChatSessionId;
  // 发消息即分配会话 id：AI 任务从启动起就绑定会话，不再等首次落库
  const id=newAiSessionId();
  aiChatSessionId=id;
  aiSessionDirty.add(id);
  history.replaceState(null,'',`/c/${id}`);
  renderAiSessionList();
  return id;
}
function enqueueAiRun(sessionId,item){
  appendAiChatMessage('user',item.shownText,item.attachments);
  if(!aiSessionTitles.has(sessionId))aiSessionTitles.set(sessionId,item.shownText.slice(0,30)||'新聊天');
  const queue=aiSessionQueues.get(sessionId)||[];aiSessionQueues.set(sessionId,queue);
  const last=document.querySelector('#aiChatMessages')?.lastElementChild;const tag=document.createElement('small');tag.className='ai-queue-tag';tag.textContent=`排队中（第 ${queue.length+1} 位）`;last?.querySelector('div')?.append(tag);
  queue.push({...item,tag});
  showToast('当前会话的 AI 任务进行中，已排队');
}
function newAiRunRecord(sessionId,fields={}){const run={id:`run-${Date.now()}-${++aiRunLocalSeq}`,jobId:null,sessionId,mode:fields.mode||aiPermissionMode,startedAt:Date.now(),prompt:fields.prompt||'',shownText:fields.shownText||'',attachments:fields.attachments||[],chosenPresets:fields.chosenPresets||[],presetSnippets:fields.presetSnippets||[],history:fields.history||null,messageAppended:Boolean(fields.messageAppended),resumed:false,status:'running',snapshot:{status:'running',events:[]},payload:null};aiRuns.set(run.id,run);renderAiSessionList();return run}
function runQueuedAiTask(sessionId,item){
  if(item.tag)item.tag.textContent='执行中';
  const run=newAiRunRecord(sessionId,{...item,messageAppended:true});
  void executeAiRun(run);
}
const AI_MODE_LABELS={chat:'仅聊天',plan:'plan模式',full:'完全访问'};
let aiPermissionMode='full';
function loadAiPermissionMode(){try{const saved=localStorage.getItem(AI_MODE_KEY);if(saved&&AI_MODE_LABELS[saved])aiPermissionMode=saved}catch{}renderAiPermissionMode()}
function renderAiPermissionMode(){const chip=document.querySelector('#aiModeBtn');if(!chip)return;chip.classList.remove('mode-chat','mode-plan','mode-full');chip.classList.add(`mode-${aiPermissionMode}`);chip.setAttribute('aria-expanded',String(!document.querySelector('#aiModeMenu')?.hidden));document.querySelector('#aiModeLabel').textContent=AI_MODE_LABELS[aiPermissionMode];document.querySelectorAll('#aiModeMenu [data-ai-mode]').forEach(item=>item.classList.toggle('active',item.dataset.aiMode===aiPermissionMode))}
function setAiPermissionMode(mode){if(!AI_MODE_LABELS[mode])return;aiPermissionMode=mode;try{localStorage.setItem(AI_MODE_KEY,mode)}catch{}renderAiPermissionMode()}
function closeAiModeMenu(){uiHide(document.querySelector('#aiModeMenu'));document.querySelector('#aiModeBtn')?.setAttribute('aria-expanded','false');uiHide(document.querySelector('#aiModelMenu'));document.querySelector('#aiModelBtn')?.setAttribute('aria-expanded','false')}
document.querySelector('#aiModeBtn')?.addEventListener('click',event=>{event.stopPropagation();const menu=document.querySelector('#aiModeMenu');if(!menu)return;const open=menu.hidden||menu.classList.contains('is-closing');closeAiModeMenu();if(open){uiShow(menu);document.querySelector('#aiModeBtn').setAttribute('aria-expanded','true')}});
document.querySelector('#aiModeMenu')?.addEventListener('click',event=>{const item=event.target.closest('[data-ai-mode]');if(!item)return;setAiPermissionMode(item.dataset.aiMode);closeAiModeMenu();showToast(`AI 权限模式：${AI_MODE_LABELS[aiPermissionMode]}`)});
document.addEventListener('click',event=>{if(!event.target.closest('.ai-mode-wrap'))closeAiModeMenu()});
loadAiPermissionMode();
updateAiContextRing();
// 模型与推理档位选择：菜单项来自 /api/ai/status（后端按提供方返回真实可用列表）
let aiModelChoice={model:'',effort:''};
let aiServiceCatalog={models:[],efforts:[],currentModel:'',currentEffort:''};
function loadAiModelChoice(){try{const saved=JSON.parse(localStorage.getItem(AI_MODEL_KEY)||'null');if(saved&&typeof saved==='object')aiModelChoice={model:String(saved.model||''),effort:String(saved.effort||'')}}catch{}renderAiModelChoice()}
function currentAiModelId(){return aiModelChoice.model||aiServiceCatalog.currentModel||''}
function currentAiEffortId(){return aiModelChoice.effort||aiServiceCatalog.currentEffort||''}
function renderAiModelChoice(){const label=document.querySelector('#aiModelLabel');if(!label)return;label.textContent=currentAiModelId()||'默认';const eff=aiServiceCatalog.efforts.find(item=>item.id===currentAiEffortId());const small=document.querySelector('#aiEffortLabel');if(small)small.textContent=eff?eff.name:'默认';buildAiModelMenu()}
function buildAiModelMenu(){const menu=document.querySelector('#aiModelMenu');if(!menu)return;const mId=currentAiModelId(),eId=currentAiEffortId();const modelRows=(aiServiceCatalog.models||[]).map(item=>`<button type="button" data-ai-model="${esc(item.id)}" class="${item.id===mId?'active':''}"><b>${esc(item.name||item.id)}</b></button>`).join('');const effortRows=(aiServiceCatalog.efforts||[]).map(item=>`<button type="button" data-ai-effort="${esc(item.id)}" class="${item.id===eId?'active':''}"><b>${esc(item.name)}</b></button>`).join('');menu.innerHTML=(modelRows?`<div class="ai-menu-group">模型</div>${modelRows}`:'')+(effortRows?`<div class="ai-menu-group">推理档位</div>${effortRows}`:'')}
document.querySelector('#aiModelBtn')?.addEventListener('click',event=>{event.stopPropagation();const menu=document.querySelector('#aiModelMenu');if(!menu)return;const open=menu.hidden||menu.classList.contains('is-closing');closeAiModeMenu();if(open){buildAiModelMenu();uiShow(menu);document.querySelector('#aiModelBtn').setAttribute('aria-expanded','true')}});
document.querySelector('#aiModelMenu')?.addEventListener('click',event=>{const mItem=event.target.closest('[data-ai-model]'),eItem=event.target.closest('[data-ai-effort]');if(mItem)aiModelChoice.model=mItem.dataset.aiModel;else if(eItem)aiModelChoice.effort=eItem.dataset.aiEffort;else return;try{localStorage.setItem(AI_MODEL_KEY,JSON.stringify(aiModelChoice))}catch{}renderAiModelChoice();closeAiModeMenu()});
loadAiModelChoice();
void loadAiStatus();
// ChatGPT 空状态：没有任何用户消息/运行卡片时，输入框与大标题居中
function updateAiEmptyState(){const messages=document.querySelector('#aiChatMessages'),panel=document.querySelector('.ai-chat-panel');if(!messages||!panel)return;const hasUser=!!messages.querySelector('.ai-message.user');const card=document.querySelector('#aiRunCard'),result=document.querySelector('#aiResult');panel.classList.toggle('ai-empty',!(hasUser||(card&&!card.hidden)||(result&&!result.hidden)));const examples=document.querySelector('.ai-examples');if(examples)examples.hidden=hasUser}
if(document.querySelector('#aiChatMessages'))new MutationObserver(updateAiEmptyState).observe(document.querySelector('#aiChatMessages'),{childList:true});
updateAiEmptyState();
const cloneJson=value=>JSON.parse(JSON.stringify(value));
const AI_IMAGE_PLACEHOLDER='[本地图片数据已省略，但必须保留原值]';
const AI_OBJECT_PLACEHOLDER='[页面浮动图片数据已省略，但必须保留原值]';
const AI_PLACEHOLDERS=new Set([AI_IMAGE_PLACEHOLDER,AI_OBJECT_PLACEHOLDER]);
let aiChatHistoryTimer=null;
// 初始消息区快照（问候语 + 结果/运行卡片/撤销按钮的干净副本），用于新聊天重置
const AI_CHAT_INITIAL_HTML=document.querySelector('#aiChatMessages')?.innerHTML||'';
function aiChatHistoryKey(){return `alchemyhatchery:${currentConsoleUser?.username||'guest'}:ai-chat:v1`}
function aiSessionIdFromPath(){const match=location.pathname.match(/^\/c\/([0-9a-fA-F-]{32,36})\/?$/);return match?match[1]:null}
function firstAiUserText(){return (document.querySelector('#aiChatMessages .ai-message.user p')?.textContent||'').trim()}
function persistAiChatHistory(){
  if(!currentConsoleUser)return;
  clearTimeout(aiChatHistoryTimer);
  // 任务进行中轮询每 500ms 触发一次保存调用，放宽防抖避免大 html 高频上传挤占链路
  aiChatHistoryTimer=setTimeout(()=>{void saveAiChatSession()},anyAiRunActive()?2500:400);
}
function cacheAiSessionHtml(id,html){
  if(!id)return;
  aiSessionHtmlCache.delete(id);aiSessionHtmlCache.set(id,html);
  for(const key of aiSessionHtmlCache.keys()){
    if(aiSessionHtmlCache.size<=30)break;
    if(key===aiChatSessionId||activeRunForSession(key))continue;
    aiSessionHtmlCache.delete(key);
  }
}
function aiSessionFirstUserText(sessionId){
  const html=aiSessionHtmlCache.get(sessionId);if(!html)return '';
  const doc=document.createElement('div');doc.innerHTML=html;
  return (doc.querySelector('.ai-message.user p')?.textContent||'').trim();
}
async function persistAiSessionHtml(sessionId,html){
  if(!sessionId||aiSessionDeleted.has(sessionId))return;
  if(aiSessionDirty.has(sessionId)){
    const title=(aiSessionTitles.get(sessionId)||aiSessionFirstUserText(sessionId).slice(0,30)||'新聊天').slice(0,30)||'新聊天';
    await consoleRequest('/api/ai/sessions',{method:'POST',body:{id:sessionId,title,messagesHtml:html}});
    aiSessionDirty.delete(sessionId);
    void refreshAiSessionList();
    return;
  }
  try{
    await consoleRequest(`/api/ai/sessions/${sessionId}`,{method:'POST',body:{messagesHtml:html}});
  }catch(error){
    if(error.status===404&&!aiSessionDeleted.has(sessionId)){aiSessionDirty.add(sessionId);await persistAiSessionHtml(sessionId,html);return}
    throw error;
  }
}
async function saveAiChatSession(){
  if(aiSessionSaving){clearTimeout(aiChatHistoryTimer);aiChatHistoryTimer=setTimeout(()=>{void saveAiChatSession()},800);return}
  const messages=document.querySelector('#aiChatMessages');if(!messages||!currentConsoleUser)return;
  const oldest=()=>messages.querySelector(':scope > .ai-message, :scope > .ai-run-archive');
  while(messages.innerHTML.length>260000&&oldest())oldest().remove();
  if(!messages.querySelector('.ai-message.user'))return;// 还没有实质内容：保持“新聊天”不落库
  const sessionId=aiChatSessionId;if(!sessionId)return;
  const html=messages.innerHTML;
  cacheAiSessionHtml(sessionId,html);
  aiSessionSaving=true;
  try{await persistAiSessionHtml(sessionId,html)}catch{/* 网络失败时静默，下一次编辑会再试 */}
  finally{aiSessionSaving=false}
}
function applyAiChatHtml(html){
  aiChatViewEpoch+=1;
  const messages=document.querySelector('#aiChatMessages');if(!messages)return;
  messages.innerHTML=html;
  messages.querySelectorAll('.ai-preset-chip, .ai-preset-confirm').forEach(node=>{node.disabled=true});
  const trace=messages.querySelector('#aiRunTrace');if(trace)trace.dataset.rendered=String(trace.childElementCount);
  const runningRun=activeRunForSession(aiChatSessionId);
  if(!runningRun){const card=messages.querySelector('#aiRunCard');card?.querySelector('.ai-working-status')?.remove();card?.classList.remove('is-running')}
  const undoBtn=messages.querySelector('#aiUndoBtn');if(undoBtn)undoBtn.hidden=!(lastAiUndo&&lastAiUndo.sessionId===aiChatSessionId);
  updateAiEmptyState();
  updateAiContextRing();
  updateAiBusy();
  if(runningRun)renderAiRunProgress(runningRun.snapshot,runningRun);
}
function resetAiChatView(){
  applyAiChatHtml(AI_CHAT_INITIAL_HTML);
  const conversation=document.querySelector('#aiConversation');if(conversation)conversation.scrollTop=0;
}
function clearAiComposer(){
  const prompt=document.querySelector('#aiPrompt');if(prompt){prompt.value='';prompt.style.height='auto'}
  aiAttachments=[];renderAiAttachments();
  updateAiContextRing();
}
function renderAiSessionList(){
  const list=document.querySelector('#aiSessionList');if(!list)return;
  const items=[...aiSessionList];
  // 刚发消息、还没落库的会话也要立刻出现在列表里
  if(aiChatSessionId&&!items.some(item=>item.id===aiChatSessionId)&&!aiSessionDeleted.has(aiChatSessionId))items.unshift({id:aiChatSessionId,title:aiSessionTitles.get(aiChatSessionId)||'新聊天'});
  list.innerHTML=items.map(item=>{const running=!!activeRunForSession(item.id);return `<button type="button" class="ai-session-item${item.id===aiChatSessionId?' active':''}" data-ai-session="${esc(item.id)}"><span>${esc(item.title||'新聊天')}</span>${running?'<em class="ai-session-spin" title="AI 任务进行中"></em>':''}<i data-ai-session-delete="${esc(item.id)}" title="删除会话" aria-label="删除会话">×</i></button>`}).join('')||'<p class="ai-session-empty">还没有历史会话</p>';
}
async function refreshAiSessionList(){
  if(!currentConsoleUser)return;
  try{const payload=await consoleRequest('/api/ai/sessions');aiSessionList=payload.sessions||[]}catch{aiSessionList=[]}
  renderAiSessionList();
}
function leaveCurrentAiSession(){
  // 切走前把当前会话的 DOM 固化进缓存并落库，保证另一个会话的任务/消息绝不写进来
  clearTimeout(aiChatHistoryTimer);
  const fromId=aiChatSessionId;
  if(!fromId||aiSessionDeleted.has(fromId))return;
  const messages=document.querySelector('#aiChatMessages');if(!messages)return;
  finishAiSummaryTyping(messages);
  const html=messages.innerHTML;
  cacheAiSessionHtml(fromId,html);
  if(messages.querySelector('.ai-message.user'))void persistAiSessionHtml(fromId,html).catch(()=>{});
}
async function loadAiChatSession(id,{silent=false}={}){
  if(id===aiChatSessionId)return;
  leaveCurrentAiSession();
  const cached=aiSessionHtmlCache.get(id);
  if(cached!==undefined){
    aiChatSessionId=id;
    applyAiChatHtml(cached);
    clearAiComposer();
    renderAiSessionList();
    scrollAiConversation(true);
    void syncAiChatSessionFromServer(id);
    return;
  }
  try{
    const payload=await consoleRequest(`/api/ai/sessions/${id}`);
    if(aiSessionIdFromPath()!==id)return;// 等待期间用户又切到别的会话
    const html=payload.session.messagesHtml||AI_CHAT_INITIAL_HTML;
    cacheAiSessionHtml(id,html);
    aiChatSessionId=payload.session.id;
    applyAiChatHtml(html);
    clearAiComposer();
    renderAiSessionList();
    scrollAiConversation(true);
  }catch(error){
    if(!silent)showToast(error.status===404?'会话不存在或已删除':`会话加载失败：${error.message}`);
    if(aiSessionIdFromPath()===id)startNewAiChat('replace');
  }
}
async function syncAiChatSessionFromServer(id){
  // 命中缓存后后台校对一次：别的设备更新过就静默刷新
  const epoch=aiChatViewEpoch;
  const cached=aiSessionHtmlCache.get(id);
  try{
    const payload=await consoleRequest(`/api/ai/sessions/${id}`);
    if(activeRunForSession(id))return;// 进行中的任务持有更新的状态，DB 版本反而更旧
    const fresh=payload.session.messagesHtml||AI_CHAT_INITIAL_HTML;
    cacheAiSessionHtml(id,fresh);
    if(id!==aiChatSessionId||epoch!==aiChatViewEpoch)return;
    if(fresh!==cached)applyAiChatHtml(fresh);
  }catch{}
}
async function prefetchAiSessionHtml(){
  if(!currentConsoleUser)return;
  for(const item of aiSessionList.slice(0,5)){
    if(aiSessionHtmlCache.has(item.id))continue;
    try{const payload=await consoleRequest(`/api/ai/sessions/${item.id}`);if(!aiSessionHtmlCache.has(item.id)&&!activeRunForSession(item.id))cacheAiSessionHtml(item.id,payload.session.messagesHtml||'')}catch{}
  }
}
function startNewAiChat(nav='push'){
  leaveCurrentAiSession();
  aiChatSessionId=null;
  resetAiChatView();
  clearAiComposer();
  renderAiSessionList();
  if(nav==='push')history.pushState(null,'','/');
  else if(nav==='replace')history.replaceState(null,'','/');
  document.querySelector('#aiPrompt')?.focus();
}
async function deleteAiChatSession(id){
  try{await consoleRequest(`/api/ai/sessions/${id}/delete`,{method:'POST',body:{}})}catch(error){showToast(error.message);return}
  aiSessionDeleted.add(id);
  aiSessionHtmlCache.delete(id);aiSessionTitles.delete(id);aiSessionDirty.delete(id);aiSessionQueues.delete(id);
  aiSessionList=aiSessionList.filter(item=>item.id!==id);
  renderAiSessionList();
  if(id===aiChatSessionId)startNewAiChat('replace');
  showToast('会话已删除');
}
async function initAiChatSessions(){
  try{localStorage.removeItem(aiChatHistoryKey())}catch{}// 旧版本地聊天记录缓存不再使用
  const id=aiSessionIdFromPath();
  void refreshAiSessionList().then(()=>void prefetchAiSessionHtml());
  if(id)await loadAiChatSession(id,{silent:true});
  else{aiChatSessionId=null;resetAiChatView();renderAiSessionList()}
}
document.querySelector('#aiSessionList')?.addEventListener('click',event=>{
  const del=event.target.closest('[data-ai-session-delete]');
  if(del){event.stopPropagation();void deleteAiChatSession(del.dataset.aiSessionDelete);return}
  const item=event.target.closest('[data-ai-session]');
  if(!item)return;
  switchActivity('ai');
  const id=item.dataset.aiSession;
  if(id===aiChatSessionId)return;
  history.pushState(null,'',`/c/${id}`);
  void loadAiChatSession(id);
});
document.querySelector('#aiNewChatBtn')?.addEventListener('click',()=>{
  switchActivity('ai');
  if(!aiChatSessionId&&!firstAiUserText()){document.querySelector('#aiPrompt')?.focus();return}
  startNewAiChat();
});
window.addEventListener('popstate',()=>{
  if(!currentConsoleUser)return;
  const id=aiSessionIdFromPath();
  if(id&&id!==aiChatSessionId)void loadAiChatSession(id,{silent:true});
  else if(!id&&aiChatSessionId)startNewAiChat('none');
});
function scrollAiConversation(force=false){const node=document.querySelector('#aiConversation');if(!force&&node.scrollHeight-node.scrollTop-node.clientHeight>120){persistAiChatHistory();return}requestAnimationFrame(()=>{node.scrollTop=node.scrollHeight});persistAiChatHistory()}
let previewManualState=true;// 刷新后默认收起网站预览，本次会话内手动展开/收起后保持选择
let previewAnimTimer=null;
function updatePreviewVisibility(){
  if(previewAnimTimer)return;
  const hasContent=state.pages.some(page=>(page.code||'').trim().length>0||(page.elements||[]).length>0||(page.objects||[]).length>0);
  const hidden=previewManualState??!hasContent;
  document.querySelector('.app-shell')?.classList.toggle('preview-hidden',hidden);
}
function togglePreviewCollapsed(){
  const shell=document.querySelector('.app-shell');if(!shell)return;
  const hidden=shell.classList.contains('preview-hidden');
  clearTimeout(previewAnimTimer);
  if(hidden){
    // 展开：先回到双栏布局，再从压缩态滑入
    previewManualState=false;
    shell.classList.remove('preview-hidden');
    shell.classList.add('preview-expanding','preview-settling');
    requestAnimationFrame(()=>requestAnimationFrame(()=>shell.classList.remove('preview-expanding')));
    previewAnimTimer=setTimeout(()=>{previewAnimTimer=null;shell.classList.remove('preview-settling');updatePreviewVisibility()},560);
  }else{
    // 收起：工作区压缩右滑淡出后，再切到居中布局
    previewManualState=true;
    shell.classList.add('preview-collapsing');
    previewAnimTimer=setTimeout(()=>{
      shell.classList.remove('preview-collapsing');shell.classList.add('preview-hidden','preview-settling');updatePreviewVisibility();
      previewAnimTimer=setTimeout(()=>{previewAnimTimer=null;shell.classList.remove('preview-settling')},380);
    },500);
  }
}
document.querySelector('#previewCollapseBtn').addEventListener('click',togglePreviewCollapsed);
document.querySelector('#previewExpandFab').addEventListener('click',togglePreviewCollapsed);
function appendAiChatMessage(role,text,attachments=[]){const messages=document.querySelector('#aiChatMessages');const files=attachments.length?`<div class="ai-message-files">${attachments.map(item=>`<span>${item.kind==='image'?'▧':'▤'} ${esc(item.name)}</span>`).join('')}</div>`:'';if(role==='user')messages.querySelector('.ai-greeting')?.remove();messages.insertAdjacentHTML('beforeend',`<article class="ai-message ${role==='user'?'user':'assistant'}"><div><p>${esc(text)}</p>${files}</div></article>`);scrollAiConversation(true)}
function aiSafeSnapshot(){
  ensureAllPagesCode();
  const snapshot=cloneJson(state);
  snapshot.pages.forEach(page=>{if(typeof page.code==='string')page.code=page.code.replace(/data:image\/[^"'\s)]+/g,AI_IMAGE_PLACEHOLDER);(page.elements||[]).forEach(element=>{(element.items||[]).forEach(item=>{if(String(item.image||'').startsWith('data:'))item.image=AI_IMAGE_PLACEHOLDER });if(String(element.settings?.image||'').startsWith('data:'))element.settings.image=AI_IMAGE_PLACEHOLDER});(page.objects||[]).forEach(object=>{if(String(object.settings?.image||'').startsWith('data:'))object.settings.image=AI_OBJECT_PLACEHOLDER})});
  return {site:snapshot,activePageId:state.activePageId,pageFormat:'pages[].code 是整页 HTML，直接整体修改；pages[].elements 仅旧数据兼容，通常为空',elementTypes:Object.keys(elementCatalog),elementCatalog:Object.entries(elementCatalog).filter(([type])=>type!=='detail').map(([type,item])=>({type,name:item.name})),rules:['未明确要求时保留全部现有内容与 ID','预览交互数据刷新后清空','发布站点使用服务端持久化']};
}
function syncEditorAfterAI(){
  updatePreviewVisibility();
  if(!state.pages.some(page=>page.id===state.activePageId))state.activePageId=state.pages[0]?.id;
  editingElementId=null;editingItemId=null;renderPages();renderCanvas();
  document.querySelectorAll('[data-theme]').forEach(item=>{const active=item.dataset.theme===state.theme;item.classList.toggle('active',active);item.setAttribute('aria-checked',String(active))});
  document.querySelectorAll('[data-bg]').forEach(item=>item.classList.toggle('active',item.dataset.bg===state.background));
  if(contentWidthInput)contentWidthInput.value=state.contentWidth;if(widthValueLabel)widthValueLabel.textContent=`${state.contentWidth}%`;
}
function restoreState(snapshot){Object.keys(state).forEach(key=>delete state[key]);Object.assign(state,cloneJson(snapshot));syncEditorAfterAI()}
function applyAiSiteOperations(operations){
  const backup=cloneJson(state);const allowedSiteFields=new Set(['siteName','description','theme','background','contentWidth']);const createdPages=new Map();const createdElements=new Map();const resolvePage=id=>state.pages.find(item=>item.id===(createdPages.get(id)||id));const resolveElement=(page,id)=>page?.elements.find(item=>item.id===(createdElements.get(id)||id));
  try{for(const operation of operations||[]){const op=operation.op;
    if(op==='set_site'){
      if(!allowedSiteFields.has(operation.field))throw new Error(`不支持修改站点字段 ${operation.field}`);let value=operation.value;
      if(operation.field==='theme'&&!['minimal','editorial','terminal','soft'].includes(value))throw new Error('AI 返回了无效主题');
      if(operation.field==='background'&&!/^#[0-9a-f]{6}$/i.test(String(value)))throw new Error('AI 返回了无效背景色');
      if(operation.field==='contentWidth')value=Math.max(70,Math.min(100,Number(value)||100));state[operation.field]=value;
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
      const page=resolvePage(operation.pageId);const element=resolveElement(page,operation.elementId);if(!page||!element||!['projects','blog'].includes(element.type)||!Array.isArray(operation.items))throw new Error('AI 指定的作品或文章列表无效');const previous=new Map((element.items||[]).map(item=>[item.id,item]));const incoming=operation.items.slice(0,30).map((raw,index)=>{const old=previous.get(raw.id);const item={id:old?.id||uid('item'),title:String(raw.title||`条目 ${index+1}`),meta:String(raw.meta||''),summary:String(raw.summary||''),image:old&&raw.image===AI_IMAGE_PLACEHOLDER?old.image:String(raw.image||''),pageId:old?.pageId||null};previous.delete(item.id);if(!item.pageId)createDetailPage(page.id,element.type==='projects'?'project':'article',item);return item});previous.forEach(item=>{if(item.pageId)removePageCascade(item.pageId)});element.items=incoming;element.items.forEach(syncItemDetail);
    }else throw new Error(`不支持的 AI 操作 ${op}`);
  }ensureForumAccounts()}catch(error){restoreState(backup);throw error}syncEditorAfterAI();return backup;
}
function describeAiOperation(operation){const page=state.pages.find(item=>item.id===operation.pageId);const element=page?.elements.find(item=>item.id===operation.elementId);const names={set_site:'修改站点设置',set_page:'修改页面',add_page:'新增页面',remove_page:'删除页面',add_element:'新增元素',update_element:'编辑元素',remove_element:'删除元素',move_element:'移动元素',set_items:'更新作品/文章'};return `${names[operation.op]||operation.op}${page?` · ${page.name}`:''}${element?` · ${elementCatalog[element.type]?.name||element.type}`:''}`}
function restoreAiPlaceholders(next,previous){
  if(typeof next==='string'&&AI_PLACEHOLDERS.has(next))return typeof previous==='string'&&!AI_PLACEHOLDERS.has(previous)?previous:next;
  if(Array.isArray(next))return next.map((item,index)=>restoreAiPlaceholders(item,Array.isArray(previous)?previous[index]:undefined));
  if(next&&typeof next==='object'){if(!previous||typeof previous!=='object'||Array.isArray(previous))previous={};const merged={};for(const key of Object.keys(next))merged[key]=restoreAiPlaceholders(next[key],previous[key]);return merged}
  return next;
}
function restoreCodeImages(nextCode,previousCode){
  const sources=[...String(previousCode||'').matchAll(/data:image\/[^"'\s)]+/g)].map(match=>match[0]);let index=0;
  return String(nextCode||'').replaceAll(AI_IMAGE_PLACEHOLDER,()=>index<sources.length?sources[index++]:AI_IMAGE_PLACEHOLDER);
}
function applyAiSiteReplace(siteData){
  if(!siteData||typeof siteData!=='object'||!Array.isArray(siteData.pages)||!siteData.pages.length)throw new Error('AI 返回的网站数据无效');
  const backup=cloneJson(state);const previousPages=new Map(state.pages.map(page=>[page.id,page]));siteData.pages.forEach(page=>{const previous=previousPages.get(page.id);if(previous&&typeof page.code==='string')page.code=restoreCodeImages(page.code,previous.code)});restoreState(restoreAiPlaceholders(cloneJson(siteData),state));ensureForumAccounts();return backup;
}
function showAiError(message,root=document){setAiWorkingStatus(false,root);const result=root.querySelector('#aiResult');if(!result)return;result.textContent=message;result.classList.add('error');result.hidden=false;if(root===document)scrollAiConversation(true)}
const aiToolNames={list_files:'列出网站文件',read_file:'读取文件',search_files:'搜索源码',replace_file:'修改文件',browser_open:'打开本机页面',browser_screenshot:'查看页面截图'};
function renderAiEvents(events=[],root=document){
  const visible=events.filter(item=>item.kind==='tool'||item.tool||(item.kind==='analysis'&&item.status!=='running'));
  const trace=root.querySelector('#aiRunTrace');if(!trace)return;const previous=Number(trace.dataset.rendered||0);const start=previous>visible.length?0:previous;
  trace.innerHTML=visible.map((item,index)=>{const fresh=index>=start?' ai-new':'';if(item.kind==='analysis')return `<p class="ai-run-thought${fresh}">${esc(item.detail||item.label||'')}</p>`;const detail=item.detail?`<small> · ${esc(item.detail)}</small>`:'';return `<p class="ai-run-tool${item.status==='failed'?' failed':''}${fresh}"><b>${esc(aiToolNames[item.tool]||item.label||item.tool||'网站工具')}</b>${detail}</p>`}).join('');
  trace.dataset.rendered=String(visible.length);
}
function aiWorkingText(mode=''){const value=mode||aiPermissionMode;return value==='chat'?'AI 正在思考回答':value==='plan'?'AI 正在制定实施计划':'AI 正在修改并验证网站'}
function setAiWorkingStatus(active,root=document,mode=''){
  const card=root.querySelector('#aiRunCard');if(!card)return;const node=card.querySelector('.ai-working-status');
  if(active&&!node)card.insertAdjacentHTML('afterbegin',`<div class="ai-working-status"><i></i><b>${aiWorkingText(mode)}</b></div>`);
  if(!active)node?.remove();
  card.classList.toggle('is-running',active);
}
function typeAiSummary(text,root=document,instant=false){
  const node=root.querySelector('#aiRunSummary');if(!node)return;clearInterval(node._typingTimer);node._fullText=String(text||'');
  if(instant){node.textContent=node._fullText;node.classList.remove('is-typing');return}
  const chars=[...node._fullText];let index=0;node.textContent='';node.classList.add('is-typing');
  node._typingTimer=setInterval(()=>{index=Math.min(chars.length,index+2);node.textContent=chars.slice(0,index).join('');if(index>=chars.length){clearInterval(node._typingTimer);node.classList.remove('is-typing');persistAiChatHistory()}},18);
}
function renderAiRunProgress(snapshot,run,root=document){
  const events=snapshot.events||[];const failed=snapshot.status==='failed';const panel=root.querySelector('#aiRunCard');if(!panel)return;panel.hidden=false;const finalBox=root.querySelector('#aiRunFinal');if(finalBox)finalBox.hidden=true;const checksBox=root.querySelector('#aiRunChecks');if(checksBox)checksBox.innerHTML='';const process=root.querySelector('#aiRunProcess');if(process)process.open=!failed;const summary=root.querySelector('#aiRunProcessLabel');if(summary){summary.hidden=!failed;summary.textContent='查看过程'}setAiWorkingStatus(!failed&&snapshot.status==='running',root,run?.mode);if(!failed&&snapshot.status==='running'&&run){const node=panel.querySelector('.ai-working-status b');if(node)node.textContent=`${aiWorkingText(run.mode)} · 已运行 ${Math.max(1,Math.round((Date.now()-run.startedAt)/1000))} 秒`}renderAiEvents(events,root);if(root===document)scrollAiConversation();
}
function renderAiRunResult(run,payload,root=document){
  const resultNode=root.querySelector('#aiResult');if(resultNode)resultNode.hidden=true;const panel=root.querySelector('#aiRunCard');if(!panel)return;panel.hidden=false;setAiWorkingStatus(false,root,run.mode);typeAiSummary(payload.summary||'网站修改已完成。',root,root!==document);const finalBox=root.querySelector('#aiRunFinal');if(finalBox)finalBox.hidden=false;
  const planBox=root.querySelector('#aiPlanBox');if(planBox){const showPlan=payload.mode==='plan'&&String(payload.plan||'').trim().length>0;planBox.hidden=!showPlan;if(showPlan)planBox.querySelector('p').textContent=payload.plan}
  const events=payload.events||payload.trace||[];renderAiEvents(events,root);const process=root.querySelector('#aiRunProcess');if(process)process.open=false;const summary=root.querySelector('#aiRunProcessLabel');if(summary){summary.hidden=false;summary.textContent='查看过程'}
  const changes=[...(payload.changedFiles||[]).map(name=>`已修改文件：${name}`),...(payload.siteOperations||[]).map(item=>describeAiOperation(item))];const checksBox=root.querySelector('#aiRunChecks');if(checksBox)checksBox.innerHTML=changes.length?changes.map(item=>`<div class="ai-change">${esc(item)}</div>`).join(''):'<div class="ai-change">未产生文件或站点改动</div>';
  const undoBtn=root.querySelector('#aiUndoBtn');if(undoBtn&&root!==document)undoBtn.hidden=!(lastAiUndo&&lastAiUndo.sessionId===run.sessionId);
  if(root===document)scrollAiConversation(true);
}
async function waitForAiRun(jobId,run){let badPolls=0;for(let attempt=0;attempt<4000;attempt++){let snapshot=null;let failure=null;try{const response=await fetch(`/api/ai/run/status?id=${encodeURIComponent(jobId)}&ts=${Date.now()}`,{cache:'no-store'});const text=await response.text();try{snapshot=text?JSON.parse(text):null}catch{snapshot=null}if(!response.ok){if(response.status===404)failure=new Error(snapshot?.error||'AI 任务不存在或已过期（可能刚重启过服务）');else if(!snapshot)failure='transient';else failure=new Error(snapshot.error||'无法读取 AI 执行状态')}else if(!snapshot){failure='transient'}}catch{failure='transient'}if(failure==='transient'){/* 公网链路（CF/网关）偶发返回 HTML 错误页或瞬断：单次抖动不该判死整个任务 */if(++badPolls>=10)throw new Error('与任务状态接口连续失联（网络或网关抖动）；任务可能仍在后台执行，刷新页面可尝试恢复进度');await new Promise(resolve=>setTimeout(resolve,1000));continue}if(failure)throw failure;badPolls=0;if(run){run.snapshot=snapshot;if(run.sessionId===aiChatSessionId)renderAiRunProgress(snapshot,run)}else renderAiRunProgress(snapshot);if(snapshot.status==='completed'){return {...snapshot.result,events:snapshot.events||[]}}if(snapshot.status==='failed')throw new Error(snapshot.error||'AI 自动任务失败');await new Promise(resolve=>setTimeout(resolve,500))}throw new Error('AI 执行超时，请稍后重试')}
async function loadAiStatus(){updateAiContextRing();try{const response=await fetch('/api/ai/status',{cache:'no-store'});const status=await response.json();const button=document.querySelector('#aiAdjustBtn');aiServiceConfigured=!!status.configured;aiServiceCatalog={models:status.models||[],efforts:status.efforts||[],currentModel:status.model||'',currentEffort:status.reasoningEffort||''};renderAiModelChoice();button.classList.toggle('ai-unavailable',!status.configured);button.title=status.configured?'发送修改要求':'AI 尚未配置：点击查看配置指引';const notice=document.querySelector('#aiServiceNotice');if(notice){if(status.degraded){notice.hidden=false;notice.innerHTML=`<b>AI 服务暂不可用</b> · ${esc(status.degraded.message||'上游额度或配额异常')}（恢复后此提示自动消失，仍可提交任务重试）`}else notice.hidden=true}}catch{/* 请求时再显示具体错误 */}}
function saveDraftNow(){clearTimeout(draftSaveTimer);if(!currentConsoleUser)return;try{const snapshot=JSON.stringify(state);void persistDraftSnapshot(snapshot).catch(()=>setSaveState('同步失败',false))}catch{}}
function persistAiUndo(){try{if(lastAiUndo)sessionStorage.setItem(AI_UNDO_KEY,JSON.stringify(lastAiUndo));else sessionStorage.removeItem(AI_UNDO_KEY)}catch{}}
function persistAiPendingRuns(){try{const list=activeAiRuns().filter(run=>run.jobId).map(run=>({jobId:run.jobId,sessionId:run.sessionId}));if(list.length)sessionStorage.setItem(AI_RUN_KEY,JSON.stringify(list));else sessionStorage.removeItem(AI_RUN_KEY)}catch{}}
function applyAiRunResult(payload){
  let stateBackup=null;if(payload.siteReplace)stateBackup=applyAiSiteReplace(payload.siteReplace);const opsBackup=applyAiSiteOperations(payload.siteOperations||[]);return stateBackup||opsBackup;
}
function syncAiUndoButton(){const button=document.querySelector('#aiUndoBtn');if(button)button.hidden=!(lastAiUndo&&lastAiUndo.sessionId===aiChatSessionId)}
function storeAiRunUndo(run,payload,stateBackup){
  const changedSite=Boolean(payload.siteReplace)||Boolean((payload.siteOperations||[]).length);if(changedSite||payload.undoAvailable){lastAiUndo={proposalId:payload.runId,state:stateBackup,sourceApplied:Boolean(payload.undoAvailable),sessionId:run.sessionId};persistAiUndo()}else{lastAiUndo=null;persistAiUndo()}
  syncAiUndoButton();
}
function finishAiSummaryTyping(root){
  // 打字动画中途要固化/切换视图时，直接补完，避免冻结到半截摘要
  const node=root.querySelector('#aiRunSummary');
  if(node&&node._typingTimer){clearInterval(node._typingTimer);node._typingTimer=null;node.textContent=node._fullText||node.textContent;node.classList.remove('is-typing')}
}
function archiveAiRunOutputInto(root){
  // 新一轮开始前，把上一轮的运行卡片/结果固化成历史记录，避免被复用清空
  finishAiSummaryTyping(root);
  const card=root.querySelector('#aiRunCard');if(!card||card.hidden)return;
  const hasTrace=(card.querySelector('#aiRunTrace')?.childElementCount||0)>0,finalShown=card.querySelector('#aiRunFinal')&&!card.querySelector('#aiRunFinal').hidden,result=root.querySelector('#aiResult'),resultShown=result&&!result.hidden&&result.textContent.trim();
  if(!hasTrace&&!finalShown&&!resultShown)return;
  const freeze=node=>{const copy=node.cloneNode(true);copy.removeAttribute('id');copy.querySelectorAll('[id]').forEach(item=>item.removeAttribute('id'));copy.classList.add('ai-run-archive');copy.querySelector('.ai-working-status')?.remove();copy.querySelector('.is-typing')?.classList.remove('is-typing');card.before(copy)};
  if(resultShown)freeze(result);
  freeze(card);
}
async function finalizeAiRunOffline(run,errorMessage){
  // 任务完成时它的会话不在前台：把结果渲染进该会话的缓存 html 并直接落库，绝不动当前视图的 DOM
  if(!run.sessionId)return;
  let html=aiSessionHtmlCache.get(run.sessionId);
  if(html===undefined){
    try{const payload=await consoleRequest(`/api/ai/sessions/${run.sessionId}`);html=payload.session.messagesHtml||''}catch{html=''}
  }
  if(!html)html=AI_CHAT_INITIAL_HTML;
  const doc=document.createElement('div');doc.innerHTML=html;
  if(!doc.querySelector('#aiRunCard')){
    const tpl=document.createElement('div');tpl.innerHTML=AI_CHAT_INITIAL_HTML;
    ['aiResult','aiRunCard','aiUndoBtn'].forEach(elId=>{const node=tpl.querySelector(`#${elId}`);if(node)doc.append(node)});
  }
  if(errorMessage){showAiError(errorMessage,doc);const undoBtn=doc.querySelector('#aiUndoBtn');if(undoBtn)undoBtn.hidden=true}
  else if(run.payload){renderAiRunResult(run,run.payload,doc)}
  cacheAiSessionHtml(run.sessionId,doc.innerHTML);
  try{await persistAiSessionHtml(run.sessionId,doc.innerHTML)}catch{}
  if(run.sessionId===aiChatSessionId&&!activeRunForSession(run.sessionId))applyAiChatHtml(doc.innerHTML);// 落库期间用户切回了该会话：直接刷新视图
  renderAiSessionList();
  showToast(errorMessage?'另一个会话的 AI 任务失败了':'另一个会话的 AI 任务已完成');
}
function resumeAiRuns(){
  let list=[];
  try{const raw=JSON.parse(sessionStorage.getItem(AI_RUN_KEY)||'null');list=Array.isArray(raw)?raw:(raw?.jobId?[raw]:[])}catch{}
  list=list.filter(item=>item&&typeof item.jobId==='string');
  if(!list.length)return;
  for(const item of list){
    const run=newAiRunRecord(typeof item.sessionId==='string'?item.sessionId:'',{});
    run.jobId=item.jobId;run.resumed=true;
    void resumeAiRun(run);
  }
  updateAiBusy();
}
async function resumeAiRun(run){
  if(!run.sessionId&&aiChatSessionId)run.sessionId=aiChatSessionId;
  const live=()=>Boolean(run.sessionId)&&run.sessionId===aiChatSessionId;
  if(live()){renderAiRunProgress(run.snapshot,run);scrollAiConversation()}
  let stateBackup=null;
  try{
    const payload=await waitForAiRun(run.jobId,run);persistAiPendingRuns();
    stateBackup=applyAiRunResult(payload);storeAiRunUndo(run,payload,stateBackup);saveDraftNow();
    run.status='completed';run.payload=payload;persistAiPendingRuns();
    if(live())renderAiRunResult(run,payload);else await finalizeAiRunOffline(run,null);
    if(payload.restartRequired)await restartLocalServer();
    showToast(payload.restartRequired?'AI 已完成修改并重启服务':'刷新前开始的 AI 任务已完成');
  }catch(error){
    run.status='failed';persistAiPendingRuns();
    if(stateBackup)restoreState(stateBackup);
    const message=`刷新前开始的 AI 任务未能恢复：${error.message}`;
    if(live())showAiError(message);else await finalizeAiRunOffline(run,message);
  }finally{updateAiBusy();updateAiContextRing();renderAiSessionList();if(live())scrollAiConversation()}
}
function notifyPublishedReload(reason){try{const channel=new BroadcastChannel('alchemyhatchery-live-preview');channel.postMessage({type:'reload',reason,at:Date.now()});channel.close()}catch{}try{localStorage.setItem('alchemyhatchery:published-reload',JSON.stringify({reason,at:Date.now()}))}catch{}}
function refreshAiChangedFiles(files=[]){
  const changedStyles=files.filter(name=>['styles.css','mica.css','ai-chat.css'].includes(name));if(changedStyles.length)document.querySelectorAll('link[rel="stylesheet"]').forEach(link=>{const url=new URL(link.href,location.href);if(changedStyles.some(name=>url.pathname.endsWith(`/${name}`))){url.searchParams.set('ai',String(Date.now()));link.href=url.toString()}});
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
  syncAiUndoButton();
}
async function consoleRequest(path,options={}){
  if(window.AIchemyHatcheryAuth)return window.AIchemyHatcheryAuth.request(path,options);
  const request={method:options.method||'GET',headers:{'Accept':'application/json'}};
  if(options.body!==undefined){request.headers['Content-Type']='application/json';request.body=JSON.stringify(options.body)}
  const response=await fetch(path,request);let payload={};try{payload=await response.json()}catch{payload={error:'服务返回了无法解析的内容'}}
  if(!response.ok){const error=new Error(payload.error||`请求失败（${response.status}）`);error.status=response.status;throw error}return payload;
}
function showAuthGate(message=''){
  currentConsoleUser=null;aiChatSessionId=null;aiSessionList=[];aiRuns.clear();aiSessionQueues.clear();aiSessionHtmlCache.clear();aiSessionDirty.clear();aiSessionTitles.clear();updateAiBusy();renderAiSessionList();if(window.AIchemyHatcheryAuth){window.AIchemyHatcheryAuth.showGate(message);return}uiShow(document.querySelector('#authGate'));const error=document.querySelector('#authError');error.textContent=message;error.hidden=!message;setSaveState('等待登录',false);
}
function updateConsoleAccount(){
  if(!currentConsoleUser)return;const username=currentConsoleUser.username;const initial=username.slice(0,1).toUpperCase();
  document.querySelector('#consoleAvatar').textContent=initial;document.querySelector('#menuAvatar').textContent=initial;document.querySelector('#menuUsername').textContent=username;const railName=document.querySelector('#activityAccountName');if(railName)railName.textContent=username;document.querySelector('#accountUsername').textContent=username;document.querySelector('#accountPreviewPath').textContent=currentConsoleUser.previewId?`/preview/${currentConsoleUser.previewId}`:'首次预览后生成';document.querySelector('#accountPublishPath').textContent=currentConsoleUser.publishSlug?`${currentConsoleUser.publishSlug}.${currentConsoleUser.publishDomain||'hatchery.mizusumi.com'}`:'未发布';document.querySelector('#inviteManagerBtn').hidden=currentConsoleUser.role!=='admin';
}
async function enterConsole(user){
  currentConsoleUser=user;const username=user.username;aiRuns.clear();aiSessionQueues.clear();aiSessionHtmlCache.clear();aiSessionDirty.clear();aiSessionTitles.clear();aiSessionDeleted.clear();updateAiBusy();DRAFT_KEY=`alchemyhatchery:${username}:draft:v3`;AI_UNDO_KEY=`alchemyhatchery:${username}:ai-undo:v2`;AI_RELOAD_NOTICE_KEY=`alchemyhatchery:${username}:ai-reload-notice:v2`;AI_RUN_KEY=`alchemyhatchery:${username}:ai-run:v1`;AI_MODE_KEY=`alchemyhatchery:${username}:ai-mode:v1`;AI_MODEL_KEY=`alchemyhatchery:${username}:ai-model:v1`;
  let draft=null;try{draft=(await consoleRequest('/api/console/draft')).draft}catch(error){if(error.status===401){showAuthGate('登录已过期，请重新登录');return}showToast(`读取云端草稿失败：${error.message}`)}
  if(!draft){try{draft=JSON.parse(localStorage.getItem(DRAFT_KEY)||'null')}catch{}}
  const next=draft?.pages?.length?{...INITIAL_STATE,...draft}:INITIAL_STATE;restoreState(next);ensureForumAccounts();syncEditorAfterAI();historyStack=[JSON.stringify(state)];historyIndex=0;updateHistoryButtons();lastAiUndo=null;try{const savedUndo=JSON.parse(sessionStorage.getItem(AI_UNDO_KEY)||'null');if(savedUndo?.proposalId)lastAiUndo=savedUndo}catch{}
  updateConsoleAccount();loadAiPermissionMode();loadAiModelChoice();uiHide(document.querySelector('#authGate'));setSaveState(draft?'草稿已同步':'新草稿',false);restoreAiReloadUi();void initAiChatSessions();void loadAiStatus();if(!draft)scheduleDraftSave();resumeAiRuns();
}
function sessionLabel(userAgent=''){
  const browser=/Edg/i.test(userAgent)?'Edge':/Chrome/i.test(userAgent)?'Chrome':/Firefox/i.test(userAgent)?'Firefox':/Safari/i.test(userAgent)?'Safari':'浏览器';const system=/Windows/i.test(userAgent)?'Windows':/Mac OS/i.test(userAgent)?'macOS':/Android/i.test(userAgent)?'Android':/iPhone|iPad/i.test(userAgent)?'iOS':'未知系统';return `${browser} · ${system}`;
}
async function openPasswordSettings(){
  try{const payload=await consoleRequest('/api/auth/sessions');const sessions=(payload.sessions||[]).map(item=>`<div class="security-session${item.current?' current':''}"><div><b>${item.current?'当前会话':sessionLabel(item.userAgent)}</b><small>${item.current?sessionLabel(item.userAgent):`最后活动 ${esc(String(item.lastSeenAt||item.createdAt||'').replace('T',' '))}`}</small></div><span>${item.current?'正在使用':'已登录'}</span></div>`).join('');openModal('账号安全',`<div class="account-security"><header><span class="console-avatar">${esc(currentConsoleUser?.username?.slice(0,1).toUpperCase()||'?')}</span><div><b>${esc(currentConsoleUser?.username||'')}</b><small>${currentConsoleUser?.role==='admin'?'管理员':'普通用户'} · 随机预览地址</small></div></header><section><h3>修改密码</h3><p>修改密码后会注销其他所有设备，当前设备自动换发新会话。</p><form class="account-settings-form" data-console-form="password"><label>当前密码<input name="oldPassword" type="password" autocomplete="current-password" required></label><label>新密码<input name="newPassword" type="password" autocomplete="new-password" minlength="8" maxlength="128" required></label><label>确认新密码<input name="confirmPassword" type="password" autocomplete="new-password" minlength="8" maxlength="128" required></label><button>确认修改密码</button></form></section><section><div class="security-title"><div><h3>登录设备</h3><p>${(payload.sessions||[]).length} 个有效会话</p></div><button data-revoke-other-sessions>退出其他设备</button></div><div class="security-sessions">${sessions}</div></section></div>`)}catch(error){showToast(error.message)}
}
const fmtTokenCount=value=>Number(value||0).toLocaleString('en-US');
async function openInviteManager(){
  openModal('账号与注册管理','<div class="invite-empty">正在加载管理后台…</div>');
  try{const [userPayload,usagePayload,aiConfig,sitesPayload]=await Promise.all([consoleRequest('/api/admin/users'),consoleRequest('/api/admin/ai-usage'),consoleRequest('/api/admin/ai-config'),consoleRequest('/api/admin/sites')]);const users=(userPayload.users||[]).map(item=>{const own=item.username.toLowerCase()===currentConsoleUser?.username?.toLowerCase();const status=item.status==='active'?'正常':'已停用';return `<div class="admin-user-row"><span class="console-avatar">${esc(item.username.slice(0,1).toUpperCase())}</span><div><b>${esc(item.username)}</b><small>${item.realName?esc(item.realName)+' · ':''}${item.role==='admin'?'管理员':'用户'} · ${item.published?'已发布':'未发布'} · ${item.sessionCount} 个会话</small></div><em class="${item.status}">${status}</em>${own?'<i>当前账号</i>':`<button data-user-status="${item.status==='active'?'disabled':'active'}" data-user-name="${esc(item.username)}">${item.status==='active'?'停用':'启用'}</button>`}</div>`}).join('')||'<div class="invite-empty">暂无用户</div>';const usageRows=(usagePayload.usage||[]).map(item=>`<div class="admin-usage-row"><span><b>${esc(item.username)}</b><small>${item.realName?esc(item.realName)+' · ':''}${esc(item.campusId||'')} · ${item.runs} 次任务</small></span><em>${fmtTokenCount(item.dayTokens)}</em><em>${fmtTokenCount(item.weekTokens)}</em><em>${fmtTokenCount(item.totalTokens)}</em><button data-ai-chats="${esc(String(item.userId))}" data-user-name="${esc(item.username)}">记录</button></div>`).join('')||'<div class="invite-empty">还没有 AI 使用记录</div>';const siteRows=(sitesPayload.sites||[]).map(item=>`<div class="admin-site-row"><span class="console-avatar">${esc((item.siteName||item.slug).slice(0,1).toUpperCase())}</span><div><b>${esc(item.siteName||item.slug)}</b><small>${esc(item.owner||'未知')}${item.ownerCampusId?' · '+esc(item.ownerCampusId):''} · 更新于 ${esc(String(item.updatedAt||'').slice(0,10))}</small><small class="admin-site-url">${esc(String(item.publicUrl||'').replace(/^https?:\/\//,''))}</small></div><em class="${item.onDisk===false?'missing':item.listed?'active':'disabled'}">${item.onDisk===false?'文件缺失':item.listed?'已收录':'未收录'}</em><a href="${esc(item.publicUrl)}" target="_blank" rel="noopener">打开 ↗</a></div>`).join('')||'<div class="invite-empty">还没有发布的网站</div>';const totals=usagePayload.totals||{};const kimiStatus=aiConfig.kimiKeySet?`已配置 ${esc(aiConfig.kimiKeyMask)}`:'未配置';const explicitNote=aiConfig.explicitProvider?`<span>注意：.env 中 MIAODA_LLM=${esc(aiConfig.explicitProvider)} 显式指定了提供方，改动 Key 不会切换当前服务。</span>`:'';const aiKeySection=`<section class="ai-key-manager"><header><div><small>AI SERVICE</small><h3>AI 服务配置</h3></div><b>${esc(aiConfig.providerLabel)}</b></header><div class="ai-key-status"><span>当前生效：${esc(aiConfig.providerLabel)} · 模型 ${esc(aiConfig.model)}</span><span>Kimi Key：${kimiStatus}${aiConfig.deepseekKeySet?' · DeepSeek Key：已配置':''}</span>${explicitNote}</div><form class="ai-key-form" data-console-form="ai-key"><input name="kimiApiKey" type="password" autocomplete="off" spellcheck="false" placeholder="粘贴新的 Kimi API Key（sk-…）"><button>保存 Key</button><button type="button" data-ai-key-clear${aiConfig.kimiKeySet?'':' disabled'}>清除</button></form><p>Key 写入服务器 .env 并立即生效；界面只显示末 4 位，完整内容不会回传。</p></section>`;openModal('账号与注册管理',`<div class="admin-console"><section><header><div><small>USERS</small><h3>控制台用户</h3></div><b>${userPayload.total||0}</b></header><div class="admin-user-list">${users}</div></section><section class="ai-usage-manager"><header><div><small>AI TOKENS</small><h3>AI 用量统计</h3></div><button data-ai-chats="" data-user-name="全部用户">全部记录</button></header><p>今日 ${fmtTokenCount(totals.dayTokens)} · 近 7 天 ${fmtTokenCount(totals.weekTokens)} · 累计 ${fmtTokenCount(totals.totalTokens)} tokens</p><div class="admin-usage-head"><span>用户</span><span>今日</span><span>近 7 天</span><span>累计</span><span></span></div><div class="admin-usage-list">${usageRows}</div></section><section class="admin-sites-manager"><header><div><small>PUBLISHED SITES</small><h3>已发布网站</h3></div><a href="/gallery" target="_blank" rel="noopener">Gallery ↗</a></header><div class="admin-site-list">${siteRows}</div></section>${aiKeySection}</div>`)}catch(error){openModal('账号与注册管理',`<div class="invite-empty">${esc(error.message)}</div>`)}
}
async function openAiChats(userId,username){
  try{const payload=await consoleRequest(`/api/admin/ai-chats${userId?`?userId=${encodeURIComponent(userId)}`:''}`);const rows=(payload.chats||[]).map(item=>{const time=esc(String(item.createdAt||'').replace('T',' ').slice(0,19));const atts=(item.attachments||[]).map(att=>`<i>${att.kind==='image'?'图片':'附件'} · ${esc(att.name||'')}</i>`).join('');return `<article class="ai-chat-item"><header><b>${esc(item.username)}</b><span>${time} · ${esc(item.model||item.provider||'AI')} · ${fmtTokenCount(item.totalTokens)} tokens（入 ${fmtTokenCount(item.inputTokens)} / 出 ${fmtTokenCount(item.outputTokens)}）· ${item.status==='completed'?'完成':'失败'}</span></header><p>${esc(item.prompt)}</p>${atts?`<div class="ai-chat-atts">${atts}</div>`:''}</article>`}).join('')||'<div class="invite-empty">暂无聊天记录</div>';openModal(`AI 记录 · ${username||'全部用户'}`,`<div class="ai-chat-manager"><button class="ai-chat-back" data-ai-chats-back>← 返回管理后台</button><div class="ai-chat-list">${rows}</div></div>`)}catch(error){showToast(error.message)}
}
async function logoutConsole(){
  try{await persistDraftSnapshot(JSON.stringify(state))}catch{}uiHide(document.querySelector('#consoleAccountMenu'));if(window.AIchemyHatcheryAuth)await window.AIchemyHatcheryAuth.logout();else showAuthGate();showToast('已退出控制台账号');
}
document.querySelector('#aiAdjustBtn').addEventListener('click',async()=>{
  if(!aiServiceConfigured){showToast('AI 尚未配置：请管理员在「管理后台 → AI 服务配置」中添加 Kimi API Key');return}
  const promptInput=document.querySelector('#aiPrompt');const typedPrompt=promptInput.value.trim();if(!typedPrompt&&!aiAttachments.length){showToast('输入修改要求，或先添加附件');promptInput.focus();return}
  const prompt=typedPrompt||'请根据附件内容调整网站。';const requestAttachments=aiAttachments.map(item=>({...item}));promptInput.value='';promptInput.style.height='auto';aiAttachments=[];renderAiAttachments();
  await startAiRun(prompt,requestAttachments);
});
function aiChatHistoryForContext(limit=8){
  // 提取最近几轮对话（用户消息 + AI 摘要），作为本次请求的上下文
  const entries=[];
  for(const node of document.querySelector('#aiChatMessages')?.children||[]){
    let role=null,text='';
    if(node.classList?.contains('ai-message')){role=node.classList.contains('user')?'user':'assistant';text=node.innerText.trim()}
    else if(node.classList?.contains('ai-run-archive')||node.id==='aiRunCard'){const summary=node.querySelector('#aiRunSummary, .ai-run-final p');if(summary?.textContent.trim()){role='assistant';text=summary.textContent.trim()}}
    if(role&&text)entries.push({role,text:text.slice(0,600)});
  }
  return entries.slice(-limit);
}
function archiveAiRunOutput(){
  archiveAiRunOutputInto(document);
}
async function startAiRun(prompt,requestAttachments=[],chosenPresets=[],presetSnippets=[],runOptions={}){
  if(!currentConsoleUser)return;
  const sessionId=ensureAiSessionId();
  const shownText=aiRunShownText(prompt,chosenPresets,runOptions.displayText||'');
  const item={prompt,shownText,attachments:requestAttachments,chosenPresets,presetSnippets,mode:runOptions.mode||aiPermissionMode,history:aiChatHistoryForContext()};
  if(activeRunForSession(sessionId)){enqueueAiRun(sessionId,item);return}
  const run=newAiRunRecord(sessionId,item);
  await executeAiRun(run);
}
async function executeAiRun(run){
  updateAiBusy();
  const live=()=>run.sessionId===aiChatSessionId;
  if(!aiSessionTitles.has(run.sessionId))aiSessionTitles.set(run.sessionId,run.shownText.slice(0,30)||'新聊天');
  if(live()){
    archiveAiRunOutput();
    if(!run.messageAppended)appendAiChatMessage('user',run.shownText,run.attachments);
    const messages=document.querySelector('#aiChatMessages');const result=document.querySelector('#aiResult');const card=document.querySelector('#aiRunCard');const undoBtn=document.querySelector('#aiUndoBtn');
    if(messages&&result&&card)messages.append(result,card,...undoBtn?[undoBtn]:[]);
    if(result){result.hidden=true;result.classList.remove('error')}
    renderAiRunProgress(run.snapshot,run);
    scrollAiConversation();
  }else{
    // 离线启动（排队任务轮到执行时已切走）：先在该会话的缓存 html 里固化上一轮输出、重置卡片
    const html=aiSessionHtmlCache.get(run.sessionId);
    if(html!==undefined){
      const doc=document.createElement('div');doc.innerHTML=html;
      archiveAiRunOutputInto(doc);
      const staleResult=doc.querySelector('#aiResult');if(staleResult){staleResult.hidden=true;staleResult.classList.remove('error')}
      renderAiRunProgress(run.snapshot,run,doc);
      doc.querySelectorAll('.ai-queue-tag').forEach(tag=>{tag.textContent='执行中'});
      cacheAiSessionHtml(run.sessionId,doc.innerHTML);
      void persistAiSessionHtml(run.sessionId,doc.innerHTML).catch(()=>{});
    }
  }
  let payload=null;let stateBackup=null;
  try{
    const response=await fetch('/api/ai/run?async=1',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:run.prompt,mode:run.mode,model:aiModelChoice.model,reasoningEffort:aiModelChoice.effort,attachments:run.attachments.map(({name,type,size,kind,content})=>({name,type,size,kind,content})),context:aiSafeSnapshot(),includeSite:true,includeSource:true,chosenPresets:run.chosenPresets,presetSnippets:run.presetSnippets,history:run.history||[]})});let started=null;const startText=await response.text();try{started=startText?JSON.parse(startText):null}catch{/* 网关瞬时故障会回 HTML 错误页 */}if(!started)throw new Error('网关返回了无效响应，任务未能启动；请稍后重试');if(!response.ok)throw new Error(started.error||'AI 自动任务启动失败');const jobId=response.status===202&&started.jobId?started.jobId:null;if(jobId){run.jobId=jobId;persistAiPendingRuns()}payload=jobId?await waitForAiRun(jobId,run):started;
    run.status='completed';run.payload=payload;persistAiPendingRuns();
    stateBackup=applyAiRunResult(payload);storeAiRunUndo(run,payload,stateBackup);const liveFiles=refreshAiChangedFiles(payload.changedFiles||[]);saveDraftNow();
    if(live())renderAiRunResult(run,payload);else await finalizeAiRunOffline(run,null);
    if(payload.restartRequired){const button=document.querySelector('#aiAdjustBtn');button.querySelector('b').textContent='重启中…';await restartLocalServer()}
    if(liveFiles.editorReloadNeeded){scheduleEditorReload('AI 网站代理已自动修改、验证并刷新编辑器。')}else showToast(payload.restartRequired?'AI 已完成修改并重启服务':(live()?'AI 已自动完成并验证网站修改':'另一个会话的 AI 任务已完成'));
  }catch(error){
    run.status='failed';persistAiPendingRuns();
    if(stateBackup)restoreState(stateBackup);if(payload?.undoAvailable){try{await fetch('/api/ai/undo',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({proposalId:payload.runId})})}catch{}}lastAiUndo=null;persistAiUndo();syncAiUndoButton();const failText=String(error.message||'');
    const message=failText.includes('配额已用尽')?failText:`自动执行失败，修改已回滚：${failText}`;
    if(live())showAiError(message);else await finalizeAiRunOffline(run,message);
    loadAiStatus();
  }finally{
    updateAiBusy();updateAiContextRing();renderAiSessionList();if(live())scrollAiConversation();
    const queue=aiSessionQueues.get(run.sessionId)||[];const next=queue.shift();if(!queue.length)aiSessionQueues.delete(run.sessionId);
    if(next)runQueuedAiTask(run.sessionId,next);
  }
}
const TEMPLATE_GROUPS=[
  {title:'页面结构',en:'STRUCTURE',items:['nav','hero','footer','cta']},
  {title:'内容展示',en:'CONTENT',items:['projects','blog','gallery','stats','team','timeline']},
  {title:'个人必备',en:'PERSONAL',items:['profile','schedule','countdown','skills','awards']},
  {title:'社团运营',en:'CLUB',items:['departments','steps','faq','downloads','poll']},
  {title:'社区功能',en:'COMMUNITY',items:['forum','account','notice','links']},
];
const TEMPLATE_DESCRIPTIONS={nav:'LOGO 与页面链接',hero:'主标题与行动按钮',footer:'联系与版权信息',cta:'一句话与主按钮',projects:'三列项目作品卡',blog:'日期、标题与摘要',gallery:'错落图片网格',stats:'关键数字与指标',team:'头像、名字与分工',timeline:'日期与活动安排',profile:'头像、简介与联系方式',schedule:'一周五天的课程网格',countdown:'高考或活动倒计天数',skills:'技能条与百分比',awards:'年份、奖项与说明',departments:'部门职责与人数',steps:'报名到入社的指引',faq:'折叠式问答列表',downloads:'文件清单与下载按钮',poll:'选项投票与实时占比',forum:'板块导航、话题列表与回复数量',account:'登录状态、账号信息与退出',notice:'招新与活动通知',links:'社群与相关站点'};
const TEMPLATE_VISUALS={nav:'<span></span><span></span><span></span>',hero:'<strong>Aa</strong><span></span>',footer:'<span></span><span></span>',cta:'<strong>→</strong><span></span>',projects:'<span></span><span></span><span></span>',blog:'<span></span><span></span><span></span>',gallery:'<span></span><span></span><span></span>',stats:'<strong>24</strong><strong>08</strong><strong>16</strong>',team:'<span></span><span></span><span></span>',timeline:'<span></span><span></span><span></span>',profile:'<span></span><span></span>',schedule:'<span></span><span></span><span></span><span></span>',countdown:'<strong>07</strong><span></span>',skills:'<span></span><span></span><span></span>',awards:'<strong>★</strong><span></span>',departments:'<span></span><span></span><span></span>',steps:'<b>1</b><b>2</b><b>3</b>',faq:'<strong>?</strong><span></span>',downloads:'<span>↓</span><span>↓</span>',poll:'<span></span><span></span>',forum:'<span></span><div><b></b><b></b><b></b></div>',account:'<span>AD</span><b></b>',notice:'<strong>!</strong><span></span>',links:'<span>↗</span><span>↗</span>'};
function closeTemplatePicker(){
  const picker=document.querySelector('#templatePicker');
  if(picker){
    if(uiMotionOK()&&!picker.classList.contains('is-closing')){picker.classList.add('is-closing');setTimeout(()=>picker.remove(),240)}
    else if(!picker.classList.contains('is-closing'))picker.remove();
  }
  document.querySelector('[data-template-open]')?.classList.remove('active');
  if(!document.querySelector('.ai-chat-panel')?.hidden)document.querySelector('#aiNewChatBtn')?.classList.add('active');
}
function openTemplatePicker(){
  if(anyAiRunActive()){showToast('AI 正在修改网站，完成后再添加模块');return}
  const panel=document.querySelector('.ai-chat-panel');if(!panel||panel.querySelector('#templatePicker'))return;
  document.querySelector('#aiNewChatBtn')?.classList.remove('active');
  document.querySelector('[data-template-open]')?.classList.add('active');
  const groups=TEMPLATE_GROUPS.map(group=>`<div class="element-group"><div class="element-group-title"><b>${group.title}</b><span>${group.en}</span></div><div class="element-grid">${group.items.map(type=>`<button type="button" class="element-card${type==='forum'?' wide':''}" data-template-type="${type}" aria-pressed="false"><i class="element-visual v-${type}">${TEMPLATE_VISUALS[type]||'<span></span>'}</i><b>${elementCatalog[type]?.name||type}</b><small>${TEMPLATE_DESCRIPTIONS[type]||''}</small></button>`).join('')}</div></div>`).join('');
  panel.insertAdjacentHTML('beforeend',`<div class="template-picker" id="templatePicker"><header class="template-picker-head"><div><small>TEMPLATES</small><b>浏览现成模板</b></div><button type="button" class="template-picker-close" data-template-close aria-label="关闭模板选择">×</button></header><div class="template-picker-body"><p class="template-picker-tip">点选要用的模块（可多选），确定后交给 AI 加进当前网站。</p>${groups}</div><footer class="template-picker-foot"><span data-template-count>未选择模块</span><button type="button" class="ai-preset-confirm" data-template-confirm disabled>确定添加</button></footer></div>`);
  const picker=panel.querySelector('#templatePicker'),confirm=picker.querySelector('[data-template-confirm]'),count=picker.querySelector('[data-template-count]');
  picker.querySelector('[data-template-close]').addEventListener('click',closeTemplatePicker);
  picker.querySelectorAll('[data-template-type]').forEach(card=>card.addEventListener('click',()=>{
    card.setAttribute('aria-pressed',String(card.getAttribute('aria-pressed')!=='true'));
    const chosen=picker.querySelectorAll('[data-template-type][aria-pressed="true"]').length;
    if(chosen>8){card.setAttribute('aria-pressed','false');showToast('一次最多添加 8 个模块，可分多次添加');return}
    count.textContent=chosen?`已选 ${chosen} 个模块`:'未选择模块';confirm.disabled=!chosen;
  }));
  confirm.addEventListener('click',async()=>{
    const chosen=[...picker.querySelectorAll('[data-template-type][aria-pressed="true"]')].map(card=>card.dataset.templateType);
    if(!chosen.length)return;
    closeTemplatePicker();
    const promptInput=document.querySelector('#aiPrompt'),typed=promptInput.value.trim();
    const prompt=typed||'请把选中的模块添加到当前网站，融入整体设计。';const requestAttachments=aiAttachments.map(item=>({...item}));
    // 把模块的默认渲染代码一并交给 AI，让它读取并嵌入整页代码中
    const page=activePage();const presetSnippets=chosen.map(type=>{const element=createElement(type,page);return {type,name:elementCatalog[type]?.name||type,html:blockContent(type,element,false)}});
    promptInput.value='';promptInput.style.height='auto';aiAttachments=[];renderAiAttachments();
    await startAiRun(prompt,requestAttachments,chosen,presetSnippets);
  });
}
function switchActivity(name){
  closeTemplatePicker();
  document.querySelectorAll('.activity-item[data-activity]').forEach(item=>{const active=item.dataset.activity===name;item.classList.toggle('active',active);item.setAttribute('aria-selected',String(active))});
  document.querySelector('#aiNewChatBtn')?.classList.toggle('active',name==='ai');
  document.querySelector('.ai-chat-panel').hidden=name!=='ai';
  document.querySelector('.security-panel').hidden=name!=='security';
}
document.querySelectorAll('.activity-item[data-activity]').forEach(item=>item.addEventListener('click',()=>switchActivity(item.dataset.activity)));
document.querySelector('[data-template-open]').addEventListener('click',()=>{if(document.querySelector('#templatePicker')){closeTemplatePicker();return}switchActivity('ai');openTemplatePicker()});
const SECURITY_SENSITIVE_PATHS=['/.env','/.env.example','/.git/config','/.gitignore','/server.py','/requirements.txt','/alchemy_hatchery.db','/miaoda.db'];
async function runSecurityChecks(){
  const button=document.querySelector('#securityRunBtn'),summary=document.querySelector('#securitySummary'),list=document.querySelector('#securityResults');
  if(button.disabled)return;button.disabled=true;button.textContent='正在测试…';list.innerHTML='';summary.hidden=true;
  const results=[];const started=Date.now();
  const render=()=>{const counts={pass:0,warn:0,fail:0};results.forEach(item=>counts[item.status]+=1);summary.hidden=false;summary.innerHTML=`<b>${counts.pass}</b> 项通过 · <b>${counts.warn}</b> 项警告 · <b>${counts.fail}</b> 项风险 · 共 ${results.length} 项`;list.innerHTML=results.map(item=>`<article class="security-check ${item.status}"><i>${item.status==='pass'?'✓':item.status==='warn'?'!':'✕'}</i><div><b>${esc(item.name)}</b><small>${esc(item.detail)}</small></div></article>`).join('')};
  const add=(name,status,detail)=>{results.push({name,status,detail});render()};
  try{
    const response=await fetch('/',{cache:'no-store'});const headers=response.headers;
    const required=[['x-content-type-options','X-Content-Type-Options'],['referrer-policy','Referrer-Policy'],['x-frame-options','X-Frame-Options'],['content-security-policy','Content-Security-Policy'],['strict-transport-security','Strict-Transport-Security']];
    const missing=required.filter(([key])=>!headers.get(key));
    add('安全响应头',missing.length?(missing.length>2?'fail':'warn'):'pass',missing.length?`缺少：${missing.map(([,label])=>label).join('、')}`:'常见安全响应头齐全');
    const serverHeader=headers.get('server');if(serverHeader)add('服务指纹','warn',`Server 响应头暴露了服务标识：${serverHeader}`);
  }catch(error){add('安全响应头','fail',`无法请求首页：${error.message}`)}
  for(const path of SECURITY_SENSITIVE_PATHS){
    try{const response=await fetch(path,{cache:'no-store'});add(`敏感文件 ${path}`,response.status===200?'fail':'pass',response.status===200?`可被直接访问下载（HTTP ${response.status}）`:`不可访问（HTTP ${response.status}）`)}
    catch{add(`敏感文件 ${path}`,'pass','请求失败，不可访问')}
  }
  add('传输加密',location.protocol==='https:'?'pass':'warn',location.protocol==='https:'?'当前通过 HTTPS 访问':'当前是明文 HTTP；本地开发属正常，对外部署必须启用 HTTPS');
  button.disabled=false;button.textContent='重新测试';summary.innerHTML+=` · 耗时 ${((Date.now()-started)/1000).toFixed(1)}s`;
}
document.querySelector('#securityRunBtn').addEventListener('click',()=>void runSecurityChecks());
// 撤销按钮在 #aiChatMessages 内部，会话切换会重建它：用事件委托避免监听器丢失
document.querySelector('#aiChatMessages').addEventListener('click',async event=>{
  const button=event.target.closest('#aiUndoBtn');if(!button)return;
  if(!lastAiUndo||lastAiUndo.sessionId!==aiChatSessionId)return;const undo=lastAiUndo;button.disabled=true;let sourceUndo={restoredFiles:[],restartRequired:false};
  try{
    if(undo.sourceApplied){const response=await fetch('/api/ai/undo',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({proposalId:undo.proposalId})});sourceUndo=await response.json();if(!response.ok)throw new Error(sourceUndo.error||'源码恢复失败')}
    restoreState(undo.state);saveDraftNow();const live=refreshAiChangedFiles(sourceUndo.restoredFiles||[]);lastAiUndo=null;persistAiUndo();button.hidden=true;
    if(sourceUndo.restartRequired)await restartLocalServer();const result=document.querySelector('#aiResult');result.classList.remove('error');result.textContent='已撤销上一次 AI 修改。';result.hidden=false;scrollAiConversation(true);
    if(live.editorReloadNeeded)scheduleEditorReload('已撤销 AI 修改，并自动刷新恢复编辑器。');else showToast('已恢复 AI 修改前的状态')
  }catch(error){showAiError(`撤销失败：${error.message}`)}finally{button.disabled=false}
});
let buildingPreviewHtml=false;
function buildPublishPayload(preview=false){
  ensureForumAccounts();ensureAllPagesCode();
  const previousPage=state.activePageId,previousAccountState=previewDB.accountLoggedIn;previewDB.accountLoggedIn=false;buildingPreviewHtml=preview;
  const pages=state.pages.map(page=>{state.activePageId=page.id;return {id:page.id,name:page.name,path:pageFullPath(page),parentId:page.parentId,kind:page.kind,html:isolatePageCode(page.code)+floatingObjectLayer(page,false)}});
  state.activePageId=previousPage;previewDB.accountLoggedIn=previousAccountState;buildingPreviewHtml=false;return {username:currentConsoleUser?.username||'',siteName:state.siteName,description:state.description,theme:state.theme,background:state.background,contentWidth:state.contentWidth,pages,forumPosts:[]};
}
// 预览持续在后台更新：已有 previewId 时点开即是当前内容，再后台补一次最新推送；
// 首次使用还没有 previewId，走一次即时生成拿到地址
document.querySelector('#previewSiteBtn').addEventListener('click',async()=>{
  const button=document.querySelector('#previewSiteBtn');
  if(currentConsoleUser?.previewId){
    window.open(`/preview/${currentConsoleUser.previewId}`,'_blank');
    void pushPreviewSnapshot(true);
    return;
  }
  const popup=window.open('about:blank','_blank');button.disabled=true;button.firstChild.textContent='生成中 ';
  try{
    await persistDraftSnapshot(JSON.stringify(state));
    const payload=await pushPreviewSnapshot(true);
    if(!payload)throw new Error('预览生成失败，请重试');
    if(popup)popup.location.replace(payload.url);else window.open(payload.url,'_blank');
    showToast('预览已开启，之后编辑会自动实时更新');
  }catch(error){popup?.close();if(error.status===401)showAuthGate('登录已过期，请重新登录');showToast(`预览失败：${error.message}`)}
  finally{button.disabled=false;button.firstChild.textContent='预览 '}
});
function siteAdminCredentialMarkup(admin){return `<div class="site-admin-credential"><small>本站独立管理员 · 仅显示这一次</small><b>${esc(admin.username)}</b><code>${esc(admin.password)}</code><button type="button" data-modal-action="copy" data-copy="用户名：${esc(admin.username)}\n密码：${esc(admin.password)}">复制管理员凭据</button><p>请登录发布网站后妥善保管。它不等于炼丹社Hatchery控制台账号，也不能登录其他网站。</p></div>`}
function openPublishModal(){
  if(!currentConsoleUser){showAuthGate('请先登录');return}
  const current=String(currentConsoleUser.publishSlug||'');
  const publishDomain=String(currentConsoleUser.publishDomain||'hatchery.mizusumi.com');
  const listed=true;// 每次打开发布弹窗都默认勾选收录进 Gallery；取消勾选仅对当次发布生效
  openModal('发布网站',`<form class="account-settings-form publish-form" data-console-form="publish"><p>输入发布路径，网站将发布在 <b class="publish-domain" data-publish-domain>${esc(current||'xxx')}.${esc(publishDomain)}</b>。</p><label>发布路径<input name="slug" required minlength="3" maxlength="32" pattern="[a-z0-9](-?[a-z0-9])+" placeholder="例如 campus-news" value="${esc(current)}" autocomplete="off" spellcheck="false"><small>3–32 位小写字母、数字或短横线；已被他人占用的路径不能取。</small></label>${current?`<p class="publish-warning" data-publish-warning hidden>更改路径后，旧地址 <b>${esc(current)}.${esc(publishDomain)}</b> 的页面（含论坛、成员账号数据）会被删除。</p>`:''}<div class="publish-gallery-opt"><label><input name="listed" type="checkbox"${listed?' checked':''}><span>收录进 Hatchery Gallery</span></label><small>收录后公开展示在 <a href="/gallery" target="_blank" rel="noopener">/gallery</a>；取消勾选不影响网站访问，只是不出现在收录页。</small></div><button>确认发布</button></form>`);
  const form=document.querySelector('#previewModal [data-console-form="publish"]');if(!form)return;const input=form.elements.slug,domain=form.querySelector('[data-publish-domain]'),warning=form.querySelector('[data-publish-warning]');
  const sync=()=>{const value=input.value.trim().toLowerCase();if(input.value!==value)input.value=value;if(domain)domain.textContent=`${value||'xxx'}.${publishDomain}`;if(warning)warning.hidden=!value||value===current};
  input.addEventListener('input',sync);sync();input.focus();
}
async function submitPublish(slug,listed=true){
  await persistDraftSnapshot(JSON.stringify(state));
  await new Promise(resolve=>setTimeout(resolve,40));// 打包整站是同步计算，先让转圈动画绘制出来
  const payload=await consoleRequest('/api/publish',{method:'POST',body:{...buildPublishPayload(),slug,listed}});
  currentConsoleUser.publishSlug=payload.slug;currentConsoleUser.publishListed=listed;updateConsoleAccount();notifyPublishedReload('站点已重新发布');
  const credential=payload.siteAdmin?siteAdminCredentialMarkup(payload.siteAdmin):payload.accountEnabled?'<div class="site-account-existing"><p>本站独立账号数据已保留，重新发布不会覆盖用户和登录密码。</p><button type="button" data-site-account-reset>忘记站长密码？重置密码</button></div>':'';
  const replaced=payload.replacedSlug?`<p class="publish-warning">旧地址 ${esc(payload.replacedSlug)}.${esc(currentConsoleUser.publishDomain||'hatchery.mizusumi.com')} 的页面已删除。</p>`:'';
  const publicUrl=payload.publicUrl||payload.url;
  const galleryNote=listed?'<p class="publish-gallery-note">已收录进 <a href="/gallery" target="_blank" rel="noopener">Hatchery Gallery ↗</a>，其他同学能在收录页看到它。</p>':'<p class="publish-gallery-note">本次未收录进 Gallery；下次发布时勾选「收录进 Hatchery Gallery」即可出现在收录页。</p>';
  openModal('发布成功',`<p>网站已发布到 <b>${esc(publicUrl)}</b>，已经打开的发布页面会自动刷新。</p>${galleryNote}${replaced}${credential}<a class="modal-action" href="${esc(publicUrl)}" target="_blank">打开 ${esc(publicUrl)} ↗</a>`);
}
document.querySelector('#publishBtn').addEventListener('click',openPublishModal);
document.querySelector('#consoleAccountBtn').addEventListener('click',event=>{event.stopPropagation();const menu=document.querySelector('#consoleAccountMenu');const opening=menu.hidden||menu.classList.contains('is-closing');if(opening)uiShow(menu);else uiHide(menu);document.querySelector('#consoleAccountBtn').setAttribute('aria-expanded',String(opening))});
document.querySelector('#consoleAccountMenu').addEventListener('click',event=>event.stopPropagation());
document.querySelector('#consoleAccountAction').addEventListener('click',openPasswordSettings);
document.querySelector('#inviteManagerBtn').addEventListener('click',()=>void openInviteManager());
document.querySelector('#consoleLogoutBtn').addEventListener('click',()=>void logoutConsole());
document.querySelector('#previewModal').addEventListener('submit',event=>{const form=event.target.closest('[data-console-form]');if(!form)return;event.preventDefault();const data=new FormData(form);const button=form.querySelector('button');button.disabled=true;button.classList.add('is-loading');void(async()=>{try{if(form.dataset.consoleForm==='password'){const next=String(data.get('newPassword')||'');if(next!==String(data.get('confirmPassword')||''))throw new Error('两次输入的新密码不一致');await consoleRequest('/api/auth/change-password',{method:'POST',body:{oldPassword:String(data.get('oldPassword')||''),newPassword:next}});closeModal();showToast('密码已修改，其他设备的会话已退出')}else if(form.dataset.consoleForm==='ai-key'){const key=String(data.get('kimiApiKey')||'').trim();if(!key)throw new Error('请输入 Kimi API Key');await consoleRequest('/api/admin/ai-config',{method:'POST',body:{kimiApiKey:key}});showToast('Kimi Key 已保存并生效');void loadAiStatus();await openInviteManager()}else if(form.dataset.consoleForm==='publish'){await submitPublish(String(data.get('slug')||'').trim().toLowerCase(), data.get('listed')==='on')}}catch(error){showToast(error.message)}finally{button.disabled=false;button.classList.remove('is-loading')}})()});
document.querySelector('#previewModal').addEventListener('click',event=>{const aiChats=event.target.closest('[data-ai-chats]');if(aiChats){void openAiChats(aiChats.dataset.aiChats||'',aiChats.dataset.userName||'');return}if(event.target.closest('[data-ai-chats-back]')){void openInviteManager();return}const otherSessions=event.target.closest('[data-revoke-other-sessions]');if(otherSessions){otherSessions.disabled=true;void consoleRequest('/api/auth/sessions/revoke-others',{method:'POST',body:{}}).then(payload=>{showToast(`已退出其他设备（${payload.removed} 个会话）`);return openPasswordSettings()}).catch(error=>{otherSessions.disabled=false;showToast(error.message)});return}const userStatus=event.target.closest('[data-user-status]');if(userStatus){const verb=userStatus.dataset.userStatus==='disabled'?'停用':'启用';if(!window.confirm(`确认${verb}用户 ${userStatus.dataset.userName}？`))return;userStatus.disabled=true;void consoleRequest('/api/admin/users/status',{method:'POST',body:{username:userStatus.dataset.userName,status:userStatus.dataset.userStatus}}).then(()=>{showToast(`已${verb}用户`);return openInviteManager()}).catch(error=>{userStatus.disabled=false;showToast(error.message)});return}const aiKeyClear=event.target.closest('[data-ai-key-clear]');if(aiKeyClear){if(!window.confirm('确认清除服务器上的 Kimi API Key？若未配置 DeepSeek Key，AI 服务将不可用。'))return;aiKeyClear.disabled=true;void consoleRequest('/api/admin/ai-config',{method:'POST',body:{kimiApiKey:''}}).then(()=>{showToast('已清除 Kimi Key');void loadAiStatus();return openInviteManager()}).catch(error=>{aiKeyClear.disabled=false;showToast(error.message)});return}const reset=event.target.closest('[data-site-account-reset]');if(reset){if(!window.confirm('重置后，原站长密码会立即失效。确认继续？'))return;reset.disabled=true;void consoleRequest('/api/site-account/reset-owner',{method:'POST',body:{}}).then(payload=>{openModal('管理员密码已重置',siteAdminCredentialMarkup(payload.siteAdmin));showToast('本站管理员密码已重置')}).catch(error=>{reset.disabled=false;showToast(error.message)});return}const modalCopy=event.target.closest('[data-modal-action="copy"]');if(modalCopy){void navigator.clipboard?.writeText(modalCopy.dataset.copy||'');showToast('已复制本站管理员凭据');return}});
document.querySelectorAll('[data-context-action]').forEach(button=>button.addEventListener('click',()=>{const id=contextElementId;if(!id)return;const action=button.dataset.contextAction;hideElementContext();if(action==='edit')openElementEditor(id)}));document.addEventListener('click',event=>{if(!event.target.closest('#elementContextMenu'))hideElementContext()});document.addEventListener('scroll',hideElementContext,true);window.addEventListener('resize',hideElementContext);
document.addEventListener('click',()=>{const menu=document.querySelector('#consoleAccountMenu');if(menu&&!menu.hidden)uiHide(menu);document.querySelector('#consoleAccountBtn').setAttribute('aria-expanded','false')});

bindCanvasObjectChrome();renderPages();renderCanvas();
window.addEventListener('alchemyhatchery:authenticated',event=>void enterConsole(event.detail.user));
window.addEventListener('alchemyhatchery:logged-out',()=>{currentConsoleUser=null;setSaveState('等待登录',false)});
window.addEventListener('alchemyhatchery:toast',event=>showToast(event.detail.message));
