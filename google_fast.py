"""Batch ordinary Google Forms UI actions without submitting or editing entry data.

One browser evaluation handles a visible page. Unknown controls or values whose
real state cannot be verified are returned for the normal Playwright fallback.
The caller must still validate answers, traverse every required page and verify
the final server confirmation. No hidden input is assigned by this module.
"""
from __future__ import annotations

import time


class FastFillError(ValueError):
    """The batch transport failed; callers may use the normal UI path."""


_FILL_PAGE = r"""async ({fields, answers}) => {
 const clean = value => String(value ?? '').normalize('NFKC').replace(/\s+/g,' ').trim();
 const fold = value => clean(value).toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g,'').replace(/đ/g,'d');
 const visible = el => !!el && el.isConnected && !!el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
 const usable = el => visible(el) && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
 const label = el => clean(el.getAttribute('aria-label') ||
   (el.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => document.getElementById(id)?.textContent || '').join(' ') ||
   Array.from(el.labels || []).map(item => item.textContent).join(' ') || el.textContent);
 const checked = el => el instanceof HTMLInputElement ? el.checked : el.getAttribute('aria-checked') === 'true';
 const nativeChoice = el => el instanceof HTMLInputElement && ['radio','checkbox'].includes(el.type);
 const roleControls = (scope, role) => Array.from(scope.querySelectorAll('[role="'+role+'"],input[type="'+role+'"]')).filter(usable)
   .filter((el, i, all) => !all.some(other => other !== el && other.contains(el) && other.getAttribute('role') === role));
 const cards = Array.from(document.querySelectorAll('[role="listitem"]')).filter(visible).map(el => ({el,text:clean(el.innerText)}));
 const cardFor = field => {
   const named = [field.id,field.id+'_sentinel'].flatMap(name => Array.from(document.getElementsByName(name)))
      .map(el => el.closest('[role="listitem"]')).filter(visible);
   const unique = [...new Set(named)];
   if(unique.length === 1) return unique[0];
   let candidates = cards.filter(item => item.text.startsWith(clean(field.question)));
   const headings = candidates.filter(item => item.el.querySelector('[role="heading"],h1,h2,h3,h4'));
   if(headings.length) candidates=headings;
   if(candidates.length === 1) return candidates[0].el;
   const number=field.id.split('.').pop();
   const matches=candidates.filter(item => Array.from(item.el.querySelectorAll('[data-params]')).some(el => {
      const values=(el.getAttribute('data-params')||'').match(/\d+/g)||[];return values.includes(number);
   }));
   return matches.length === 1 ? matches[0].el : null;
 };
 const scopeFor = (card,field,role) => {
   if(!['grid','grid_multiple'].includes(field.kind)) return card;
   const wanted=clean(field.row);
   for(const candidateRole of ['radiogroup','group','row']) {
     const candidates=Array.from(card.querySelectorAll('[role="'+candidateRole+'"]')).filter(visible)
       .filter(el => roleControls(el,role).length === field.options.length);
     const exact=candidates.filter(el=>label(el)===wanted);
     if(exact.length===1) return exact[0];
     const partial=candidates.filter(el=>label(el).includes(wanted));
     if(partial.length===1) return partial[0];
   }
   const labels=Array.from(card.querySelectorAll('*')).filter(el=>visible(el)&&clean(el.textContent)===wanted);
   const candidates=[];
   for(const node of labels) {
     let candidate=node;
     for(let depth=0;candidate&&candidate!==card&&depth<6;depth++,candidate=candidate.parentElement)
       if(roleControls(candidate,role).length===field.options.length) candidates.push(candidate);
   }
   const unique=[...new Set(candidates)];
   return unique.length===1 ? unique[0] : null;
 };
 const choiceFor = (controls,field,value) => {
   let matches=controls.filter(el=>el.getAttribute('data-value')===String(value)||clean(el.getAttribute('aria-label'))===String(value));
   if(matches.length===1) return matches[0];
   matches=controls.filter(el=>label(el)===String(value));
   if(matches.length===1) return matches[0];
   if(field.kind==='scale'&&controls.length===field.options.length) {
     const index=field.options.indexOf(value);return index>=0 ? controls[index] : null;
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
 const same=(left,right)=>left.length===right.length&&[...left].sort().every((value,index)=>value===[...right].sort()[index]);
 const dateText=value=>String(value.year).padStart(4,'0')+'-'+String(value.month).padStart(2,'0')+'-'+String(value.day).padStart(2,'0');
 const timeText=value=>String(value.hour).padStart(2,'0')+':'+String(value.minute).padStart(2,'0');
 const waitUntil=async (predicate,milliseconds)=>{
   const deadline=performance.now()+milliseconds;
   do {const value=predicate();if(value)return value;if(performance.now()>=deadline)return null;
       await new Promise(resolve=>setTimeout(resolve,16));}while(true);
 };
 const plans=[], failures=[];
 for(const field of fields) {
   try {
     if(!Object.prototype.hasOwnProperty.call(answers,field.id))throw Error('Missing answer');
     const value=answers[field.id],card=cardFor(field);
     if(!card)throw Error('Question card is not unique');
     if(field.kind==='dropdown') {
       const selects=Array.from(card.querySelectorAll('select')).filter(usable);
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
       const boxes=Array.from(card.querySelectorAll('[role="listbox"]')).filter(usable);
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
       const boxes=Array.from(card.querySelectorAll('input:not([type="hidden"]),textarea')).filter(usable)
         .filter(el=>el instanceof HTMLTextAreaElement||['text','email','tel','url','search','number',''].includes(el.type));
       const named=boxes.filter(el=>el.name===field.id),box=named.length===1?named[0]:boxes.length===1?boxes[0]:null;
       if(!box||box.readOnly)throw Error('Textbox is not unique');
       plans.push({field,apply:()=>setText(box,value),verify:()=>visible(box)&&box.value===String(value)&&(!box.willValidate||box.validity.valid)});
       continue;
     }
     if(['date','time'].includes(field.kind)) {
       const inputs=Array.from(card.querySelectorAll('input:not([type="hidden"])')).filter(usable);
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
     const native=controls.every(nativeChoice);
     plans.push({field,apply:()=>{
       if(multiple)for(const el of controls)if(checked(el)&&!chosen.includes(el))el.click();
       for(const el of chosen)if(!checked(el))el.click();
     },verify:()=>{
       if(!chosen.every(el=>visible(el)&&checked(el)))return false;
       if(multiple&&controls.some(el=>checked(el)!==chosen.includes(el)))return false;
       // Star/rating widgets mark every value up to the chosen value as checked.
       if(!multiple&&field.kind!=='scale'&&controls.some(el=>el!==chosen[0]&&checked(el)))return false;
       if(native)return chosen.every(el=>!!el.name&&el.form);
       // Google keeps choice answers in its controller until Next is clicked.
       // Some versions expose entry inputs; validate them when they exist.
       return !document.getElementsByName(field.id).length || same(serialized(field),wanted.map(String));
     }});
   } catch(error) {failures.push({id:field.id,message:String(error.message||error).slice(0,220)});}
 }
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
 return {handled,errors:failures};
}"""


def fill_google_page(page, fields, answers):
    """Fill supported visible controls in one DOM call, never navigating/submitting.

    ``handled`` contains only verified fields. Callers must fill every ``fallback``
    field with the existing path and retain page/confirmation validation.
    """
    started = time.perf_counter()
    fields = list(fields)
    identifiers = [field["id"] for field in fields]
    if len(set(identifiers)) != len(identifiers):
        raise FastFillError("Mã câu hỏi trùng trong trang đang điền.")
    try:
        # pageHistory changes before Google's controllers finish enabling inputs.
        # Wait for readiness once per page, instead of retrying every question.
        page.wait_for_function("""() => {
            const visible=el=>!!el.getClientRects().length&&getComputedStyle(el).visibility!=='hidden';
            const controls=Array.from(document.querySelectorAll(
                '[role="listitem"] [role="radio"],[role="listitem"] [role="checkbox"],'
                +'[role="listitem"] input:not([type="hidden"]),[role="listitem"] textarea,'
                +'[role="listitem"] [role="listbox"],[role="listitem"] select')).filter(visible);
            return controls.every(el=>!el.disabled&&el.getAttribute('aria-disabled')!=='true');
        }""", timeout=15000)
        result = page.evaluate(_FILL_PAGE, {"fields": fields, "answers": {identifier: answers[identifier]
                               for identifier in identifiers if identifier in answers}})
        handled = result.get("handled") if isinstance(result, dict) else None
        if not isinstance(handled, list) or any(not isinstance(identifier, str) or identifier not in identifiers for identifier in handled):
            raise ValueError("Invalid batch result")
    except Exception as exc:
        raise FastFillError("Không dùng được bộ điền nhanh; cần chuyển sang cách điền thông thường.") from exc
    verified = set(handled)
    errors = result.get("errors", [])
    if not isinstance(errors, list):
        errors = []
    return {"handled": [identifier for identifier in identifiers if identifier in verified],
            "fallback": [identifier for identifier in identifiers if identifier not in verified],
            "errors": errors, "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)}
