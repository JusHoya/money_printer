/**
 * mascot.js — Mr. Krabs Canvas Sprite Animation Engine
 *
 * Loads PNG sprite sheets from /static/img/sprites/ and renders frames
 * using canvas drawImage for crisp, high-quality animation.
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

  const FRAME_INTERVAL = 350;   // ms between animation frames
  const CANVAS_ID = "mascot-canvas";

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
    canvas.style.filter = "drop-shadow(0 0 8px rgba(255, 200, 50, 0.5))";
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

    var elapsed = timestamp - lastFrameTime;
    if (elapsed < FRAME_INTERVAL) return;

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

  // --- Public API ---
  function setMascotState(state) {
    if (!loaded) {
      currentState = state;
      return;
    }
    if (!(state in SHEETS)) {
      console.warn("[mascot] Unknown state:", state, "— falling back to IDLE");
      state = "IDLE";
    }
    if (state === currentState) return;
    currentState = state;
    currentFrameIndex = 0;
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
