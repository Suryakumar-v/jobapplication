"""JavaScript snippets evaluated inside the page. Kept as constants so they can be reviewed."""

from __future__ import annotations

FIELD_ATTRIBUTE = "data-ja-field"
SUBMIT_ATTRIBUTE = "data-ja-submit"

# Stops every form submission path in the page. Only the approved submission task sets
# window.__jaAllowSubmit, and only around its single click.
SUBMIT_GUARD_JS = """
(() => {
  const blocked = () => window.__jaAllowSubmit !== true;
  const count = () => { window.__jaBlockedSubmits = (window.__jaBlockedSubmits || 0) + 1; };
  window.addEventListener('submit', (event) => {
    if (blocked()) { event.preventDefault(); event.stopImmediatePropagation(); count(); }
  }, true);
  const nativeSubmit = HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit = function () {
    if (blocked()) { count(); return; }
    return nativeSubmit.call(this);
  };
  const nativeRequestSubmit = HTMLFormElement.prototype.requestSubmit;
  HTMLFormElement.prototype.requestSubmit = function (...args) {
    if (blocked()) { count(); return; }
    return nativeRequestSubmit.apply(this, args);
  };
})();
"""

DISCOVER_FIELDS_JS = """
(attribute) => {
  const clean = (text) => (text || '').replace(/\\s+/g, ' ').trim().slice(0, 300);
  const visible = (el) => {
    const style = getComputedStyle(el);
    const box = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none'
      && (box.width > 0 || box.height > 0);
  };
  const skipTypes = ['hidden', 'submit', 'button', 'image', 'reset', 'password'];
  const labelFor = (el) => {
    const parts = [];
    if (el.labels) { for (const l of el.labels) { parts.push(l.textContent); } }
    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      for (const id of labelledBy.split(/\\s+/)) {
        const ref = document.getElementById(id);
        if (ref) { parts.push(ref.textContent); }
      }
    }
    return clean(parts.join(' '));
  };
  const groupLabel = (el) => {
    const fieldset = el.closest('fieldset');
    const legend = fieldset ? fieldset.querySelector('legend') : null;
    return legend ? clean(legend.textContent) : '';
  };
  const results = [];
  const radioGroups = new Map();
  let counter = 0;
  for (const el of document.querySelectorAll('input, select, textarea')) {
    const tag = el.tagName.toLowerCase();
    const type = tag === 'input' ? (el.getAttribute('type') || 'text').toLowerCase() : '';
    if (skipTypes.includes(type) || el.disabled || el.readOnly) { continue; }
    if (type !== 'file' && !visible(el)) { continue; }
    if (type === 'radio' && el.name) {
      const existing = radioGroups.get(el.name);
      if (existing) {
        existing.options.push(labelFor(el) || el.value);
        if (el.checked) { existing.has_value = true; }
        continue;
      }
    }
    const index = counter++;
    el.setAttribute(attribute, String(index));
    const entry = {
      index,
      tag,
      input_type: type || tag,
      name: el.getAttribute('name') || '',
      element_id: el.id || '',
      autocomplete: el.getAttribute('autocomplete') || '',
      label: labelFor(el),
      aria_label: clean(el.getAttribute('aria-label')),
      placeholder: clean(el.getAttribute('placeholder')),
      group_label: groupLabel(el),
      options: [],
      required: el.required || el.getAttribute('aria-required') === 'true',
      has_value: type === 'checkbox' || type === 'radio' ? el.checked : (el.value || '') !== '',
    };
    if (tag === 'select') {
      entry.options = Array.from(el.options).map((o) => clean(o.text)).filter((t) => t);
    } else if (type === 'radio') {
      entry.options = [labelFor(el) || el.value];
      if (el.name) { radioGroups.set(el.name, entry); }
    }
    results.push(entry);
  }
  return results;
}
"""

DETECT_BLOCKERS_JS = """
() => {
  const found = [];
  const visible = (el) => {
    const style = getComputedStyle(el);
    const box = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && box.width > 0;
  };
  const captchaSelector = [
    'iframe[src*="recaptcha" i]', 'iframe[src*="hcaptcha" i]',
    'iframe[src*="challenges.cloudflare.com" i]', '.g-recaptcha', '.h-captcha',
    '[data-sitekey]', '.cf-turnstile',
  ].join(',');
  if (document.querySelector(captchaSelector)) {
    found.push({kind: 'CAPTCHA', detail: 'A CAPTCHA widget is present'});
  }
  if (Array.from(document.querySelectorAll('input[type=password]')).some(visible)) {
    found.push({kind: 'LOGIN', detail: 'A password field is present; sign-in is required'});
  }
  const text = (document.body ? document.body.innerText : '').slice(0, 20000);
  const codePrompt = /(verification|security|one[- ]time|authenticator)\\s+code/i;
  const factorPrompt = /two[- ]factor|\\b2fa\\b|multi[- ]factor/i;
  if (codePrompt.test(text) || factorPrompt.test(text)) {
    found.push({kind: 'MFA', detail: 'The page asks for a verification code'});
  }
  if (/verify (that )?you are (a )?human|are you a robot|unusual traffic/i.test(text)) {
    found.push({kind: 'CAPTCHA', detail: 'The page asks for a human check'});
  }
  return found;
}
"""

EXTRACT_JOB_JS = """
() => {
  const clean = (text) => (text || '').replace(/[ \\t]+/g, ' ').trim();
  const meta = (name) => {
    const el = document.querySelector(
      `meta[property="${name}"], meta[name="${name}"]`);
    return el ? clean(el.getAttribute('content')) : '';
  };
  const postings = [];
  for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      const data = JSON.parse(script.textContent);
      const stack = Array.isArray(data) ? data.slice() : [data];
      while (stack.length) {
        const node = stack.pop();
        if (!node || typeof node !== 'object') { continue; }
        if (Array.isArray(node['@graph'])) { stack.push(...node['@graph']); }
        const type = node['@type'];
        if (type === 'JobPosting' || (Array.isArray(type) && type.includes('JobPosting'))) {
          postings.push(node);
        }
      }
    } catch (error) { /* malformed JSON-LD is ignored */ }
  }
  const main = document.querySelector('main, article, [role=main]') || document.body;
  const heading = document.querySelector('h1');
  return {
    json_ld: postings.length ? postings[0] : null,
    heading: heading ? clean(heading.textContent) : '',
    site_name: meta('og:site_name'),
    og_title: meta('og:title'),
    page_title: clean(document.title),
    body_text: main ? (main.innerText || '').slice(0, 100000) : '',
  };
}
"""

# Uses the browser's own validation, so required and malformed fields block exactly as they would
# on a real submit. Returns only labels, never values. Marks the single submit control, if any.
CHECK_SUBMIT_JS = """
(attribute) => {
  const clean = (text) => (text || '').replace(/\\s+/g, ' ').trim().slice(0, 100);
  const visible = (el) => {
    const style = getComputedStyle(el);
    const box = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none'
      && (box.width > 0 || box.height > 0);
  };
  const labelOf = (el) => {
    const parts = [];
    if (el.labels) { for (const l of el.labels) { parts.push(l.textContent); } }
    return clean(parts.join(' ')) || clean(el.getAttribute('aria-label'))
      || el.getAttribute('name') || el.id || el.tagName.toLowerCase();
  };
  const invalid = [];
  for (const el of document.querySelectorAll('input, select, textarea')) {
    if (el.willValidate && !el.disabled && !el.validity.valid) { invalid.push(labelOf(el)); }
  }
  for (const el of document.querySelectorAll('[' + attribute + ']')) {
    el.removeAttribute(attribute);
  }
  const controls = Array.from(document.querySelectorAll('form button, form input[type=submit]'))
    .filter((el) => {
      const type = (el.getAttribute('type') || (el.tagName === 'BUTTON' ? 'submit' : ''))
        .toLowerCase();
      return type === 'submit' && !el.disabled && visible(el);
    });
  if (controls.length === 1) { controls[0].setAttribute(attribute, '1'); }
  return {invalid, controls: controls.length};
}
"""
