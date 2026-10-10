/* Injected into exactly one authorized top-level document.
 * No keys, typed values, page HTML, arbitrary DOM dumps or screenshots leave this script.
 */
(()=>{
  if(window!==window.top || globalThis.__worktwinCaptureActive)return;
  globalThis.__worktwinCaptureActive=true;
  let active=true;
  const label=node=>{
    const el=node?.closest?.("button,a,input,select,textarea,[role=button]")||node;
    if(!el || el.closest?.("[type=password],[autocomplete='one-time-code']"))return "";
    const raw=el.getAttribute?.("aria-label")||el.getAttribute?.("title")||
      el.getAttribute?.("name")||el.tagName?.toLowerCase()||"";
    return String(raw).slice(0,100); // never read .value, innerText, HTML or href query
  };
  const emit=(kind,value)=>{
    if(!active)return;
    chrome.runtime.sendMessage({type:"capture:event",kind,label:String(value||"").slice(0,100)})
      .catch(()=>{active=false});
  };
  document.addEventListener("click",e=>emit("click",label(e.target)),true);
  document.addEventListener("change",e=>emit("change",label(e.target)),true);
  document.addEventListener("submit",e=>emit("submit",label(e.target)),true);
  const observer=new MutationObserver(records=>{
    for(const r of records){
      const node=r.target?.nodeType===1?r.target:r.target?.parentElement;
      if(node?.getAttribute?.("role")==="alert" || node?.getAttribute?.("aria-live")==="assertive"){
        emit("feedback",node.getAttribute("role")||"assertive");
        break;
      }
    }
  });
  observer.observe(document.documentElement,{subtree:true,childList:true,attributes:true,attributeFilter:["role","aria-live"]});
  chrome.runtime.onMessage.addListener(msg=>{
    if(msg?.type==="capture:stop"){active=false;observer.disconnect()}
  });
})();