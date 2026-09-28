PAGE = r'''<!doctype html>
<html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>total-agent-memory — общая память</title>
<style>
:root{color-scheme:dark;font:16px system-ui;background:#101821;color:#e5edf5}body{max-width:1080px;margin:auto;padding:24px}
h1{font-size:26px}small,.muted{color:#a8bbcc}section,article{background:#192532;padding:20px;border:1px solid #36495c;border-radius:12px;margin:16px 0}
label{display:block;margin:12px 0}input,select,textarea,button{font:inherit;padding:10px;border:1px solid #566d83;border-radius:6px;background:#101821;color:inherit;box-sizing:border-box}
input,textarea{width:100%}textarea{min-height:110px}button{cursor:pointer;background:#245e87;margin:8px 8px 0 0}button:disabled{opacity:.5;cursor:wait}
button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:3px solid #8ad3ff;outline-offset:2px}
a{color:#8ad3ff}pre{white-space:pre-wrap;overflow-wrap:anywhere}#status{min-height:24px;color:#a9dbff}nav{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
</style>
<h1>total-agent-memory</h1><small>__VERSION__ · __DATE__ · <a href="/dashboard/">Панель компании</a></small>
<section id="login"><label>Личный токен<input id="token" type="password" autocomplete="off"></label><button id="connect">Подключиться</button></section>
<p id="status" role="status" aria-live="polite"></p>
<main id="main" hidden><nav><strong id="actor"></strong><button id="logout">Выйти</button></nav>
<section><h2>Поиск</h2><label>Область<select id="searchScope"><option value="">Все доступные</option></select></label>
<label>Запрос<input id="query" type="search"></label><button id="search">Найти</button><button id="browse">Просмотреть выбранную область</button><button id="more" hidden>Следующая страница</button></section>
<section><h2>Сохранить</h2><label>Область<select id="writeScope"></select></label><label>Текст<textarea id="content"></textarea></label>
<label>Проект<input id="project" value="general"></label><label>Тематические теги через запятую<input id="tags"></label><button id="save">Сохранить</button></section>
<div id="results"></div></main>
<script>
let credential='',cursor=0,workspaces=[];
const el=id=>document.getElementById(id);
function scopeOf(id){return JSON.parse(el(id).value||'{"kind":"personal"}');}
function label(scope){return scope.kind==='personal'?'Личная':scope.kind==='shared'?'Общая':'Команда: '+scope.team_id;}
async function call(name,args={}){const r=await fetch('/api/call',{method:'POST',headers:{'Authorization':'Bearer '+credential,'Content-Type':'application/json'},body:JSON.stringify({name,arguments:args})});const data=await r.json();if(!r.ok)throw Error(data.error||'Ошибка запроса');return data;}
async function run(action){const buttons=[...document.querySelectorAll('button')];buttons.forEach(b=>b.disabled=true);el('status').textContent='Выполняется…';try{await action();el('status').textContent='Готово';}catch(e){el('status').textContent=e.message;}finally{buttons.forEach(b=>b.disabled=false);}}
function show(records){el('results').replaceChildren();for(const {scope,record} of records){const card=document.createElement('article');const heading=document.createElement('strong');heading.textContent=label(scope)+' · #'+record.id+' · ревизия '+record.revision;const author=document.createElement('p');author.className='muted';author.textContent='Автор: '+record.created_by.display_name+' · Последняя правка: '+record.updated_by.display_name;const text=document.createElement('pre');text.textContent=record.content;card.append(heading,author,text);
const history=document.createElement('button');let after=0;history.textContent='История';history.onclick=()=>run(async()=>{const r=await call('memory_history',{scope,id:record.id,after});for(const event of r.data){const view=document.createElement('section');const title=document.createElement('strong');title.textContent=new Date(event.at).toLocaleString()+' · '+event.actor.display_name+' · '+event.actor.client;const reason=document.createElement('p');reason.textContent=event.reason||({insert:'Сохранение',update:'Изменение',delete:'Удаление'}[event.operation]||event.operation);const before=document.createElement('pre');before.textContent=event.before_state?'До: '+event.before_state.content:'';const next=document.createElement('pre');next.textContent=event.after_state?'После: '+event.after_state.content+' ['+event.after_state.status+']':'Запись удалена';view.append(title,reason,before,next);card.append(view);}after=r.data.length?r.data.at(-1).sequence:after;history.textContent='Следующие изменения';history.hidden=r.data.length<50;});card.append(history);
if(workspaces.some(w=>JSON.stringify(w.scope)===JSON.stringify(scope)&&w.writable)&&record.status!=='deleted'&&record.status!=='superseded'){const edit=document.createElement('button');edit.textContent='Редактировать';edit.onclick=()=>{const area=document.createElement('textarea');area.value=record.content;area.setAttribute('aria-label','Новый текст');const reason=document.createElement('input');reason.placeholder='Причина изменения';reason.setAttribute('aria-label','Причина изменения');const submit=document.createElement('button');submit.textContent='Сохранить правку';submit.onclick=()=>run(async()=>{const r=await call('memory_update',{scope,id:record.id,expected_revision:record.revision,content:area.value,reason:reason.value});show([{scope,record:r.data}]);});card.append(area,reason,submit);};card.append(edit);}el('results').append(card);}}
el('connect').onclick=()=>run(async()=>{credential=el('token').value.trim();const data=await call('memory_scopes');workspaces=data.workspaces;el('actor').textContent=data.actor.display_name;el('token').value='';el('searchScope').length=1;el('writeScope').replaceChildren();for(const w of workspaces){const option=new Option(label(w.scope),JSON.stringify(w.scope));el('searchScope').add(option);if(w.writable)el('writeScope').add(option.cloneNode(true));}el('login').hidden=true;el('main').hidden=false;});
el('logout').onclick=()=>{credential='';el('main').hidden=true;el('login').hidden=false;el('results').replaceChildren();el('content').value='';el('query').value='';el('status').textContent='Вы вышли';};
el('search').onclick=()=>run(async()=>{const r=await call('memory_recall',{query:el('query').value,scope:el('searchScope').value?scopeOf('searchScope'):null});el('more').hidden=true;show(r.results);});
async function browse(){if(!el('searchScope').value)throw Error('Выберите одну область');const scope=scopeOf('searchScope');const r=await call('memory_export',{scope,after:cursor,limit:50});show(r.data.map(record=>({scope,record})));cursor=r.data.length?r.data.at(-1).id:cursor;el('more').hidden=r.data.length<50;}
el('browse').onclick=()=>run(async()=>{cursor=0;await browse();});el('more').onclick=()=>run(browse);el('searchScope').onchange=()=>{cursor=0;el('more').hidden=true;};
el('save').onclick=()=>run(async()=>{const scope=scopeOf('writeScope');const r=await call('memory_save',{scope,content:el('content').value,project:el('project').value,tags:el('tags').value.split(',').map(t=>t.trim()).filter(Boolean)});if(r.data.saved===false)throw Error('Запись отклонена проверкой качества');show([{scope,record:r.data}]);el('content').value='';});
</script></html>'''
