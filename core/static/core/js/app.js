/**
 * FlexyVotes application behaviours. Everything is attached with
 * addEventListener / data-* attributes so the strict Content-Security-Policy
 * (no inline handlers) holds. Pages work without this script; it only adds
 * convenience (live updates, confirmations, client-side ballot checks).
 */
(function () {
  'use strict';

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute('content') : '';
  }
  window.fvCsrfToken = csrfToken;

  function post(url, body, json) {
    var headers = { 'X-CSRFToken': csrfToken(), 'X-Requested-With': 'XMLHttpRequest' };
    if (json) headers['Content-Type'] = 'application/json';
    return fetch(url, { method: 'POST', credentials: 'same-origin', headers: headers, body: body });
  }
  window.fvPost = post;

  document.addEventListener('DOMContentLoaded', function () {
    initConfirm();
    initAutoSubmit();
    initTimezone();
    initFingerprint();
    initLiveCounts();
    initBallot();
    initPayQuote();
    initMonitor();
    initPasskeys();
    initAjaxForms();
    initCountdown();
    initCopy();
  });

  /* data-confirm="Are you sure?" on forms or buttons */
  function initConfirm() {
    document.addEventListener('submit', function (e) {
      var form = e.target;
      var message = form.getAttribute('data-confirm') ||
        (e.submitter && e.submitter.getAttribute('data-confirm'));
      if (message && !window.confirm(message)) e.preventDefault();
    }, true);
  }

  function initAutoSubmit() {
    document.querySelectorAll('form[data-autosubmit] select').forEach(function (select) {
      select.addEventListener('change', function () { select.form.submit(); });
    });
  }

  /* Remember the browser's time zone so dates render locally. */
  function initTimezone() {
    try {
      var tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
      if (tz && document.cookie.indexOf('fv_tz=') === -1) {
        var data = new FormData();
        data.append('timezone', tz);
        post('/prefs/timezone/', data);
      }
    } catch (err) { /* ignore */ }
  }

  /* Coarse, privacy-preserving device signal for fraud scoring. */
  function initFingerprint() {
    var fields = document.querySelectorAll('input[name="fv_fp"]');
    if (!fields.length || !window.crypto || !window.crypto.subtle) return;
    var raw = [navigator.userAgent, navigator.language, screen.width + 'x' + screen.height,
      screen.colorDepth, new Date().getTimezoneOffset(), navigator.hardwareConcurrency || ''].join('|');
    window.crypto.subtle.digest('SHA-256', new TextEncoder().encode(raw)).then(function (buf) {
      var hex = Array.prototype.map.call(new Uint8Array(buf), function (b) { return ('0' + b.toString(16)).slice(-2); }).join('');
      fields.forEach(function (f) { f.value = hex; });
    });
  }

  /* Live counts for paid elections: [data-live-url] container. */
  function initLiveCounts() {
    var box = document.querySelector('[data-live-url]');
    if (!box) return;
    var url = box.getAttribute('data-live-url');
    function refresh() {
      fetch(url, { credentials: 'same-origin' }).then(function (r) { return r.ok ? r.json() : null; }).then(function (data) {
        if (!data) return;
        data.candidates.forEach(function (c) {
          var count = document.getElementById('vote-count-' + c.id);
          if (count) count.textContent = c.votes.toLocaleString();
          var pct = document.getElementById('percentage-' + c.id);
          if (pct) pct.textContent = c.percentage + '%';
          var bar = document.getElementById('progress-' + c.id);
          if (bar) { bar.style.width = c.percentage + '%'; bar.setAttribute('aria-valuenow', c.percentage); }
        });
        var total = document.getElementById('total-votes');
        if (total) total.textContent = data.total_votes.toLocaleString();
      }).catch(function () {});
    }
    setInterval(refresh, parseInt(box.getAttribute('data-live-interval') || '10000', 10));
  }

  /* Ballot helpers: enforce max selections, abstain toggles, rank uniqueness. */
  function initBallot() {
    document.querySelectorAll('[data-position]').forEach(function (fieldset) {
      var max = parseInt(fieldset.getAttribute('data-max') || '0', 10);
      var type = fieldset.getAttribute('data-type');
      var abstain = fieldset.querySelector('[data-abstain]');
      var inputs = fieldset.querySelectorAll('input[data-choice]');
      var counter = fieldset.querySelector('[data-counter]');
      function update() {
        var checked = fieldset.querySelectorAll('input[data-choice]:checked').length;
        if (counter) counter.textContent = checked + ' / ' + max;
        if (type === 'MULTIPLE' || type === 'FPTP' || type === 'APPROVAL') {
          inputs.forEach(function (i) { if (!i.checked) i.disabled = max > 0 && checked >= max; });
        }
        if (abstain && checked > 0) abstain.checked = false;
      }
      inputs.forEach(function (i) { i.addEventListener('change', update); });
      if (abstain) {
        abstain.addEventListener('change', function () {
          if (abstain.checked) {
            inputs.forEach(function (i) { if (i.type === 'radio' || i.type === 'checkbox') { i.checked = false; i.disabled = false; } else { i.value = ''; } });
            update();
          }
        });
      }
      fieldset.querySelectorAll('[data-rank],[data-score]').forEach(function (input) {
        input.addEventListener('input', function () { if (abstain && input.value) abstain.checked = false; });
      });
      update();
    });
  }

  /* Pay page: live quote. */
  function initPayQuote() {
    var form = document.querySelector('form[data-quote-url]');
    if (!form) return;
    var out = document.getElementById('quote-output');
    var timer;
    function refresh() {
      var params = new URLSearchParams();
      var pkg = form.querySelector('input[name="package"]:checked');
      if (pkg && pkg.value) params.set('package', pkg.value);
      var votes = form.querySelector('input[name="votes"]');
      if (votes && votes.value && !(pkg && pkg.value)) params.set('votes', votes.value);
      var code = form.querySelector('input[name="discount_code"]');
      if (code && code.value) params.set('discount_code', code.value);
      fetch(form.getAttribute('data-quote-url') + '?' + params.toString(), { credentials: 'same-origin' })
        .then(function (r) { return r.json(); }).then(function (q) {
          if (!out) return;
          if (q.errors && q.errors.length) { out.textContent = q.errors[0]; out.className = 'text-danger small'; return; }
          out.className = 'fw-semibold';
          out.textContent = q.total_votes + ' vote(s) - ' + q.currency + ' ' + q.amount +
            (parseFloat(q.discount) > 0 ? ' (saved ' + q.currency + ' ' + q.discount + ')' : '');
        }).catch(function () {});
    }
    form.addEventListener('input', function () { clearTimeout(timer); timer = setTimeout(refresh, 300); });
    form.addEventListener('change', refresh);
    refresh();
  }

  /* Live election monitoring dashboard. */
  function initMonitor() {
    var box = document.querySelector('[data-monitor-url]');
    if (!box) return;
    function render(data) {
      var set = function (id, value) { var el = document.getElementById(id); if (el) el.textContent = value; };
      set('mon-last5', data.last_5_min);
      set('mon-alerts', data.open_alerts);
      set('mon-status', data.status);
      set('mon-updated', new Date(data.generated_at).toLocaleTimeString());
      if (data.payment_success_rate !== undefined) set('mon-psr', data.payment_success_rate === null ? '-' : data.payment_success_rate + '%');
      if (data.turnout) set('mon-turnout', data.turnout.turnout_percentage + '% (' + data.turnout.voted + ' / ' + data.turnout.eligible + ')');
      var chart = document.getElementById('mon-chart');
      if (chart) {
        chart.innerHTML = '';
        var max = Math.max.apply(null, data.series.map(function (p) { return p.n; }).concat([1]));
        data.series.forEach(function (p) {
          var bar = document.createElement('span');
          bar.style.height = Math.max(2, Math.round(100 * p.n / max)) + '%';
          bar.title = new Date(p.t).toLocaleTimeString() + ': ' + p.n;
          chart.appendChild(bar);
        });
      }
      var list = document.getElementById('mon-alert-list');
      if (list) {
        list.innerHTML = '';
        data.latest_alerts.forEach(function (a) {
          var li = document.createElement('li');
          li.className = 'list-group-item d-flex justify-content-between';
          li.textContent = a.kind + ' - ' + a.decision + ' (' + a.score + ')';
          list.appendChild(li);
        });
      }
    }
    function refresh() {
      fetch(box.getAttribute('data-monitor-url'), { credentials: 'same-origin' })
        .then(function (r) { return r.json(); }).then(render).catch(function () {});
    }
    refresh();
    setInterval(refresh, 10000);
  }

  /* WebAuthn helpers */
  function b64urlToBuf(value) {
    var pad = '='.repeat((4 - value.length % 4) % 4);
    var raw = atob((value + pad).replace(/-/g, '+').replace(/_/g, '/'));
    var buf = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) buf[i] = raw.charCodeAt(i);
    return buf.buffer;
  }
  function bufToB64url(buf) {
    var bytes = new Uint8Array(buf), s = '';
    for (var i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }
  function credentialToJSON(cred) {
    var out = { id: cred.id, rawId: bufToB64url(cred.rawId), type: cred.type, response: {}, clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {} };
    ['clientDataJSON', 'attestationObject', 'authenticatorData', 'signature', 'userHandle'].forEach(function (k) {
      if (cred.response[k]) out.response[k] = bufToB64url(cred.response[k]);
    });
    if (cred.response.getTransports) out.response.transports = cred.response.getTransports();
    if (cred.authenticatorAttachment) out.authenticatorAttachment = cred.authenticatorAttachment;
    return out;
  }

  function initPasskeys() {
    var register = document.getElementById('passkey-register');
    if (register) {
      register.addEventListener('click', function () {
        if (!window.PublicKeyCredential) { alert('Passkeys are not supported in this browser.'); return; }
        post(register.getAttribute('data-options-url'), '', false).then(function (r) { return r.json(); }).then(function (opts) {
          opts.challenge = b64urlToBuf(opts.challenge);
          opts.user.id = b64urlToBuf(opts.user.id);
          (opts.excludeCredentials || []).forEach(function (c) { c.id = b64urlToBuf(c.id); });
          return navigator.credentials.create({ publicKey: opts });
        }).then(function (cred) {
          var name = document.getElementById('passkey-name');
          return post(register.getAttribute('data-register-url'),
            JSON.stringify({ credential: credentialToJSON(cred), name: name ? name.value : 'Passkey' }), true);
        }).then(function (r) { if (r.ok) window.location.reload(); else alert('Passkey registration failed.'); })
          .catch(function (err) { alert('Passkey registration cancelled or failed.'); });
      });
    }
    var login = document.getElementById('passkey-login');
    if (login) {
      login.addEventListener('click', function () {
        post(login.getAttribute('data-options-url'), '', false).then(function (r) { return r.json(); }).then(function (opts) {
          opts.challenge = b64urlToBuf(opts.challenge);
          (opts.allowCredentials || []).forEach(function (c) { c.id = b64urlToBuf(c.id); });
          return navigator.credentials.get({ publicKey: opts });
        }).then(function (cred) {
          return post(login.getAttribute('data-verify-url'), JSON.stringify(credentialToJSON(cred)), true);
        }).then(function (r) { return r.json(); }).then(function (data) {
          if (data.redirect) window.location.href = data.redirect; else alert(data.error || 'Passkey sign-in failed.');
        }).catch(function () { alert('Passkey sign-in cancelled or failed.'); });
      });
    }
  }

  /* Forms with data-ajax reload the page after a successful JSON response. */
  function initAjaxForms() {
    document.querySelectorAll('form[data-ajax]').forEach(function (form) {
      form.addEventListener('submit', function (e) {
        e.preventDefault();
        var button = form.querySelector('button[type="submit"]');
        if (button) button.disabled = true;
        post(form.action, new FormData(form), false).then(function (r) { return r.json(); }).then(function (data) {
          if (data.status === 'success') window.location.reload();
          else { alert(data.message || 'Something went wrong.'); if (button) button.disabled = false; }
        }).catch(function () { alert('Network error.'); if (button) button.disabled = false; });
      });
    });
  }

  function initCountdown() {
    var box = document.querySelector('[data-countdown]');
    if (!box) return;
    var end = new Date(box.getAttribute('data-countdown')).getTime();
    function tick() {
      var d = end - Date.now();
      if (d <= 0) { box.textContent = box.getAttribute('data-ended') || 'Voting has ended'; return; }
      var days = Math.floor(d / 86400000), hours = Math.floor(d % 86400000 / 3600000),
        mins = Math.floor(d % 3600000 / 60000), secs = Math.floor(d % 60000 / 1000);
      box.textContent = days + 'd ' + hours + 'h ' + mins + 'm ' + secs + 's';
      setTimeout(tick, 1000);
    }
    tick();
  }

  function initCopy() {
    document.querySelectorAll('[data-copy]').forEach(function (button) {
      button.addEventListener('click', function () {
        var target = document.getElementById(button.getAttribute('data-copy'));
        if (target && navigator.clipboard) {
          navigator.clipboard.writeText(target.textContent.trim()).then(function () {
            var label = button.textContent; button.textContent = 'Copied'; setTimeout(function () { button.textContent = label; }, 1500);
          });
        }
      });
    });
    document.querySelectorAll('[data-print]').forEach(function (button) {
      button.addEventListener('click', function () { window.print(); });
    });
  }
})();
