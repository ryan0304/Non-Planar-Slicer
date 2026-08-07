(function(){
  'use strict';

  // ---- How-to-use guide -----------------------------------------------------
  // Six-slide onboarding modal (#guide-modal in index.html). Reached via the
  // "?" trigger button beside #toggle-hints, the "?" key, or auto-opened on a
  // true first visit (see the IIFE at the bottom of this file for the exact
  // conditions). Plain vanilla JS, no framework, matches the rest of viewer/.

  var SEEN_KEY = 'trident-guide-seen';
  var DESIGN_KEY = 'design-state';

  var modal = document.getElementById('guide-modal');
  if(!modal) return;

  var card = modal.querySelector('.guide-modal-card');
  var backdrop = modal.querySelector('.guide-modal-backdrop');
  var closeBtn = document.getElementById('guide-modal-close');
  var backBtn = document.getElementById('guide-back');
  var nextBtn = document.getElementById('guide-next');
  var dotsWrap = document.getElementById('guide-dots');
  var triggerBtn = document.getElementById('guide-btn');
  var slides = Array.prototype.slice.call(modal.querySelectorAll('.guide-slide'));

  var current = 0;
  var opener = null;

  // Build the dot indicator once, one dot per slide.
  slides.forEach(function(){
    var d = document.createElement('span');
    d.className = 'guide-dot';
    dotsWrap.appendChild(d);
  });
  var dots = Array.prototype.slice.call(dotsWrap.querySelectorAll('.guide-dot'));

  function markSeen(){
    try { localStorage.setItem(SEEN_KEY, '1'); } catch(e){}
  }

  function renderSlide(){
    slides.forEach(function(s, i){ s.style.display = (i === current) ? '' : 'none'; });
    dots.forEach(function(d, i){ d.classList.toggle('active', i === current); });
    backBtn.textContent = (current === 0) ? 'Skip' : 'Back';
    nextBtn.textContent = (current === slides.length - 1) ? 'Done' : 'Next';
  }

  function isOpen(){ return modal.style.display !== 'none'; }

  function openGuide(fromEl){
    opener = fromEl || document.activeElement;
    current = 0;
    renderSlide();
    modal.style.display = 'flex';
    if(closeBtn) closeBtn.focus();
  }

  // Closing always marks the guide seen, whichever way it happens (X, Esc,
  // backdrop click, Skip on slide 1, Done on the last slide) -- there is no
  // "closed but still counts as unseen" path.
  function closeGuide(){
    if(!isOpen()) return;
    modal.style.display = 'none';
    markSeen();
    var target = (opener && typeof opener.focus === 'function') ? opener : triggerBtn;
    if(target) target.focus();
    opener = null;
  }

  function trapFocus(e){
    var all = card.querySelectorAll('button, input, select, textarea, [tabindex]');
    var focusables = Array.prototype.filter.call(all, function(el){
      return !el.disabled && el.tabIndex !== -1 && el.offsetParent !== null;
    });
    if(!focusables.length) return;
    var first = focusables[0], last = focusables[focusables.length - 1];
    if(e.shiftKey && document.activeElement === first){ e.preventDefault(); last.focus(); }
    else if(!e.shiftKey && document.activeElement === last){ e.preventDefault(); first.focus(); }
  }

  if(backBtn) backBtn.addEventListener('click', function(){
    if(current === 0){ closeGuide(); return; }   // slide 1: this button reads "Skip"
    current -= 1;
    renderSlide();
  });
  if(nextBtn) nextBtn.addEventListener('click', function(){
    if(current === slides.length - 1){ closeGuide(); return; }   // last slide: reads "Done"
    current += 1;
    renderSlide();
  });
  if(closeBtn) closeBtn.addEventListener('click', closeGuide);
  // This is orientation content, not a decision that needs guarding against a
  // stray click (contrast the session-restore modal, which deliberately has
  // NO backdrop dismissal because "continue vs. start new" is a real choice).
  if(backdrop) backdrop.addEventListener('click', closeGuide);
  if(triggerBtn) triggerBtn.addEventListener('click', function(){ openGuide(triggerBtn); });

  document.addEventListener('keydown', function(e){
    if(isOpen()){
      if(e.key === 'Escape'){ e.preventDefault(); closeGuide(); return; }
      if(e.key === 'Tab'){ trapFocus(e); }
      return;
    }
    if(e.key !== '?') return;
    // Leave native "?" behavior (e.g. typed into a search box) alone.
    var tag = e.target && e.target.tagName;
    if(tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
    e.preventDefault();
    openGuide(triggerBtn);
  }, true);

  // ---- auto-open on a true first visit --------------------------------------
  // ALL three must hold:
  //   1. trident-guide-seen has never been set,
  //   2. no design is stored under design-state -- otherwise this would race
  //      designer.js's own session-restore modal, which only shows when a
  //      design IS already stored (see the IIFE near the end of designer.js).
  //      The two conditions are mutually exclusive by construction, so only
  //      one of the two modals can ever want to auto-open on a given load.
  //   3. window.top === window.self.
  //
  // (3) is NOT optional. viewer/dev_smoke.html loads this page inside an
  // IFRAME and drives it by clicking real controls to run its assertions; a
  // modal overlay auto-opening on top of the whole page would silently
  // swallow every one of those clicks, and the failures would look like
  // unrelated regressions rather than "the guide popped up". Do not remove
  // this guard, and do not "fix" it by special-casing dev_smoke.html's URL --
  // the general rule (never auto-open while framed) is what has to hold.
  var seen = true, hasDesign = true;
  try { seen = !!localStorage.getItem(SEEN_KEY); } catch(e){ seen = true; }       // fail closed
  try { hasDesign = !!localStorage.getItem(DESIGN_KEY); } catch(e){ hasDesign = true; } // fail closed
  var isTopLevel = (window.top === window.self);
  if(!seen && !hasDesign && isTopLevel){
    openGuide(triggerBtn);
  }
})();
