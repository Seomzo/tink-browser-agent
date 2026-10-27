(() => {
  if (!document.body) return null;
  const cache = window.__jevFast ||= {ids:new WeakMap(), nodes:new Map(), next:1};
  const identity = e => {
    if (!cache.ids.has(e)) cache.ids.set(e,cache.next++);
    const id=cache.ids.get(e); cache.nodes.set(id,e); return id;
  };
  for (const [id,e] of cache.nodes) if (!e.isConnected) cache.nodes.delete(id);
  const safe = e => !['file','hidden'].includes(e.type);
  // Keep password values inside the page; an opaque revision preserves freshness.
  cache.passwords ||= new WeakMap();
  cache.fieldValue = e => {
    if (e.type !== 'password') return e.value ?? null;
    const previous = cache.passwords.get(e);
    const revision = previous ? previous.revision + Number(previous.value !== e.value) : 0;
    cache.passwords.set(e, {value:e.value, revision});
    return {filled:Boolean(e.value), revision};
  };
  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const name = (e,seen=new Set()) => {
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const referenced=(e.getAttribute('aria-labelledby')||'').split(/\s+/)
      .map(id=>name(document.getElementById(id),seen)).filter(Boolean).join(' ');
    return referenced || e.getAttribute('aria-label') ||
      [...(e.labels||[])].map(l=>name(l,seen)).filter(Boolean).join(' ') ||
      (['button','submit','reset'].includes(e.type) ? e.value : '') || e.getAttribute('alt') ||
      (e.tagName==='INPUT' ? '' : [...e.childNodes].map(n=>n.nodeType===3 ? n.textContent :
        n.nodeType===1 && n.getAttribute('aria-hidden')!=='true' ? name(n,seen) : '').join(' ').trim()) ||
      e.getAttribute('title') || e.getAttribute('placeholder') || '';
  };
  const roles=['button','link','checkbox','radio','switch','tab','menuitem','menuitemradio',
    'option','gridcell','combobox','textbox','searchbox','spinbutton'];
  const selector='a[href],button,input,textarea,select,summary,[contenteditable="true"],'+
    roles.map(role=>'[role="'+role+'"]').join(',');
  cache.hitPoint = e => {
    const r=e.getBoundingClientRect(), left=Math.max(0,r.left), top=Math.max(0,r.top);
    const right=Math.min(innerWidth,r.right), bottom=Math.min(innerHeight,r.bottom);
    if (right<=left || bottom<=top) return null;
    for (const [fx,fy] of [[.5,.5],[.2,.2],[.8,.2],[.2,.8],[.8,.8]]) {
      const x=left+(right-left)*fx, y=top+(bottom-top)*fy, hit=document.elementFromPoint(x,y);
      if (!hit || !e.contains(hit)) continue;
      // A card's nested button is a different action, even when the card contains it.
      const owner=hit.closest(selector);
      if (owner && owner!==e && e.contains(owner)) continue;
      return {x,y};
    }
    return null;
  };
  cache.context = e => {
    const own=name(e).replace(/\s+/g,' ').trim();
    for (let p=e, depth=0; p && p!==document.body && depth<4; p=p.parentElement, depth++) {
      const text=(p.innerText||'').replace(/\s+/g,' ').trim();
      if (text.length>400) break;
      if (text && text!==own) return text;
    }
    return '';
  };
  const role = e => {
    const explicit=e.getAttribute('role');
    if (roles.includes(explicit)) return explicit;
    if (e.tagName==='BUTTON' || e.tagName==='SUMMARY') return 'button';
    if (e.tagName==='A') return 'link';
    if (e.tagName==='SELECT') return 'combobox';
    if (e.tagName==='TEXTAREA' || e.isContentEditable) return 'textbox';
    if (e.tagName==='INPUT') {
      if (['checkbox','radio'].includes(e.type)) return e.type;
      if (['button','submit','reset','image'].includes(e.type)) return 'button';
      if (e.type==='search') return 'searchbox';
      if (e.type==='number') return 'spinbutton';
      if (['text','email','url','tel','password'].includes(e.type)) return 'textbox';
    }
    return null;
  };
  cache.pageKey=()=>[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    [...document.querySelectorAll('input,textarea,select')].filter(safe)
      .map(e=>[identity(e),cache.fieldValue(e),e.checked,e.selectedIndex,e.disabled,e.readOnly])];
  cache.guard=e=>{
    if (!e?.isConnected || !visible(e)) return null;
    const scope=e.closest('form,dialog,[role="dialog"],article,li,tr,[role="row"]') || e.parentElement;
    return [identity(e),role(e),name(e),cache.fieldValue(e),e.checked??null,e.selectedIndex??null,
      e.readOnly??null,e.matches(':disabled'),e.getAttribute('aria-disabled'),
      e.getAttribute('aria-expanded'),e.getAttribute('aria-checked'),e.getAttribute('aria-selected'),
      e.getAttribute('href'),scope?.innerText?.slice(0,6000)||'',cache.context(e)];
  };
  cache.escapeTarget=()=>{
    const overlays=[...document.querySelectorAll('dialog[open],[role="dialog"],[role="menu"]')]
      .filter(e=>{
        if (!visible(e)) return false;
        const r=e.getBoundingClientRect();
        const x=Math.max(0,r.left)+(Math.min(innerWidth,r.right)-Math.max(0,r.left))/2;
        const y=Math.max(0,r.top)+(Math.min(innerHeight,r.bottom)-Math.max(0,r.top))/2;
        return r.bottom>0 && r.top<innerHeight && r.right>0 && r.left<innerWidth &&
          e.contains(document.elementFromPoint(x,y));
      });
    const focused=document.activeElement?.closest('dialog[open],[role="dialog"],[role="menu"]');
    return overlays.includes(focused) ? focused : overlays.at(-1);
  };
  cache.keyAllowed=(e,key)=>{
    if (!e?.isConnected || !visible(e)) return false;
    if (key==='Escape') return cache.escapeTarget()===e;
    return key==='Enter' && e===document.activeElement && e.tagName==='INPUT' &&
      ['text','search','email','url','tel','number','password'].includes(e.type) &&
      !e.readOnly && !e.matches(':disabled') && !e.closest('[aria-disabled="true"]') &&
      e.getAttribute('aria-readonly')!=='true' && Boolean(cache.hitPoint(e));
  };
  const actions=[];
  for (const e of document.querySelectorAll(selector)) {
    if (!safe(e) || !visible(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"]')) continue;
    const r=e.getBoundingClientRect(), rname=role(e);
    if (!rname || !cache.hitPoint(e)) continue;
    if (rname==='gridcell' && e.querySelector('button,[role="button"]')) continue;
    const base={node:identity(e),role:rname,label:name(e)||rname,
      rect:{x:r.x,y:r.y,w:r.width,h:r.height}};
    const context=cache.context(e);
    if (context) base.context=context;
    if (e.tagName==='A' && /^https?:/.test(e.href)) base.href=e.href.slice(0,2000);
    for (const key of ['checked','selected','expanded']) {
      const value=e.getAttribute('aria-'+key);
      if (value!==null) base[key]=value;
    }
    if (['checkbox','radio'].includes(e.type)) base.checked=String(e.checked);
    if (e.tagName==='SELECT') {
      for (const o of e.options) if (!o.selected && !o.disabled && !o.closest('optgroup[disabled]'))
        actions.push({...base,kind:'select',value:o.value,
          current_value:[...e.selectedOptions].map(o=>o.label).join(', '),label:base.label+' → '+o.label});
    } else {
      const editable=!e.readOnly && e.getAttribute('aria-readonly')!=='true' &&
        (['textbox','searchbox','spinbutton'].includes(rname) ||
          (rname==='combobox' && ['INPUT','TEXTAREA'].includes(e.tagName)));
      const sensitive=e.type==='password';
      const value=sensitive ? (e.value ? '[filled]' : '') : 'value' in e ? String(e.value) :
        e.isContentEditable || rname==='combobox' ? e.innerText.trim() : '';
      actions.push({...base,kind:editable?'fill':'click',value,sensitive});
      if (editable) actions.push({...base,kind:'click',value,sensitive,label:'Open '+base.label});
    }
  }
  const focused=document.activeElement, overlay=cache.escapeTarget();
  if (cache.keyAllowed(focused,'Enter')) actions.unshift({node:identity(focused),kind:'key',key:'Enter',
    role:'keyboard',label:'Press Enter in '+(name(focused)||'focused field')});
  if (overlay) actions.unshift({node:identity(overlay),kind:'key',key:'Escape',role:'keyboard',
    label:'Press Escape for '+(name(overlay).slice(0,160)||'open dialog or menu')});
  const words=[], walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
  const range=document.createRange(); let node,length=0;
  while ((node=walker.nextNode()) && length<6000) {
    const value=node.textContent.trim(), parent=node.parentElement;
    if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent)) continue;
    range.selectNodeContents(node); const r=range.getBoundingClientRect();
    if (r.width>0 && r.height>0 && r.bottom>0 && r.top<innerHeight && r.right>0 && r.left<innerWidth) {
      words.push(value); length+=value.length;
    }
  }
  const text=words.join('\n').slice(0,6000), height=document.documentElement.scrollHeight;
  const page_key=cache.pageKey(), guards={};
  for (const a of actions) if (!(a.node in guards)) guards[a.node]=cache.guard(cache.nodes.get(a.node));
  // Compare meaning and identity. Geometry is always resolved and hit-tested just before input.
  const semantics=actions.map(({rect,...action})=>action);
  const marker=[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    document.title,text,semantics,page_key[6],focused ? identity(focused) : null];
  const omitted_actions=Math.max(0,actions.length-250);
  actions.splice(250);
  actions.forEach((a,i)=>a.id='e'+(i+1));
  if (scrollY+innerHeight<height-2) actions.push({id:'scroll_down',kind:'scroll',label:'Scroll down',delta:560});
  if (scrollY>0) actions.push({id:'scroll_up',kind:'scroll',label:'Scroll up',delta:-560});
  actions.push({id:'wait',kind:'wait',label:'Wait for the page to update'});
  const authText=/verification|authentication|one.time|security code|two.factor/i.test(text);
  const codeField=actions.some(a=>a.kind==='fill' && /code|otp/i.test(a.label)) ||
    [...document.querySelectorAll('input[autocomplete="one-time-code"]')].some(visible);
  const authenticationRequired=authText && (codeField ||
    (/enter.{0,30}code|code.{0,30}sent/i.test(text) && actions.some(a=>a.kind==='fill')));
  return {authenticationRequired,url:location.href,title:document.title,w:innerWidth,h:innerHeight,text,
    scroll:{y:scrollY,height},actions,marker,page_key,guards,omitted_actions};
})()
