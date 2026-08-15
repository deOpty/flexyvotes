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

  /* --- Fade + rise elements marked with .reveal as they enter viewport --- */
  function initScrollReveal() {
    var targets = document.querySelectorAll('.reveal');
    if (!targets.length) return;

    if (prefersReducedMotion || !('IntersectionObserver' in window)) {
      targets.forEach(function (el) { el.classList.add('is-visible'); });
      return;
    }

    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-visible');
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0.12, rootMargin: '0px 0px -40px 0px' });

    targets.forEach(function (el) { observer.observe(el); });
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
     Applies to every form on the page, including ones with their own
     AJAX handlers - this never calls preventDefault, so it cannot
     interfere with existing submit logic. A safety timeout clears the
     state in case an AJAX call never navigates away or resets it. */
  function initButtonLoadingState() {
    document.addEventListener('submit', function (e) {
      var form = e.target;
      if (!(form instanceof HTMLFormElement)) return;

      var submitBtn = form.querySelector('button[type="submit"], input[type="submit"]');
      if (!submitBtn || submitBtn.classList.contains('is-loading')) return;

      submitBtn.classList.add('is-loading');
      submitBtn.setAttribute('aria-busy', 'true');

      window.setTimeout(function () {
        submitBtn.classList.remove('is-loading');
        submitBtn.removeAttribute('aria-busy');
      }, 8000);
    }, true);
  }

  /* --- Auto-dismiss success/info alerts after a few seconds --- */
  function initAlertAutoDismiss() {
    var alerts = document.querySelectorAll('.alert.show, .alert:not(.alert-permanent)');
    alerts.forEach(function (alertEl) {
      if (alertEl.classList.contains('alert-permanent')) return;
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
