/* OhMySmell: checkout ownership proof, isolated from Tilda order pricing. */
(function () {
  "use strict";
  if (window.__omsCheckoutLinkInstalled) return;
  window.__omsCheckoutLinkInstalled = true;

  const ENDPOINT = "https://ohmysmell-backend-production.up.railway.app/integrations/tilda/telegram-link";
  const STORAGE_KEY = "oms-last-checkout-nonce-v1";
  const BOT = "https://t.me/OhMySmell_OrdersBot";
  const fieldName = "oms_checkout_nonce_hash";
  const prepared = new WeakSet();
  let overlay;

  function token() {
    const bytes = new Uint8Array(32);
    crypto.getRandomValues(bytes);
    let binary = "";
    bytes.forEach(b => binary += String.fromCharCode(b));
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  async function digest(value) {
    const bytes = new TextEncoder().encode(value);
    const result = await crypto.subtle.digest("SHA-256", bytes);
    return [...new Uint8Array(result)].map(b => b.toString(16).padStart(2, "0")).join("");
  }

  function panel() {
    if (overlay) return overlay;
    overlay = document.createElement("section");
    overlay.setAttribute("role", "status");
    overlay.setAttribute("aria-live", "polite");
    overlay.style.cssText = "position:fixed;bottom:20px;right:20px;z-index:2147483600;max-width:360px;background:#fffaf7;border:1px solid #e6d8d4;border-radius:14px;padding:22px;font:15px/1.5 Arial,sans-serif;color:#352e2d;box-shadow:0 6px 32px #0002";
    const title = document.createElement("div");
    title.textContent = "Ваш заказ принят";
    title.style.cssText = "font-weight:bold;font-size:18px;margin-bottom:10px";
    const status = document.createElement("p");
    status.style.cssText = "margin:0 0 12px";
    const link = document.createElement("a");
    link.textContent = "Открыть заказ в Telegram";
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.referrerPolicy = "no-referrer";
    link.style.cssText = "display:none;background:#aa7f80;color:#fff;text-decoration:none;padding:11px 14px;border-radius:8px;text-align:center";
    const again = document.createElement("button");
    again.type = "button";
    again.textContent = "Получить новую ссылку";
    again.style.cssText = "display:none;border:1px solid #aa7f80;background:transparent;color:#644647;border-radius:8px;padding:9px;margin-top:10px;cursor:pointer";
    const close = document.createElement("button");
    close.type = "button";
    close.textContent = "×";
    close.setAttribute("aria-label", "Закрыть");
    close.style.cssText = "position:absolute;right:12px;top:8px;background:transparent;border:0;font-size:24px;cursor:pointer";
    close.addEventListener("click", () => { overlay.remove(); overlay = null; });
    overlay.append(title, status, link, again, close);
    document.body.append(overlay);
    return {status, link, again};
  }

  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  async function displayLink(nonce) {
    const ui = panel();
    ui.status.textContent = "Готовим персональную ссылку на ваш заказ…";
    ui.link.style.display = "none";
    ui.again.style.display = "none";
    for (const delay of [0, 500, 1000, 2000, 3500, 5500]) {
      if (delay) await sleep(delay);
      try {
        const response = await fetch(ENDPOINT, {
          method: "POST",
          mode: "cors",
          credentials: "omit",
          cache: "no-store",
          redirect: "error",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({checkout_nonce: nonce})
        });
        if (response.ok) {
          const body = await response.json();
          if (!/^https:\/\/t\.me\/OhMySmell_OrdersBot\?start=[A-Za-z0-9_-]{43}$/.test(body.deep_link)) throw Error("unexpected_link");
          ui.status.textContent = "Перейдите по персональной ссылке и нажмите Start в Telegram.";
          ui.link.href = body.deep_link;
          ui.link.style.display = "block";
          ui.link.onclick = () => { try { sessionStorage.removeItem(STORAGE_KEY); } catch (_) {} };
          return;
        }
        if (response.status !== 404 && response.status !== 503) break;
      } catch (_) {}
    }
    ui.status.textContent = "Заказ сохранён. Ссылка пока недоступна. Попробуйте ещё раз или свяжитесь с магазином.";
    ui.again.style.display = "inline-block";
    ui.again.onclick = () => displayLink(nonce);
    ui.link.href = BOT;
  }

  async function prepare(form) {
    if (prepared.has(form)) return;
    prepared.add(form);
    const hidden = document.createElement("input");
    hidden.type = "hidden";
    hidden.name = fieldName;
    form.appendChild(hidden);
    let nonce = token();
    hidden.value = await digest(nonce);
    form.addEventListener("tildaform:aftersuccess", function () {
      if (!hidden.value) return;
      const completed = nonce;
      try { sessionStorage.setItem(STORAGE_KEY, completed); } catch (_) {}
      displayLink(completed);
      // Reuse of this same form for a second real order must get a new proof.
      nonce = token();
      digest(nonce).then(hash => { hidden.value = hash; });
    });
  }

  function findForms() {
    document.querySelectorAll("#rec4616518301 form, .t706__cartwin form").forEach(prepare);
  }
  function init() {
    if (!crypto || !crypto.subtle) return;
    findForms();
    new MutationObserver(findForms).observe(document.documentElement, {childList: true, subtree: true});
    try {
      const lastNonce = sessionStorage.getItem(STORAGE_KEY);
      if (lastNonce && /^[A-Za-z0-9_-]{43}$/.test(lastNonce)) displayLink(lastNonce);
    } catch (_) {}
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
