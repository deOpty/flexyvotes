/**
 * FlexyVotes site-wide interaction layer.
 * Purely presentational: scroll reveals, navbar elevation, button ripple,
 * button loading feedback, and alert auto-dismiss. Never intercepts or
 * blocks existing form/page logic - only observes and adds visual state.
 */
(function () {
  'use strict';

  var prefersReducedMotion = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  document.addEventListener('DOMContentLoaded', function () {
    initNavbarElevation();
    initScrollReveal();
    initButtonRipple();
    initButtonLoadingState();
    initAlertAutoDismiss();
    initPageFadeIn();
    initImageFade();
    initCardLinks();
  });

  /* --- Navbar gains elevation/blur once the page is scrolled --- */
  function initNavbarElevation() {
    var nav = document.querySelector('.navbar');
    if (!nav) return;
    var toggle = function () {
      nav.classList.toggle('is-scrolled', window.scrollY > 8);
    };
    toggle();
    window.addEventListener('scroll', toggle, { passive: true });
  }

  /* --- Fade + rise elements marked with .reveal as they enter viewport ---
     Content must never stay hidden. A hidden .reveal element sits 24px lower
     than its layout position, so the last element on a page (often a form's
     submit row) can only ever be partly on screen, even at maximum scroll.
     An earlier threshold of 12% with a -40px bottom margin left such elements
     at opacity 0 for good (e.g. the "Create Event" button). So: reveal on
     the first visible pixel, and reveal whatever is left once the page is
     scrolled to the bottom or a hidden element receives focus. */
  function initScrollReveal() {
    var targets = document.querySelectorAll('.reveal');
    if (!targets.length) return;

    var show = function (el) { el.classList.add('is-visible'); };

    if (prefersReducedMotion || !('IntersectionObserver' in window)) {
      targets.forEach(show);
      return;
    }

    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          show(entry.target);
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0, rootMargin: '0px' });

    targets.forEach(function (el) { observer.observe(el); });

    var revealAll = function () {
      document.querySelectorAll('.reveal:not(.is-visible)').forEach(function (el) {
        show(el);
        observer.unobserve(el);
      });
    };
    var atBottom = function () {
      var doc = document.documentElement;
      return window.innerHeight + window.scrollY >= doc.scrollHeight - 2;
    };
    var onScroll = function () {
      if (!atBottom()) return;
      revealAll();
      window.removeEventListener('scroll', onScroll);
    };
    window.addEventListener('scroll', onScroll, { passive: true });
    onScroll(); // short pages: everything already fits on screen

    // Keyboard users tabbing into a not-yet-revealed block must see it.
    document.addEventListener('focusin', function (e) {
      var el = e.target.closest && e.target.closest('.reveal:not(.is-visible)');
      if (el) { show(el); observer.unobserve(el); }
    });
  }

  /* --- Subtle ripple feedback on buttons (visual only, never blocks clicks) --- */
  function initButtonRipple() {
    if (prefersReducedMotion) return;
    document.addEventListener('click', function (e) {
      var btn = e.target.closest('.btn');
      if (!btn || btn.disabled || btn.classList.contains('disabled') || btn.classList.contains('is-loading')) return;

      var rect = btn.getBoundingClientRect();
      var size = Math.max(rect.width, rect.height);
      var ripple = document.createElement('span');
      ripple.className = 'btn-ripple';
      ripple.style.width = ripple.style.height = size + 'px';
      ripple.style.left = (e.clientX - rect.left - size / 2) + 'px';
      ripple.style.top = (e.clientY - rect.top - size / 2) + 'px';

      var computedPosition = window.getComputedStyle(btn).position;
      if (computedPosition === 'static') {
        btn.style.position = 'relative';
      }

      btn.appendChild(ripple);
      window.setTimeout(function () {
        if (ripple.parentNode) ripple.parentNode.removeChild(ripple);
      }, 650);
    });
  }

  /* --- Show a loading spinner on the submit button when a form is submitted ---
     Applies to every form that really submits (forms whose handlers cancel
     the submission, such as AJAX forms or a declined confirmation, are
     skipped). It never calls preventDefault, so it cannot interfere with
     existing submit logic. A safety timeout clears the state in case the
     page never navigates away. */
  function initButtonLoadingState() {
    document.addEventListener('submit', function (e) {
      var form = e.target;
      if (!(form instanceof HTMLFormElement)) return;

      // The button that was pressed; a <button> without a type is a submit
      // button too (e.g. "Continue to secure payment").
      var submitBtn = (e.submitter && e.submitter.matches('button, input[type="submit"]')) ? e.submitter :
        form.querySelector('button[type="submit"], button:not([type]), input[type="submit"]');
      if (!submitBtn || submitBtn.classList.contains('is-loading')) return;

      // Runs after every other submit handler (e.g. a cancelled data-confirm
      // dialog), so a submission that did not happen leaves no spinner behind.
      window.setTimeout(function () {
        if (e.defaultPrevented) return;
        submitBtn.classList.add('is-loading');
        submitBtn.setAttribute('aria-busy', 'true');

        window.setTimeout(function () {
          submitBtn.classList.remove('is-loading');
          submitBtn.removeAttribute('aria-busy');
        }, 8000);
      }, 0);
    }, true);
  }

  /* --- Auto-dismiss success/info flash messages after a few seconds ---
     Only alerts marked [data-autodismiss] (success/info flash messages in
     voting/base.html). Errors and warnings stay until closed so the reader
     learns why an action failed, and .alert boxes that are page content
     (the live price on the pay page, "voting is closed" notices, ...) are
     never removed: closing those made the payment page stop updating. */
  function initAlertAutoDismiss() {
    var alerts = document.querySelectorAll('.alert[data-autodismiss]');
    alerts.forEach(function (alertEl) {
      window.setTimeout(function () {
        if (window.bootstrap && window.bootstrap.Alert) {
          var instance = window.bootstrap.Alert.getOrCreateInstance(alertEl);
          try { instance.close(); } catch (err) { /* already closed */ }
        }
      }, 6000);
    });
  }

  /* --- Gentle whole-page fade-in on load ---
     IMPORTANT: the class is removed again once the animation finishes.
     A CSS animation targeting opacity/transform makes its element establish
     its own stacking context for as long as the animation/class is applied;
     since every modal on the site lives inside this same element, leaving
     the class on permanently traps modals (z-index: 1055) inside that local
     context while Bootstrap appends `.modal-backdrop` straight to <body>,
     so the backdrop would render above the modal and block all clicks on it.
     Removing the class post-animation restores normal stacking with no
     visible difference (the animation already finished holding opacity:1 /
     transform:none, which is also the element's default appearance). */
  function initPageFadeIn() {
    var content = document.querySelector('.content');
    if (!content || prefersReducedMotion) return;

    content.classList.add('page-fade-in');
    var clear = function () { content.classList.remove('page-fade-in'); };
    content.addEventListener('animationend', clear, { once: true });
    window.setTimeout(clear, 700); // safety net if animationend doesn't fire
  }

  /* --- Whole-card click-through: any element with [data-card-link] navigates
     to that URL when clicked, unless the actual click landed on a real link/
     button/form control inside it (so nested interactive elements keep their
     own behavior and never get double-handled). Selecting text by dragging
     is also respected - only fires on a genuine, non-text-selecting click. --- */
  function initCardLinks() {
    document.addEventListener('click', function (e) {
      const card = e.target.closest('[data-card-link]');
      if (!card) return;
      if (e.target.closest('a, button, input, textarea, select, label')) return;
      if (window.getSelection && String(window.getSelection())) return; // user was selecting text

      const href = card.getAttribute('data-card-link');
      if (href) window.location.href = href;
    });
  }

  /* --- Progressively fade in images as they finish loading. Applied
     site-wide with no template changes and no layout shift, since every
     image container on the site already reserves its own height (inline
     style or aspect box) before the image itself loads. --- */
  function initImageFade() {
    if (prefersReducedMotion) return;
    document.querySelectorAll('img').forEach(function (img) {
      if (img.complete && img.naturalWidth > 0) return; // already cached/loaded
      img.classList.add('img-fade-init');
      var reveal = function () { img.classList.add('img-fade-in'); };
      img.addEventListener('load', reveal, { once: true });
      img.addEventListener('error', reveal, { once: true });
    });
  }
})();
