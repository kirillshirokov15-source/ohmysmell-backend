"use strict";
// Safe browser-mocking smoke test: no real Tilda, Telegram or customer orders.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const {webcrypto} = require("node:crypto");

(async () => {
  let availableForms = [];
  const addedControls = [];
  const submit = {parentNode: {insertBefore(element) { addedControls.push(element); }}};
  const form = {
    querySelector(selector) {
      if (selector === "[type=submit]") return submit;
      if (selector === "[name=oms_contact_preference]") return addedControls[0] || null;
      return null;
    },
    tagName: "FORM",
    id: "form4616518301",
    appended: [],
    listeners: {},
    appendChild(el) { this.appended.push(el); },
    addEventListener(event, callback) { this.listeners[event] = callback; }
  };
  let mutationCallback;
  const doc = {
    readyState: "complete",
    currentScript: {src: "https://example.invalid/integrations/tilda/checkout-link.js"},
    documentElement: {},
    querySelectorAll(selector) {
      assert.ok(selector.includes("form#form4616518301"));
      return availableForms;
    },
    createElement(tag) {
      return {tagName: tag.toUpperCase(), style: {}, children: [],
        setAttribute() {}, append(...children) {this.children.push(...children);},
        appendChild(child) {this.children.push(child);},
        addEventListener() {}};
    },
    body: {append() {}}
  };
  const browser = {
    window: {},
    document: doc,
    MutationObserver: class {
      constructor(callback) { mutationCallback = callback; }
      observe() {}
    },
    crypto: webcrypto,
    TextEncoder,
    Uint8Array,
    btoa: data => Buffer.from(data, "binary").toString("base64"),
    URL,
    WeakSet,
    sessionStorage: {getItem() {return null;}},
    console,
    setTimeout
  };
  const checkoutSource = fs.readFileSync("app/static/tilda_checkout_link.js", "utf8");
  assert.match(checkoutSource, /const ENDPOINT = "https:\/\/ohmysmell-backend-production\.up\.railway\.app\/integrations\/tilda\/telegram-link"/);
  assert.doesNotMatch(checkoutSource, /new URL\([^\n]*document\.currentScript/);
  vm.runInNewContext(checkoutSource, browser);
  assert.equal(typeof mutationCallback, "function", "must watch the dynamic Tilda cart");

  availableForms = [form];
  mutationCallback();
  for (let attempt = 0; attempt < 25 && !form.appended[0]?.value; attempt++) {
    await new Promise(resolve => setTimeout(resolve, 20));
  }
  assert.equal(form.appended.length, 1, "must inject one hidden field in the real cart form");
  assert.equal(form.appended[0].type, "hidden");
  assert.equal(form.appended[0].name, "oms_checkout_nonce_hash");
  assert.equal(addedControls.length, 1, "must offer contact method without forcing Telegram");
  assert.equal(addedControls[0].children[1].name, "oms_contact_preference");
  assert.deepEqual(addedControls[0].children[1].children.map(x => x.value),
      ["any", "phone", "email", "telegram"]);
  assert.match(form.appended[0].value, /^[0-9a-f]{64}$/);
  assert.equal(typeof form.listeners["tildaform:aftersuccess"], "function");
  assert.equal(browser.window.__omsCheckoutLinkStatus.bound, 1);

  mutationCallback();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(form.appended.length, 1, "must not append duplicates");
  console.log("Tilda dynamic-cart hidden-field smoke test passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
