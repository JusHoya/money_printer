/**
 * mascot.js — Mr. Krabs Canvas Sprite Animation Engine
 *
 * Loads PNG sprite sheets from /static/img/sprites/ and renders frames
 * using canvas drawImage. The canvas is scaled to 64x64 CSS pixels with
 * image-rendering: pixelated (see dashboard.css) and smoothing disabled
 * for crisp pixel-art scaling.
 *
 * Per-state frame intervals plus lightweight state-entry effects
 * (CSS classes and short-lived elements on #mascot-wrap):
 *   PANIC       — one-shot container shake + red glow
 *   MONEY_EYES  — falling gold-square coin burst (max 12, ~1.5s, cleaned up)
 *   TINY_VIOLIN — slow floating musical-note glyph while in state
 *   RUNNING     — slight horizontal bob while in state
 *
 * Public API:
 *   setMascotState(state)  — switch animation state (e.g. "IDLE", "PANIC", "MONEY_EYES")
 */

(function () {
  "use strict";

  // --- Sprite sheet definitions ---
  const SHEETS = {
    IDLE:        { src: "/static/img/sprites/Idle.png",    frames: 4 },
    MONEY_EYES:  { src: "/static/img/sprites/money.png",   frames: 4 },
    PANIC:       { src: "/static/img/sprites/Panic.png",   frames: 3 },
    RUNNING:     { src: "/static/img/sprites/Running.png", frames: 3 },
    TINY_VIOLIN: { src: "/static/img/sprites/Violin.png",  frames: 1 },
  };

  // ms between animation frames, per state
  const FRAME_INTERVALS = {
    IDLE:        450,
    RUNNING:     200,
    MONEY_EYES:  250,
    PANIC:       140,
    TINY_VIOLIN: 500,
  };
  const DEFAULT_INTERVAL = 350;

  const CANVAS_ID = "mascot-canvas";
  const WRAP_ID = "mascot-wrap";
  const COIN_COUNT = 12;

  // --- State ---
  let currentState = "IDLE";
  let currentFrameIndex = 0;
  let lastFrameTime = 0;
  let animationId = null;
  let canvas = null;
  let ctx = null;
  let loaded = false;

  // --- Canvas setup ---
  function initCanvas() {
    canvas = document.getElementById(CANVAS_ID);
    if (!canvas) {
      console.warn("[mascot] No element with id='" + CANVAS_ID + "' found.");
      return false;
    }
    ctx = canvas.getContext("2d");
    return true;
  }

  // --- Preload all sprite sheets ---
  function preloadSheets() {
    var entries = Object.entries(SHEETS);
    var remaining = entries.length;

    entries.forEach(function (pair) {
      var def = pair[1];
      var img = new Image();
      img.onload = function () {
        def.img = img;
        def.frameWidth = Math.floor(img.naturalWidth / def.frames);
        def.frameHeight = img.naturalHeight;
        remaining--;
        if (remaining === 0) {
          loaded = true;
          startAnimation();
        }
      };
      img.onerror = function () {
        console.error("[mascot] Failed to load sprite sheet:", def.src);
        remaining--;
        if (remaining === 0) {
          loaded = true;
          startAnimation();
        }
      };
      img.src = def.src;
    });
  }

  // --- Rendering ---
  function renderFrame() {
    if (!ctx || !loaded) return;

    var sheet = SHEETS[currentState] || SHEETS["IDLE"];
    if (!sheet || !sheet.img) return;

    if (canvas.width !== sheet.frameWidth || canvas.height !== sheet.frameHeight) {
      canvas.width = sheet.frameWidth;
      canvas.height = sheet.frameHeight;
    }

    // Resizing the canvas resets context state — keep smoothing off so the
    // 64x64 CSS upscale stays pixel-crisp.
    ctx.imageSmoothingEnabled = false;

    ctx.clearRect(0, 0, canvas.width, canvas.height);

    var sx = currentFrameIndex * sheet.frameWidth;
    ctx.drawImage(
      sheet.img,
      sx, 0, sheet.frameWidth, sheet.frameHeight,
      0, 0, sheet.frameWidth, sheet.frameHeight
    );
  }

  // --- Animation loop ---
  function tick(timestamp) {
    animationId = requestAnimationFrame(tick);

    var interval = FRAME_INTERVALS[currentState] || DEFAULT_INTERVAL;
    var elapsed = timestamp - lastFrameTime;
    if (elapsed < interval) return;

    lastFrameTime = timestamp;

    var sheet = SHEETS[currentState] || SHEETS["IDLE"];
    if (!sheet || !sheet.img) return;

    currentFrameIndex = currentFrameIndex % sheet.frames;
    renderFrame();
    currentFrameIndex = (currentFrameIndex + 1) % sheet.frames;
  }

  function startAnimation() {
    if (animationId !== null) {
      cancelAnimationFrame(animationId);
    }
    lastFrameTime = 0;
    currentFrameIndex = 0;
    animationId = requestAnimationFrame(tick);
  }

  // --- State-entry effects (CSS classes + short-lived elements on the wrap) ---
  function playStateEffect(state) {
    var wrap = document.getElementById(WRAP_ID);
    if (!wrap) return;

    wrap.classList.remove("fx-panic", "fx-running", "fx-violin");
    wrap.querySelectorAll(".mascot-note").forEach(function (el) { el.remove(); });

    if (state === "PANIC") {
      void wrap.offsetWidth; // restart the one-shot shake animation
      wrap.classList.add("fx-panic");
    } else if (state === "RUNNING") {
      wrap.classList.add("fx-running");
    } else if (state === "TINY_VIOLIN") {
      wrap.classList.add("fx-violin");
      var note = document.createElement("span");
      note.className = "mascot-note";
      note.textContent = "♪";
      wrap.appendChild(note);
    } else if (state === "MONEY_EYES") {
      spawnCoins(wrap);
    }
  }

  function spawnCoins(wrap) {
    var frag = document.createDocumentFragment();
    for (var i = 0; i < COIN_COUNT; i++) {
      var coin = document.createElement("span");
      coin.className = "mascot-coin";
      coin.style.left = (4 + Math.random() * 56) + "px";
      coin.style.animationDelay = (Math.random() * 0.5).toFixed(2) + "s";
      frag.appendChild(coin);
    }
    wrap.appendChild(frag);
    // 1.5s fall + up to 0.5s stagger, then cleanup (coins only)
    setTimeout(function () {
      wrap.querySelectorAll(".mascot-coin").forEach(function (el) { el.remove(); });
    }, 2100);
  }

  // --- Public API ---
  function setMascotState(state) {
    if (!(state in SHEETS)) {
      console.warn("[mascot] Unknown state:", state, "— falling back to IDLE");
      state = "IDLE";
    }
    if (state === currentState) return;
    currentState = state;
    currentFrameIndex = 0;
    playStateEffect(state);
    if (loaded) renderFrame();
  }

  // --- Initialization ---
  function init() {
    if (!initCanvas()) return;
    preloadSheets();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.setMascotState = setMascotState;
})();
