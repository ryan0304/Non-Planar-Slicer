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
    figures.forEach(stopFigure);   // never leave an off-screen slide ticking
    var rec = currentFigure();
    if(rec) playFigure(rec, false);
  }

  // ---- recorded-interaction figures -----------------------------------------
  // Every slide after the first shows a short sequence of PNG frames captured
  // from THIS app while a real click was driven through it -- see
  // viewer/guide/README.md. Nothing in a frame is drawn on afterwards: the
  // cursor, the click ring, the hover states and the numbers that change were
  // all rendered by the page at capture time.
  //
  // The <img> stack is built on FIRST OPEN rather than at page load, so a user
  // who never opens the guide never fetches ~640 kB of screenshots. That is
  // also why the frames are not inlined into index.html.
  var FRAME_MS = 850;
  var figures = [];
  var figuresBuilt = false;

  function buildFigures(){
    if(figuresBuilt) return;
    figuresBuilt = true;
    slides.forEach(function(slide){
      var fig = slide.querySelector('.guide-fig[data-frames]');
      if(!fig) return;
      var base = fig.getAttribute('data-frames');
      var count = parseInt(fig.getAttribute('data-count'), 10);
      if(!(count > 0)) return;

      var frames = [];
      for(var i = 1; i <= count; i++){
        var img = document.createElement('img');
        img.className = 'guide-shot';
        img.alt = '';            // decorative: the bullets carry the meaning
        img.src = base + '/' + (i < 10 ? '0' : '') + i + '.png';
        fig.appendChild(img);
        frames.push(img);
      }

      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'gf-replay';
      btn.textContent = 'Replay';
      btn.setAttribute('aria-label', 'Replay this recording');
      fig.appendChild(btn);

      var rec = { frames: frames, timer: 0 };
      // force:true -- pressing Replay is an explicit ask for motion, so it
      // steps the sequence even under prefers-reduced-motion.
      btn.addEventListener('click', function(){ playFigure(rec, true); });
      fig.recording = rec;
      figures.push(rec);
    });
  }

  function currentFigure(){
    var fig = slides[current] && slides[current].querySelector('.guide-fig[data-frames]');
    return (fig && fig.recording) || null;
  }

  function showFrame(rec, i){
    rec.frames.forEach(function(f, k){ f.classList.toggle('gf-on', k === i); });
  }

  function stopFigure(rec){
    if(rec.timer){ clearInterval(rec.timer); rec.timer = 0; }
  }

  // Runs once and HOLDS on the last frame rather than looping: the last frame
  // is the outcome of the click, so the figure reads correctly frozen -- the
  // same rule the rest of this UI's motion follows. Replay is the way back.
  function playFigure(rec, force){
    stopFigure(rec);
    if(!rec.frames.length) return;
    var mq = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)');
    if(mq && mq.matches && !force){ showFrame(rec, rec.frames.length - 1); return; }
    var i = 0;
    showFrame(rec, 0);
    rec.timer = setInterval(function(){
      i += 1;
      if(i >= rec.frames.length){ stopFigure(rec); return; }
      showFrame(rec, i);
    }, FRAME_MS);
  }

  function isOpen(){ return modal.style.display !== 'none'; }

  function openGuide(fromEl){
    opener = fromEl || document.activeElement;
    current = 0;
    buildFigures();   // first open is when the frames start downloading
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
    figures.forEach(stopFigure);   // a closed guide must not keep a timer alive
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
