'use strict';
const $=id=>document.getElementById(id), tg=window.Telegram?.WebApp;
let editor, revision=0, dirty=false, saving=false, preview=false, editable=false;
const params=new URLSearchParams(location.search), chat=params.get('chat_id');
const api=(window.RULES_CONFIG?.apiBase||'').replace(/\/$/,'');
function status(text,error=false){$('status').textContent=text;$('status').classList.toggle('error',error)}
function refresh(){ $('save').disabled=!dirty||saving; }
function view(){editor.enable(editable&&!preview);$('toolbar').hidden=!editable||preview;document.body.classList.toggle('readonly',!editable||preview);$('preview').textContent=preview?'Продолжить редактирование':'Предпросмотр'}
async function request(method,body){
 const response=await fetch(`${api}/rules-api/chats/${chat}`,{method,headers:{Authorization:`tma ${tg.initData}`,...(body?{'Content-Type':'application/json'}:{})},...(body?{body:JSON.stringify(body)}:{})});
 let result;try{result=await response.json()}catch{throw new Error(`Сервер вернул ${response.status}. Проверьте HTTPS и адрес API.`)}
 if(!response.ok)throw new Error(result.error||`Ошибка ${response.status}`);return result;
}
function markDirty(){dirty=true;tg?.enableClosingConfirmation?.();status('Есть несохранённые изменения. Они не публикуются автоматически.');refresh()}
async function start(){
 tg?.ready();tg?.expand();
 if(!tg?.initData){status('Откройте приложение кнопкой из личного чата с ботом. В обычном браузере доступа нет.',true);return}
 if(!/^-[0-9]+$/.test(chat||'')){status('Не указана группа. Откройте редактор через /rules edit.',true);return}
 if(!api.startsWith('https://')||api.includes('example.com')){status('Администратору сервера нужно настроить apiBase в config.js.',true);return}
 if(!window.Quill){status('Не загрузился редактор. Проверьте подключение к CDN.',true);return}
 editor=new Quill('#editor',{theme:'snow',readOnly:true,placeholder:'Напишите правила группы…',modules:{toolbar:'#toolbar'},formats:['header','bold','italic','underline','strike','color','background','list','indent','align','blockquote','code-block','code','link']});
 try{
  const doc=await request('GET');revision=doc.revision;
  editor.setContents(doc.delta,'silent');$('title').textContent=doc.chat_title;
  editable=doc.can_edit&&params.get('mode')==='edit';$('controls').hidden=!editable;view();
  status(doc.revision?`Версия ${revision} · обновлено ${new Date(doc.updated_at*1000).toLocaleString('ru-RU')}`:'Правила пока не добавлены.');
  editor.on('text-change',(_,__,source)=>{if(source==='user')markDirty()});
 }catch(error){status(error.message||'Не удалось связаться с сервером. Проверьте адрес API и CORS.',true)}
}
$('save').onclick=async()=>{
 if(!editable||saving)return;
 saving=true;refresh();status('Сохранение…');
 try{
  if(editor.getLength()>100001)throw new Error('Максимум 100 000 символов.');
  const result=await request('PUT',{revision,delta:editor.getContents()});revision=result.revision;dirty=false;tg?.disableClosingConfirmation?.();status(`Сохранено · версия ${revision}`);tg?.HapticFeedback?.notificationOccurred('success');
 }catch(error){status(error.message||'Ошибка сети. Ваш текст остался в редакторе. Попробуйте сохранить снова.',true)}
 finally{saving=false;refresh()}
};
$('preview').onclick=()=>{preview=!preview;view()};
$('export').onclick=()=>{
 const blob=new Blob([JSON.stringify({delta:editor.getContents()},null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`rules-${chat}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);
 status('Экспорт запрошен. Если Telegram не скачивает файл, используйте браузерный клиент Telegram.');
};
$('import').onchange=async event=>{
 const file=event.target.files[0];if(!file)return;
 try{if(file.size>1000000)throw new Error('Файл больше 1 МБ.');const data=JSON.parse(await file.text());const delta=data.delta||data;
 if(!Array.isArray(delta.ops)||delta.ops.some(op=>typeof op.insert!=='string'))throw new Error('Нужен JSON-файл правил, ранее экспортированный из редактора.');
 if(delta.ops.reduce((n,op)=>n+op.insert.length,0)>100001)throw new Error('Слишком большой документ.');
 if(dirty&&!confirm('Заменить несохранённые изменения импортированным текстом?'))return;
 editor.setContents(delta,'silent');markDirty();
 }catch(error){status(error.message,true)}event.target.value='';
};
window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue=''}});
start();
