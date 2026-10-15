/* OhMySmell: checkout ownership proof, isolated from Tilda order pricing. */
(function () {
  "use strict";
  if (window.__omsCheckoutLinkInstalled) return;
  window.__omsCheckoutLinkInstalled = true;

  const ENDPOINT = new URL("/integrations/tilda/telegram-link", (document.currentScript && document.currentScript.src) || "https://ohmysmell-backend-production.up.railway.app").href;
  const STORAGE_KEY = "oms-last-checkout-nonce-v1";
  const BOT = "https://t.me/OhMySmell_OrdersBot";
  const fieldName = "oms_checkout_nonce_hash";
  const prepared = new WeakSet();
  const checkoutDebug = {loaded: true, scanned: 0, bound: 0, lastError: null};
  // Read-only diagnostics: never expose the nonce, its hash or order details.
  window.__omsCheckoutLinkStatus = checkoutDebug;
  let overlay;
  let panelControls;

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
    if (overlay) return panelControls;
    overlay = document.createElement("section");
    overlay.setAttribute("role", "status");
    overlay.setAttribute("aria-live", "polite");
    overlay.style.cssText = "position:fixed;bottom:max(16px,env(safe-area-inset-bottom));right:16px;left:auto;z-index:2147483600;width:calc(100vw - 32px);max-width:360px;box-sizing:border-box;background:#fffaf7;border:1px solid #e6d8d4;border-radius:14px;padding:20px;font:15px/1.5 Arial,sans-serif;color:#352e2d;box-shadow:0 6px 32px #0002";
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
    const noTelegram = document.createElement("button");
    noTelegram.type = "button";
    noTelegram.textContent = "У меня нет Telegram";
    noTelegram.style.cssText = "display:block;background:transparent;color:#644647;border:0;padding:12px 0 0;text-decoration:underline;cursor:pointer;font:inherit";
    noTelegram.addEventListener("click", function () {
      status.textContent = "Заказ уже принят! Telegram не обязателен. Менеджер сможет связаться с вами по телефону или email, указанным при оформлении.";
      link.style.display = "none";
    });
    const again = document.createElement("button");
    again.type = "button";
    again.textContent = "Получить новую ссылку";
    again.style.cssText = "display:none;border:1px solid #aa7f80;background:transparent;color:#644647;border-radius:8px;padding:9px;margin-top:10px;cursor:pointer";
    const close = document.createElement("button");
    close.type = "button";
    close.textContent = "×";
    close.setAttribute("aria-label", "Закрыть");
    close.style.cssText = "position:absolute;right:12px;top:8px;background:transparent;border:0;font-size:24px;cursor:pointer";
    close.addEventListener("click", () => { overlay.remove(); overlay = null; panelControls = null; });
    overlay.append(title, status, link, noTelegram, again, close);
    document.body.append(overlay);
    panelControls = {status, link, again, noTelegram};
    return panelControls;
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
          ui.status.textContent = "Заказ принят. Для отслеживания откройте Telegram и нажмите Start в боте. Если Telegram не установлен, можно дождаться связи по контактам из заказа.";
          ui.link.href = body.deep_link;
          ui.link.style.display = "block";
          const expiresAt = Date.now() + Number(body.expires_in_seconds || 1800) * 1000;
          ui.link.onclick = event => {
            if (Date.now() >= expiresAt - 10000) {
              event.preventDefault();
              displayLink(nonce);
            }
          };
          ui.again.style.display = "inline-block";
          ui.again.onclick = () => displayLink(nonce);
          return;
        }
        if (response.status !== 404 && response.status !== 503) break;
      } catch (_) {}
    }
    ui.status.textContent = "Заказ принят даже без Telegram. Если бот временно недоступен, менеджер сможет связаться по телефону или email из заказа. Попробуйте получить ссылку позднее.";
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
    hidden.className = "t-input";
    form.appendChild(hidden);
    // Tilda's existing phone/email fields are the fallback if Telegram
    // isn't installed. Ask explicitly which contact method the buyer prefers.
    // This control is optional; it does not gate normal Tilda checkout.
    const selector = form.querySelector && form.querySelector("[type=submit]");
    if (selector && selector.parentNode && !form.querySelector("[name=oms_contact_preference]")) {
      const preference = document.createElement("select");
      preference.name = "oms_contact_preference";
      preference.className = "t-input";
      preference.setAttribute("aria-label", "Как удобнее связаться по заказу?");
      preference.style.cssText = "display:block;width:100%;min-height:44px;box-sizing:border-box;margin:10px 0";
      const choices = [
        ["any", "Любым способом: телефон или email"],
        ["phone", "Позвонить по телефону"],
        ["email", "Написать на email"],
        ["telegram", "Telegram (если установлен)"]
      ];
      choices.forEach(([value, caption]) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = caption;
        preference.appendChild(option);
      });
      const caption = document.createElement("label");
      caption.style.cssText = "display:block;font-size:14px;margin-top:10px";
      caption.append("Как связаться по заказу (Telegram необязателен)", preference);
      selector.parentNode.insertBefore(caption, selector);
    }
    let nonce = token();
    hidden.value = await digest(nonce);
    checkoutDebug.bound += 1;
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
    // Tilda may move the cart popup outside its original record or render
    // a new .t706__orderform only after the user opens their shopping cart.
    // form4616518301 is the observed production cart form.
    const selector = [
      "form#form4616518301",
      ".t706 form",
      ".t706__cartwin form",
      ".t706__orderform",
      "#rec4616518301 form"
    ].join(", ");
    try {
      const forms = document.querySelectorAll(selector);
      checkoutDebug.scanned = Math.max(checkoutDebug.scanned, forms.length);
      forms.forEach(form => {
        if (form.tagName !== "FORM") return;
        prepare(form).catch(error => {
          checkoutDebug.lastError = error && error.name || "prepare_error";
        });
      });
    } catch (error) {
      checkoutDebug.lastError = error && error.name || "selector_error";
    }
  }
  function init() {
    if (typeof crypto === "undefined" || !crypto.subtle) {
      checkoutDebug.lastError = "webcrypto_unavailable";
      return;
    }
    findForms();
    const observer = new MutationObserver(findForms);
    observer.observe(document.documentElement, {childList: true, subtree: true});
    try {
      const lastNonce = sessionStorage.getItem(STORAGE_KEY);
      if (lastNonce && /^[A-Za-z0-9_-]{43}$/.test(lastNonce)) displayLink(lastNonce);
    } catch (_) {}
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
