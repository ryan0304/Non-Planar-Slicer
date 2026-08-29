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
    if(rec) playFigure(rec);
    warmAhead();
  }

  // Fetch the NEXT slide's frames while this one plays, so paging forward
  // finds them decoded. Deliberately behind a delay and never at open time:
  // requesting all 24 frames at once puts the sequence the reader is actually
  // watching behind eighteen images it does not need yet, and the dev server
  // only serves a handful of connections at a time. Whatever is on screen
  // gets the pipe first.
  var warmTimer = 0;
  function warmAhead(){
    if(warmTimer) clearTimeout(warmTimer);
    warmTimer = setTimeout(function(){
      warmTimer = 0;
      var rec = figureIn(current + 1);
      if(rec) preload(rec);
    }, 700);
  }

  // ---- recorded-interaction figures -----------------------------------------
  // Every slide after the first shows a short sequence of PNG frames captured
  // from THIS app while a real click was driven through it -- see
  // viewer/guide/README.md. Nothing in a frame is drawn on afterwards: the
  // cursor, the click ring, the hover states and the numbers that change were
  // all rendered by the page at capture time.
  //
  // The <img> stack is built on FIRST OPEN rather than at page load, so a user
  // who never opens the guide never fetches ~950 kB of screenshots. That is
  // also why the frames are not inlined into index.html.
  //
  // Timing: the step has to be long enough to read a frame and short enough to
  // feel like one gesture. 620 ms with a 140 ms dissolve inside it leaves the
  // image fully settled for ~480 ms before the next one starts. Several of
  // these sequences swap the WHOLE panel between frames (the step tabs, the
  // texture pattern), and a big change needs time to land -- stepped faster it
  // reads as flicker rather than as one thing becoming another. FRAME_MS must
  // stay well above the dissolve in style.css or the next fade starts before
  // the last one finished and the box smears through three frames at once.
  //
  // The sequence LOOPS: it holds on the last frame -- the outcome of the click,
  // the state the bullets describe -- for LOOP_PAUSE_MS, dissolves back to the
  // start and runs again. The hold carries most of the calm: a short one turns
  // the figure into something that restarts in the corner of the reader's eye
  // while they are still reading the bullets beside it.
  var FRAME_MS = 620;
  var LOOP_PAUSE_MS = 2000;
  var REWIND_MS = 200;          // must cover the dissolve in style.css
  var DECODE_WAIT_MS = 600;
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

      // token: bumped by every stopFigure(), so an async decode that finishes
      // after the user paged away cannot start a stale sequence.
      var rec = { fig: fig, frames: frames, raf: 0, deadline: 0,
                  ready: false, pending: false, waiting: null, token: 0 };
      fig.recording = rec;
      figures.push(rec);
    });
  }

  function figureIn(index){
    var slide = slides[index];
    var fig = slide && slide.querySelector('.guide-fig[data-frames]');
    return (fig && fig.recording) || null;
  }

  function currentFigure(){ return figureIn(current); }

  // Reveal, not crossfade: frames 0..i are all lit, so the incoming frame
  // fades in OVER an opaque stack instead of trading places with a frame that
  // is simultaneously fading out. A symmetric crossfade passes through about
  // half brightness at its midpoint, which on this dark UI reads as a flicker
  // between every pair of frames. See the note in style.css.
  function showFrame(rec, i){
    rec.frames.forEach(function(f, k){ f.classList.toggle('gf-on', k <= i); });
  }

  // Hard cut to frame 0, used when a figure STARTS (nothing is on screen yet,
  // so there is nothing to dissolve from). Un-lighting the stack with
  // transitions live would fade every opaque layer out together, and the blend
  // of all of them on the way down is a smear. Suppress, mutate, flush, restore.
  function resetToStart(rec){
    rec.fig.classList.add('gf-rewind');
    rec.frames.forEach(function(f, k){ f.classList.toggle('gf-on', k === 0); });
    void rec.fig.offsetWidth;   // force style flush while transitions are off
    rec.fig.classList.remove('gf-rewind');
  }

  // Loop restart, and the one moment the figure would otherwise jump. Cutting
  // straight back to frame 0 is a hard jolt every few seconds beside prose
  // somebody is still reading, so dissolve instead -- but dissolve exactly TWO
  // layers, not the whole stack.
  //
  // Every frame between the first and the last is hidden BEHIND the top frame
  // while the sequence is fully revealed, so un-lighting them costs nothing
  // visually and has to be done with transitions off. What is left is the top
  // frame over frame 0: fading the top one out is a plain one-to-one dissolve
  // back to the beginning, with no intermediate blend of six images.
  function dissolveToStart(rec){
    var frames = rec.frames, top = frames.length - 1;
    rec.fig.classList.add('gf-rewind');
    frames.forEach(function(f, k){ f.classList.toggle('gf-on', k === 0 || k === top); });
    void rec.fig.offsetWidth;
    rec.fig.classList.remove('gf-rewind');
    if(top > 0) frames[top].classList.remove('gf-on');   // now transitions
  }

  function stopFigure(rec){
    rec.token += 1;        // invalidates any decode still in flight
    rec.waiting = null;
    if(rec.raf){ cancelAnimationFrame(rec.raf); rec.raf = 0; }
    if(rec.deadline){ clearTimeout(rec.deadline); rec.deadline = 0; }
    if(rec.fig) rec.fig.classList.remove('gf-running');
  }

  function settle(rec){
    rec.ready = true;
    rec.pending = false;
    var go = rec.waiting;
    rec.waiting = null;
    if(go) go();
  }

  // Decode every frame before the first step. An <img> that is merely fetched
  // still costs a decode the first time the compositor has to paint it, and
  // that lands exactly on a frame flip -- which is what makes the first play
  // of a sequence feel like it is stuttering. decode() moves that work off the
  // animation. Rejections resolve the same as successes: a missing PNG must
  // not hang playback, it just stays blank.
  //
  // The complete/naturalWidth short-circuit is not just an optimisation. In a
  // BACKGROUND tab Chrome may never settle decode(), which would otherwise
  // wedge rec.pending forever and make every later play sit out the full
  // DECODE_WAIT_MS deadline -- reopening a guide whose frames were already on
  // screen once would feel slower than the first time. Bytes in hand and a
  // known intrinsic size is readiness enough.
  function allComplete(rec){
    return rec.frames.every(function(img){ return img.complete && img.naturalWidth > 0; });
  }

  function preload(rec){
    if(rec.ready) return;
    if(allComplete(rec)){ settle(rec); return; }
    if(rec.pending) return;
    rec.pending = true;
    var left = rec.frames.length;
    function done(){
      left -= 1;
      if(left <= 0) settle(rec);
    }
    rec.frames.forEach(function(img){
      if(typeof img.decode === 'function'){ img.decode().then(done, done); return; }
      if(img.complete){ done(); return; }
      img.addEventListener('load', done);
      img.addEventListener('error', done);
    });
  }

  // Stepped on requestAnimationFrame rather than setInterval. Two reasons, and
  // both are about how this reads rather than about correctness:
  //   - a flip scheduled by a timer can land just after the frame it wanted,
  //     so the dissolve starts one vsync late and the cadence wobbles; rAF
  //     hands the class change to the same frame the compositor is building.
  //   - a hidden tab clamps timers to one second, so a guide left open in a
  //     background tab would step at 1 s and land the reader mid-sequence on
  //     return. rAF simply does not run when nobody is looking, and resumes.
  //     That matters more now that the sequence loops: a looping figure in a
  //     background tab would otherwise burn the whole time it is not watched.
  // Three phases on one clock: 'step' reveals a frame every FRAME_MS, 'hold'
  // sits on the payoff, 'reset' is the dissolve back to the start. Keeping the
  // reset as a phase rather than a setTimeout means a slide change cancels it
  // with the same cancelAnimationFrame as everything else, and the figure can
  // never be left mid-dissolve on a slide nobody is looking at.
  function stepFigure(rec){
    var i = 0, since = 0, phase = 'step';
    var due = { step: FRAME_MS, hold: LOOP_PAUSE_MS, reset: REWIND_MS };
    rec.fig.classList.add('gf-running');
    showFrame(rec, 0);
    function tick(now){
      if(!since) since = now;
      if(now - since >= due[phase]){
        since = now;
        if(phase === 'step'){
          i += 1;
          // Reaching the end starts the hold rather than ending the loop; the
          // last frame stays up, it just stops being replaced.
          if(i >= rec.frames.length){ phase = 'hold'; }
          else showFrame(rec, i);
        } else if(phase === 'hold'){
          dissolveToStart(rec);
          phase = 'reset';
        } else {
          i = 0;
          showFrame(rec, 0);   // the dissolve already landed here; make it exact
          phase = 'step';
        }
      }
      rec.raf = requestAnimationFrame(tick);
    }
    rec.raf = requestAnimationFrame(tick);
  }

  // Loops for as long as its slide is on screen. Only ONE figure ever runs --
  // renderSlide() stops every other one and closeGuide() stops them all -- so
  // this is a single looping animation, not five.
  //
  // Under prefers-reduced-motion it does not loop and does not step: the last
  // frame, the outcome of the click, is shown and left alone. That reader gets
  // the same information without anything moving, and there is no longer a
  // Replay control to opt back in with, so the still has to stand on its own.
  function playFigure(rec){
    stopFigure(rec);
    if(!rec.frames.length) return;
    var mq = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)');
    if(mq && mq.matches){ showFrame(rec, rec.frames.length - 1); return; }
    resetToStart(rec);
    if(rec.ready){ stepFigure(rec); return; }
    // Wait for the decode, but only up to DECODE_WAIT_MS. A fully decoded run
    // is the goal; a reader staring at a frozen frame 0 because one PNG is
    // slow is worse than the pop it would have caused. The deadline bounds the
    // stall either way, and whichever of the two fires first cancels the other.
    var token = rec.token;
    var start = function(){
      if(rec.token !== token) return;      // slide changed under us
      rec.waiting = null;
      if(rec.deadline){ clearTimeout(rec.deadline); rec.deadline = 0; }
      stepFigure(rec);
    };
    rec.waiting = start;
    rec.deadline = setTimeout(start, DECODE_WAIT_MS);
    preload(rec);
  }

  function isOpen(){ return modal.style.display !== 'none'; }

  function openGuide(fromEl){
    opener = fromEl || document.activeElement;
    current = 0;
    buildFigures();   // first open is when the frames start downloading
    renderSlide();    // which then warms slide 2's frames in the background
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
