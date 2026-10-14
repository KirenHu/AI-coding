/* One explicitly authorized document (top-level or same-site embedded frame).
 * Observe named actions, not typed values, page HTML, URLs or screenshots.
 * A local task-complete signal stops these listeners immediately.
 */
(()=>{
  if(globalThis.__worktwinCaptureActive)return;
  globalThis.__worktwinCaptureActive=true;
  let active=true;
  let lastFeedback="",lastFeedbackAt=0;
  const PRIVATE=/(password|passcode|secret|token|api.?key|authorization|ssn|身份证|手机号|银行卡|credit.?card|cc-number|email|phone|otp)/i;
  const looksPrivate=text=>{
    if(PRIVATE.test(text))return true;
    if(/\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b/i.test(text))return true;
    if(/(?:\+?86[- ]?)?1[3-9]\d{9}/.test(text))return true;
    if(/\b(?:\d[ -]?){12,19}\b/.test(text))return true;
    return false;
  };
  function clean(text){
    const value=String(text||"").replace(/\s+/g," ").trim().slice(0,85);
    return looksPrivate(value)?"[隐藏]":value;
  }
  function sensitive(el){
    if(!el?.matches)return false;
    const own=[el.getAttribute("name"),el.getAttribute("id"),
      el.getAttribute("autocomplete"),el.getAttribute("type"),
      el.getAttribute("aria-label")].join(" ");
    if(PRIVATE.test(own))return true;
    return !!el.closest('input[type="password"],input[type="hidden"],[autocomplete="one-time-code"],[autocomplete^="cc-"]');
  }
  function describe(node){
    const el=node?.closest?.("button,a,input,select,textarea,[role=button],[role=checkbox],[role=radio]");
    if(!el || sensitive(el))return "";
    const isAction=el.matches("button,a,[role=button]");
    const isForm=el.matches("input,select,textarea,[role=checkbox],[role=radio]");
    const type=isAction?"按钮":isForm?"字段":"控件";
    const explicit=el.getAttribute("aria-label")||el.getAttribute("title");
    const labelled=el.labels?.[0]?.textContent ||
      (el.id?document.querySelector('label[for="'+CSS.escape(el.id)+'"]')?.textContent:"");
    // Text is read only from the small interactive element, not surrounding
    // page content. Never inspect input.value or select.selectedOptions.
    const text=isAction?el.textContent:"";
    const label=clean(explicit||labelled||text||
      el.getAttribute("placeholder")||el.getAttribute("name")||el.tagName.toLowerCase());
    return type+"："+label;
  }
  function emit(kind,value){
    if(!active)return;
    chrome.runtime.sendMessage({type:"capture:event",kind,label:clean(value)})
      .then(result=>{if(result?.ok===false)stop()})
      .catch(stop);
  }
  function safeAction(e,kind){
    // composedPath preserves the actual control in an open shadow-root dialog.
    const target=e.composedPath?.()[0]||e.target;
    const node=target?.closest?.("button,a,input,select,textarea,[role=button],[role=checkbox],[role=radio]");
    if(node && sensitive(node))return; // Even interaction metadata can be sensitive.
    emit(kind,describe(target));
  }
  const click=e=>safeAction(e,"click");
  const change=e=>safeAction(e,"change");
  const submit=e=>safeAction(e,"submit");
  document.addEventListener("click",click,true);
  document.addEventListener("change",change,true);
  document.addEventListener("submit",submit,true);
  function classifyFeedback(node){
    const element=(node?.nodeType===1?node:node?.parentElement)?.closest?.(
      '[role="alert"],[role="status"],[aria-live="assertive"],[aria-live="polite"]');
    if(!element)return;
    const text=String(element.textContent||"").trim().slice(0,160);
    if(!text)return;
    const state=/(保存成功|提交成功|操作成功|已保存|success|saved successfully)/i.test(text)
      ?"feedback_success"
      :/(失败|错误|保存失败|提交失败|error|failed)/i.test(text)
      ?"feedback_failure":"feedback_unknown";
    const now=Date.now();
    if(state===lastFeedback && now-lastFeedbackAt<1200)return;
    lastFeedback=state;lastFeedbackAt=now;
    // Only a classification leaves the page, not feedback text.
    emit("feedback",state);
  }
  const observer=new MutationObserver(records=>{
    if(!active)return;
    for(const r of records){
      classifyFeedback(r.target);
      for(const n of Array.from(r.addedNodes||[]).slice(0,8))classifyFeedback(n);
    }
  });
  observer.observe(document.documentElement||document,{subtree:true,
    childList:true,characterData:true,attributes:true,
    attributeFilter:["role","aria-live"]});
  function stop(){
    if(!active)return;
    active=false;
    globalThis.__worktwinCaptureActive=false;
    observer.disconnect();
    document.removeEventListener("click",click,true);
    document.removeEventListener("change",change,true);
    document.removeEventListener("submit",submit,true);
    chrome.runtime.onMessage.removeListener(onMessage);
  }
  function onMessage(message){
    if(message?.type==="capture:stop")stop();
  }
  chrome.runtime.onMessage.addListener(onMessage);
})();
