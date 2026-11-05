async ({fields, answers, email}) => {
 const clean = value => String(value ?? '').normalize('NFKC').replace(/\s+/g,' ').trim();
 const fold = value => clean(value).toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g,'').replace(/\u0111/g,'d');
 const visible = el => !!el && el.isConnected && !!el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
 const usable = el => visible(el) && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
 const waitUntil=async (predicate,milliseconds)=>{
   const deadline=performance.now()+milliseconds;
   do {const value=predicate();if(value)return value;if(performance.now()>=deadline)return null;
       await new Promise(resolve=>setTimeout(resolve,16));}while(true);
 };
 // Wait once before indexing: Google's page marker can update before controls
 // finish hydration. Planning caches are discarded logically before mutations.
 if(!await waitUntil(()=>{
   const controls=Array.from(document.querySelectorAll(
     '[role="listitem"] [role="radio"],[role="listitem"] [role="checkbox"],'
     +'[role="listitem"] input:not([type="hidden"]),[role="listitem"] textarea,'
     +'[role="listitem"] [role="listbox"],[role="listitem"] select')).filter(visible);
   return controls.every(el=>!el.disabled&&el.getAttribute('aria-disabled')!=='true');
 },15000))throw Error('Page controls are not ready');
 let planning=true;
 const visibility=new WeakMap(), labels=new WeakMap(), scopes=new WeakMap(), choices=new WeakMap();
 const planVisible=el=>{
   if(!planning)return visible(el);
   if(!el)return false;
   if(!visibility.has(el))visibility.set(el,visible(el));
   return visibility.get(el);
 };
 const planUsable=el=>planVisible(el)&&!el.disabled&&el.getAttribute('aria-disabled')!=='true';
 const label = el => {
   if(planning&&labels.has(el))return labels.get(el);
   const text=clean(el.getAttribute('aria-label') ||
     (el.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => document.getElementById(id)?.textContent || '').join(' ') ||
     Array.from(el.labels || []).map(item => item.textContent).join(' ') || el.textContent);
   if(planning)labels.set(el,text);
   return text;
 };
 const checked = el => el instanceof HTMLInputElement ? el.checked : el.getAttribute('aria-checked') === 'true';
 const nativeChoice = el => el instanceof HTMLInputElement && ['radio','checkbox'].includes(el.type);
 const roleControls = (scope, role) => {
   let cached=scopes.get(scope);
   if(cached?.has(role))return cached.get(role);
   const controls=Array.from(scope.querySelectorAll('[role="'+role+'"],input[type="'+role+'"]')).filter(planUsable);
   const controlSet=new Set(controls);
   // Exclude native inputs wrapped in a usable ARIA control without comparing
   // every control with every other control.
   const result=controls.filter(el=>{
     for(let parent=el.parentElement;parent&&parent!==scope;parent=parent.parentElement)
       if(controlSet.has(parent)&&parent.getAttribute('role')===role)return false;
     return true;
   });
   if(!cached){cached=new Map();scopes.set(scope,cached);}
   cached.set(role,result);
   return result;
 };
 const cards=Array.from(document.querySelectorAll('[role="listitem"]')).filter(planVisible);
 const cardSet=new Set(cards), cardInfo=new WeakMap(), names=new Map();
 const questions=new Map(fields.map(field=>[field.id,clean(field.question)]));
 const wantedNames=new Set(fields.flatMap(field=>[field.id,field.id+'_sentinel']));
 const infoFor=card=>{
   if(!cardInfo.has(card))cardInfo.set(card,{});
   return cardInfo.get(card);
 };
 const cardText=card=>{
   const info=infoFor(card);
   if(info.text===undefined)info.text=clean(card.innerText);
   return info.text;
 };
 const hasHeading=card=>{
   const info=infoFor(card);
   if(info.heading===undefined)info.heading=!!card.querySelector('[role="heading"],h1,h2,h3,h4');
   return info.heading;
 };
 const add=(index,key,card)=>{
   if(!index.has(key))index.set(key,new Set());
   index.get(key).add(card);
 };
 for(const element of document.querySelectorAll('[name]')) {
   const name=element.getAttribute('name');
   if(!wantedNames.has(name))continue;
   const card=element.closest('[role="listitem"]');
   if(cardSet.has(card))add(names,name,card);
 }
 let parameters=null, titles=null;
 const parameterCards=()=>{
   if(parameters)return parameters;
   parameters=new Map();
   const identifiers=new Set(fields.map(field=>field.id.split('.').pop()));
   for(const element of document.querySelectorAll('[data-params]')) {
     const ids=new Set((element.getAttribute('data-params')||'').match(/\d+/g)||[]);
     const relevant=[...ids].filter(id=>identifiers.has(id));
     if(!relevant.length)continue;
     // Include visible ancestor question cards, matching querySelectorAll's
     // descendant semantics even if a fixture contains nested listitems.
     for(let card=element.parentElement?.closest('[role="listitem"]');card;card=card.parentElement?.closest('[role="listitem"]'))
       if(cardSet.has(card))for(const id of relevant)add(parameters,id,card);
   }
   return parameters;
 };
 const titleCards=()=>{
   if(titles)return titles;
   titles=new Map();
   const root={children:new Map(),question:null};
   for(const question of new Set(questions.values())) {
     let node=root;
     for(const character of question) {
       if(!node.children.has(character))node.children.set(character,{children:new Map(),question:null});
       node=node.children.get(character);
     }
     node.question=question;
   }
   // Prefix trie reads each card's visible text at most once; duplicate titles
   // remain sets of candidates and are never resolved by DOM order.
   for(const card of cards) {
     let node=root;
     if(node.question!==null)add(titles,node.question,card);
     for(const character of cardText(card)) {
       node=node.children.get(character);
       if(!node)break;
       if(node.question!==null)add(titles,node.question,card);
     }
   }
   return titles;
 };
 const cardFor = field => {
   const named=new Set([...(names.get(field.id)||[]),...(names.get(field.id+'_sentinel')||[])]);
   if(named.size===1)return named.values().next().value;
   const question=questions.get(field.id), parameterMatches=parameterCards().get(field.id.split('.').pop())||new Set();
   const direct=[...parameterMatches].filter(card=>hasHeading(card)&&cardText(card).startsWith(question));
   if(direct.length===1)return direct[0];
   let candidates=[...(titleCards().get(question)||[])];
   const headings=candidates.filter(hasHeading);
   if(headings.length)candidates=headings;
   if(candidates.length===1)return candidates[0];
   const matches=candidates.filter(card=>parameterMatches.has(card));
   return matches.length===1?matches[0]:null;
 };
 const scopeFor = (card,field,role) => {
   if(!['grid','grid_multiple'].includes(field.kind)) return card;
   const wanted=clean(field.row);
   for(const candidateRole of ['radiogroup','group','row']) {
     const candidates=Array.from(card.querySelectorAll('[role="'+candidateRole+'"]')).filter(planVisible)
       .filter(el => roleControls(el,role).length === field.options.length);
     const exact=candidates.filter(el=>label(el)===wanted);
     if(exact.length===1) return exact[0];
     const partial=candidates.filter(el=>label(el).includes(wanted));
     if(partial.length===1) return partial[0];
   }
   const rowLabels=Array.from(card.querySelectorAll('*')).filter(el=>planVisible(el)&&clean(el.textContent)===wanted);
   const candidates=[];
   for(const node of rowLabels) {
     let candidate=node;
     for(let depth=0;candidate&&candidate!==card&&depth<6;depth++,candidate=candidate.parentElement)
       if(roleControls(candidate,role).length===field.options.length) candidates.push(candidate);
   }
   const unique=[...new Set(candidates)];
   return unique.length===1 ? unique[0] : null;
 };
 const choiceFor = (controls,field,value) => {
   let index=choices.get(controls);
   if(!index) {
     index={values:new Map(),labels:null};
     for(const control of controls) {
       const data=control.getAttribute('data-value');
       if(data!==null)add(index.values,data,control);
       add(index.values,clean(control.getAttribute('aria-label')),control);
     }
     choices.set(controls,index);
   }
   const text=String(value), exact=index.values.get(text);
   if(exact?.size===1)return exact.values().next().value;
   if(!index.labels) {
     index.labels=new Map();
     for(const control of controls)add(index.labels,label(control),control);
   }
   const matches=index.labels.get(text);
   if(matches?.size===1)return matches.values().next().value;
   if(field.kind==='scale'&&controls.length===field.options.length) {
     const position=field.options.indexOf(value);return position>=0?controls[position]:null;
   }
   return null;
 };
 const setText=(el,value)=>{
   if(!(el instanceof HTMLInputElement||el instanceof HTMLTextAreaElement)||el.readOnly) throw Error('Non-native textbox');
   const proto=el instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
   const setter=Object.getOwnPropertyDescriptor(proto,'value').set;
   setter.call(el,String(value));
   el.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:String(value)}));
   el.dispatchEvent(new Event('change',{bubbles:true}));
 };
 const serialized = field => Array.from(document.getElementsByName(field.id)).flatMap(el=>{
   if(el.disabled)return [];
   if(el instanceof HTMLInputElement&&['radio','checkbox'].includes(el.type))return el.checked?[String(el.value)]:[];
   if(el instanceof HTMLSelectElement)return Array.from(el.selectedOptions).map(option=>String(option.value));
   return el instanceof HTMLInputElement||el instanceof HTMLTextAreaElement?[String(el.value)]:[];
 }).filter(value=>value!=='');
 const same=(left,right)=>{
   if(left.length!==right.length)return false;
   const counts=new Map();
   for(const value of left)counts.set(value,(counts.get(value)||0)+1);
   for(const value of right) {
     const count=counts.get(value)||0;
     if(!count)return false;
     counts.set(value,count-1);
   }
   return true;
 };
 const dateText=value=>String(value.year).padStart(4,'0')+'-'+String(value.month).padStart(2,'0')+'-'+String(value.day).padStart(2,'0');
 const timeText=value=>String(value.hour).padStart(2,'0')+':'+String(value.minute).padStart(2,'0');

 const plans=[], failures=[];
 for(const field of fields) {
   try {
     if(!Object.prototype.hasOwnProperty.call(answers,field.id))throw Error('Missing answer');
     const value=answers[field.id],card=cardFor(field);
     if(!card)throw Error('Question card is not unique');
     if(field.kind==='dropdown') {
       const selects=Array.from(card.querySelectorAll('select')).filter(planUsable);
       if(selects.length) {
         if(selects.length!==1||selects[0].multiple||selects[0].name!==field.id||!selects[0].form)
           throw Error('Native dropdown is not unique or serializable');
         const select=selects[0],options=Array.from(select.options).filter(option=>!option.disabled&&
           (!option.parentElement.matches('optgroup')||!option.parentElement.disabled));
         const matches=options.filter(option=>String(option.value)===String(value)||clean(option.label)===String(value));
         if(matches.length!==1)throw Error('Native dropdown option is not unique');
         const option=matches[0];
         plans.push({field,apply:()=>{
           Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(select,option.value);
           select.dispatchEvent(new Event('input',{bubbles:true}));
           select.dispatchEvent(new Event('change',{bubbles:true}));
         },verify:()=>visible(select)&&select.selectedOptions.length===1&&select.selectedOptions[0]===option&&
           same(serialized(field),[String(value)])&&
           (!select.willValidate||select.validity.valid)});
         continue;
       }
       const boxes=Array.from(card.querySelectorAll('[role="listbox"]')).filter(planUsable);
       if(boxes.length!==1)throw Error('Dropdown listbox is not unique');
       const box=boxes[0],roots=[box,...(box.getAttribute('aria-controls')||box.getAttribute('aria-owns')||'')
         .split(/\s+/).map(id=>document.getElementById(id)).filter(Boolean)];
       const options=()=>[...new Set(roots.flatMap(root=>Array.from(root.querySelectorAll('[role="option"]'))))];
       if(!options().length&&roots.length===1)throw Error('Dropdown popup cannot be safely scoped');
       let opened=false,chosen=null;
       const dismiss=()=>{
         if(!opened)return;
         box.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',code:'Escape',bubbles:true,cancelable:true}));
         if(box.getAttribute('aria-expanded')==='true')box.click();
         opened=false;
       };
       plans.push({field,cleanup:dismiss,apply:async()=>{
         box.click();opened=true;
         const result=await waitUntil(()=>{
           const matches=options().filter(usable).filter(option=>
             option.getAttribute('data-value')===String(value)||label(option)===String(value));
           if(matches.length>1)throw Error('Dropdown option is not unique');
           return matches.length===1?matches[0]:null;
         },250);
         if(!result)throw Error('Dropdown popup is not ready');
         chosen=result;chosen.click();
       },verify:()=>{
         if(!visible(box)||!chosen)return false;
         // Some Google controllers replace the clicked popup item with a
         // selected display item. Verify semantic selection after that update.
         const selected=options().filter(option=>option.getAttribute('aria-selected')==='true');
         if(!selected.length||selected.some(option=>
           option.getAttribute('data-value')!==String(value)&&label(option)!==String(value)))return false;
         if(document.getElementsByName(field.id).length&&!same(serialized(field),[String(value)]))return false;
         dismiss();return true;
       }});
       continue;
     }
     if(['text','paragraph'].includes(field.kind)) {
       const boxes=Array.from(card.querySelectorAll('input:not([type="hidden"]),textarea')).filter(planUsable)
         .filter(el=>el instanceof HTMLTextAreaElement||['text','email','tel','url','search','number',''].includes(el.type));
       const named=boxes.filter(el=>el.name===field.id),box=named.length===1?named[0]:boxes.length===1?boxes[0]:null;
       if(!box||box.readOnly)throw Error('Textbox is not unique');
       plans.push({field,apply:()=>setText(box,value),verify:()=>visible(box)&&box.value===String(value)&&(!box.willValidate||box.validity.valid)});
       continue;
     }
     if(['date','time'].includes(field.kind)) {
       const inputs=Array.from(card.querySelectorAll('input:not([type="hidden"])')).filter(planUsable);
       if(!inputs.length)throw Error('No temporal inputs');
       const actions=[];
       for(const input of inputs) {
         if(input.type==='date')actions.push([input,dateText(value)]);
         else if(input.type==='datetime-local')actions.push([input,dateText(value)+'T'+timeText(value)]);
         else if(input.type==='time')actions.push([input,timeText(value)]);
         else {
           const text=fold(['name','aria-label','placeholder','title'].map(name=>input.getAttribute(name)||'').join(' '));
           const aliases={year:['year','nam'],month:['month','thang'],day:['day','ngay'],hour:['hour','gio'],minute:['minute','phut'],second:['second','giay']};
           const part=Object.keys(aliases).find(part=>aliases[part].some(alias=>new RegExp('(^|[^\\w])'+alias+'($|[^\\w])').test(text)||text.endsWith('_'+alias)));
           if(!part||value[part]===undefined)throw Error('Temporal input is not recognized');
           actions.push([input,String(value[part])]);
         }
       }
       if(actions.some(([input,text])=>input.readOnly||text.includes('undefined')))throw Error('Temporal input is readonly or incomplete');
       plans.push({field,apply:()=>actions.forEach(([input,text])=>setText(input,text)),verify:()=>actions.every(([input,text])=>visible(input)&&input.value===text&&(!input.willValidate||input.validity.valid))});
       continue;
     }
     const multiple=['multiple','grid_multiple'].includes(field.kind),role=multiple?'checkbox':'radio';
     const scope=scopeFor(card,field,role);
     if(!scope)throw Error('Grid row is not unique');
     const controls=roleControls(scope,role),wanted=multiple?value:[value];
     if(!Array.isArray(wanted)||!controls.length)throw Error('Choice controls are not recognized');
     const chosen=wanted.map(option=>choiceFor(controls,field,option));
     if(chosen.some(el=>!el)||new Set(chosen).size!==chosen.length)throw Error('Choice option is not unique');
     const native=controls.every(nativeChoice),chosenSet=new Set(chosen);
     plans.push({field,apply:()=>{
       if(multiple)for(const el of controls)if(checked(el)&&!chosenSet.has(el))el.click();
       for(const el of chosen)if(!checked(el))el.click();
     },verify:()=>{
       if(!chosen.every(el=>visible(el)&&checked(el)))return false;
       if(multiple&&controls.some(el=>checked(el)!==chosenSet.has(el)))return false;
       // Star/rating widgets mark every value up to the chosen value as checked.
       if(!multiple&&field.kind!=='scale'&&controls.some(el=>el!==chosen[0]&&checked(el)))return false;
       if(native)return chosen.every(el=>!!el.name&&el.form);
       // Google keeps choice answers in its controller until Next is clicked.
       // Some versions expose entry inputs; validate them when they exist.
       return !document.getElementsByName(field.id).length || same(serialized(field),wanted.map(String));
     }});
   } catch(error) {failures.push({id:field.id,message:String(error.message||error).slice(0,220)});}
 }
 planning=false;
 const applied=[];
 for(const plan of plans) {
   try {const result=plan.apply();if(result&&typeof result.then==='function')await result;applied.push(plan);}
   catch(error){plan.cleanup?.();failures.push({id:plan.field.id,message:String(error.message||error).slice(0,220)});}
 }
 // Google schedules controller updates after the click task. Verify them together
 // as soon as they settle, without repeating clicks or sleeping per question.
 const handled=[], pending=new Set(applied), deadline=performance.now()+1000;
 while(pending.size) {
   for(const plan of pending) {
     try {if(plan.verify()){handled.push(plan.field.id);pending.delete(plan);}}
     catch(error){pending.delete(plan);plan.cleanup?.();failures.push({id:plan.field.id,message:String(error.message||error).slice(0,220)});}
   }
   if(!pending.size||performance.now()>=deadline)break;
   await new Promise(resolve=>setTimeout(resolve,16));
 }
 for(const plan of pending){plan.cleanup?.();failures.push({id:plan.field.id,message:'The real input or selected state could not be verified'});}
 let emailHandled=true;
 const emails=Array.from(document.querySelectorAll('input[name="emailAddress"]')).filter(visible);
 for(const control of emails) {
   try {
     if(!control.value) {
       if(typeof email!=='string'||!email)throw Error('Missing email value');
       setText(control,email);
     }
     if(!control.value||control.disabled||control.readOnly||(control.willValidate&&!control.validity.valid))
       throw Error('Email input could not be verified');
   } catch(error) {emailHandled=false;}
 }
 const buttonNames=new Set(['ti\u1ebfp','ti\u1ebfp theo','next','g\u1eedi','submit']);
 const buttons=Array.from(document.querySelectorAll('button,[role="button"],input[type="submit"],input[type="button"]'))
   .filter(usable).map(element=>clean(element.innerText||element.value||element.getAttribute('aria-label')).toLowerCase())
   .filter(name=>buttonNames.has(name));
 const navigation={history:document.querySelector('input[name="pageHistory"]')?.value??null,
   next:buttons.some(name=>['ti\u1ebfp','ti\u1ebfp theo','next'].includes(name)),
   submit:buttons.some(name=>['g\u1eedi','submit'].includes(name))};
 return {handled,errors:failures,navigation,email_handled:emailHandled};
}
