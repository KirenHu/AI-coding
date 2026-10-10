/* WorkTwin MV3 content script: dormant on all ordinary pages.
 * It never reads/copies page DOM until a signed workflow ticket has been
 * accepted by the local WorkTwin service and this exact document is bound.
 */
"use strict";
(() => {
  if (window !== window.top) return;
  let monitoring = false;
  let sessionId = "";
  let seq = 0;
  let pending = [];
  let sending = false;
  let retryTimer = null;
  let observer = null;
  let lastGesture = 0;
  let lastFeedback = 0;
  let messageError = false;

  const pathOnly = value => {
    try {
      const u = new URL(value, location.href);
      return u.protocol === "https:" ? u.origin + u.pathname : "";
    } catch { return ""; }
  };
  const currentPath = () => pathOnly(location.href);
  const compact = text => String(text || "").replace(/\s+/g, " ").trim().slice(0, 100);
  const safelyLabel = element => {
    if (!element || element.closest("[contenteditable],input,textarea,select,[data-private],[data-sensitive]")) return "";
    const tag = element.tagName?.toLowerCase() || "";
    if (!["button", "a", "summary"].includes(tag) &&
        element.getAttribute("role") !== "button") return "";
    return compact(element.getAttribute("aria-label") || element.getAttribute("title")
      || element.textContent || "");
  };
  const emit = (kind, details) => {
    if (!monitoring || !sessionId || !currentPath()) return;
    seq += 1;
    if (pending.length >= 400) {
      // Do not retain unlimited site data; sequence gaps reveal loss.
      pending.shift();
    }
    pending.push({seq,kind,occurred_at:Date.now(),details});
    flush();
  };
  const flush = () => {
    if (!monitoring || sending || !pending.length) return;
    if (retryTimer) {clearTimeout(retryTimer);retryTimer=null}
    const batch = pending.slice(0, 25);
    sending = true;
    chrome.runtime.sendMessage({type:"WT_EVENTS",session_id:sessionId,events:batch}, result => {
      sending = false;
      const failure = chrome.runtime.lastError;
      if (!failure && result?.ok) {
        pending.splice(0, batch.length);
      } else if (result?.stop) {
        stop();
        return;
      }
      if (pending.length) retryTimer = setTimeout(flush, failure || !result?.ok ? 1200 : 150);
    });
  };
  const click = event => {
    if (!monitoring || !event.isTrusted) return;
    const el = event.target instanceof Element ? event.target : null;
    const actionable = el?.closest("button,a,summary,[role=button]");
    if (!actionable || actionable.closest("[data-private],[data-sensitive]")) return;
    const label = safelyLabel(actionable);
    emit("click", {tag:compact(actionable.tagName?.toLowerCase()),
      role:compact(actionable.getAttribute("role") || ""), label});
    if (actionable.matches("a[href]")) {
      const to = pathOnly(actionable.href);
      if (to) emit("navigation_intent",{tag:"a",target_path:to});
    }
  };
  const changed = event => {
    if (!monitoring || !event.isTrusted) return;
    const element = event.target;
    if (!(element instanceof HTMLElement) || element.closest("[data-private],[data-sensitive]")) return;
    // Passwords, typed text, selected values, files and autocomplete
    // attributes are never included. No keylogging or document snapshot.
    if (!element.matches("input,select,textarea")) return;
    const type = element.matches("select") ? "select" :
      element.matches("textarea") ? "textarea" : (element.getAttribute("type") || "text");
    if (["password", "file", "hidden", "tel", "email", "search"].includes(type)) return;
    emit("change",{tag:element.tagName.toLowerCase(),field_type:compact(type)});
  };
  const submitted = event => {
    if (monitoring && event.isTrusted) emit("submit",{tag:"form"});
  };
  const mutate = entries => {
    if (!monitoring || Date.now() - lastFeedback < 800) return;
    for (const entry of entries) {
      // A live region may already exist and receive only a new text node;
      // include its parent without scanning or copying unrelated page DOM.
      const candidates = [entry.target,...(entry.addedNodes ? Array.from(entry.addedNodes) : [])];
      for (const node of candidates.slice(0, 5)) {
        if (!(node instanceof HTMLElement)) continue;
        let live = null;
        if (node.matches("[role=status],[role=alert],[aria-live=polite],[aria-live=assertive]")) {
          live = node;
        } else {
          live = node.closest("[role=status],[role=alert],[aria-live=polite],[aria-live=assertive]") ||
            node.querySelector("[role=status],[role=alert],[aria-live=polite],[aria-live=assertive]");
        }
        if (!live || live.matches("input,textarea") ||
            live.closest("[data-private],[data-sensitive],[contenteditable]") ||
            live.querySelector("input,textarea,[contenteditable]")) continue;
        const label = compact(live.textContent);
        if (!label) continue;
        lastFeedback = Date.now();
        emit("feedback",{role:compact(live.getAttribute("role") || "status"),label});
        return;
      }
    }
  };
  function stop() {
    monitoring = false;
    sessionId = "";
    pending = [];
    if (retryTimer) clearTimeout(retryTimer);
    retryTimer = null;
    observer?.disconnect();
    observer = null;
    document.removeEventListener("click",click,true);
    document.removeEventListener("change",changed,true);
    document.removeEventListener("submit",submitted,true);
  }
  function start(message) {
    if (monitoring) stop();
    if (pathOnly(message.target_path) !== currentPath() || !message.session_id) return false;
    sessionId = String(message.session_id);
    seq = 0;
    monitoring = true;
    document.addEventListener("click",click,true);
    document.addEventListener("change",changed,true);
    document.addEventListener("submit",submitted,true);
    // MutationObserver is only instantiated for an explicitly bound page.
    observer = new MutationObserver(mutate);
    observer.observe(document.documentElement || document, {childList:true,subtree:true});
    return true;
  }
  document.addEventListener("click", event => {
    if (event.isTrusted) lastGesture = Date.now();
  }, true);

  // Called synchronously from a normal workflow button click; the original
  // link/navigation is never cancelled or routed through WorkTwin.
  window.addEventListener("message", event => {
    if (event.source !== window || event.origin !== location.origin) return;
    const value = event.data;
    if (!value || value.channel !== "worktwin.workflow" ||
        !["capture.start", "capture.complete"].includes(value.type)) return;
    // Only START is tied to a real user click. A signed COMPLETE may be
    // emitted asynchronously when the workflow backend marks a task finished.
    if (value.type === "capture.start" &&
        (!Number.isFinite(lastGesture) || Date.now() - lastGesture > 1500)) return;
    if (typeof value.ticket !== "string" || value.ticket.length > 4500) return;
    chrome.runtime.sendMessage({type:"WT_FLOW_SIGNAL",ticket:value.ticket,
      source_origin:location.origin}, () => {void chrome.runtime.lastError});
  });

  chrome.runtime.onMessage.addListener((msg,_sender,reply) => {
    if (msg?.type === "WT_ENABLE") {
      reply({ok:start(msg)});
    } else if (msg?.type === "WT_DISABLE") {
      stop();
      reply({ok:true});
    }
  });
  // Ready events carry no page content, only identity metadata. WorkTwin will
  // not receive them, and the extension ignores them without a pending task.
  chrome.runtime.sendMessage({type:"WT_PAGE_READY"},() => {void chrome.runtime.lastError});
  window.addEventListener("pagehide",stop,{once:true});
})();
