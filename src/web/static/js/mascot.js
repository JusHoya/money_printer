/**
 * mascot.js — Mr. Krabs Canvas Sprite Animation Engine
 *
 * Loads sprite_data.json and renders pixel-art frames to a <canvas id="mascot-canvas">.
 * Each sprite pixel is rendered as a scaled block (PIXEL_SCALE x PIXEL_SCALE canvas pixels).
 * Animates by cycling through frames at FRAME_INTERVAL ms.
 *
 * Public API:
 *   setMascotState(state)  — switch animation state (e.g. "IDLE", "PANIC", "MONEY_EYES")
 */

(function () {
  "use strict";

  // --- Configuration ---
  const SPRITE_DATA_URL = "/static/img/sprite_data.json";
  const PIXEL_SCALE = 4;        // canvas pixels per sprite pixel
  const FRAME_INTERVAL = 350;   // ms between animation frames
  const CANVAS_ID = "mascot-canvas";

  // Glow effect settings
  const GLOW_COLOR = "rgba(255, 200, 50, 0.45)";
  const GLOW_BLUR = 18;         // px shadow blur

  // --- State ---
  let spriteData = null;
  let currentState = "IDLE";
  let currentFrameIndex = 0;
  let lastFrameTime = 0;
  let animationId = null;
  let canvas = null;
  let ctx = null;

  // --- Color conversion ---
  // Sprite pixels are either null (transparent) or [R, G, B] arrays.
  function rgbToStyle(pixel) {
    return `rgb(${pixel[0]},${pixel[1]},${pixel[2]})`;
  }

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

  function resizeCanvas(frame) {
    if (!frame || frame.length === 0) return;
    const rows = frame.length;
    const cols = frame[0].length;
    const newW = cols * PIXEL_SCALE;
    const newH = rows * PIXEL_SCALE;
    if (canvas.width !== newW || canvas.height !== newH) {
      canvas.width = newW;
      canvas.height = newH;
    }
  }

  // --- Rendering ---
  function renderFrame(frame) {
    if (!ctx || !frame) return;

    resizeCanvas(frame);
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    // First pass: collect non-transparent pixels and draw them with glow
    ctx.save();
    ctx.shadowColor = GLOW_COLOR;
    ctx.shadowBlur = GLOW_BLUR;

    const rows = frame.length;
    const cols = frame[0] ? frame[0].length : 0;

    for (let y = 0; y < rows; y++) {
      const row = frame[y];
      for (let x = 0; x < cols; x++) {
        const pixel = row[x];
        if (pixel === null || pixel === undefined) continue;

        ctx.fillStyle = rgbToStyle(pixel);
        ctx.fillRect(
          x * PIXEL_SCALE,
          y * PIXEL_SCALE,
          PIXEL_SCALE,
          PIXEL_SCALE
        );
      }
    }

    ctx.restore();
  }

  // --- Animation loop ---
  function getCurrentFrames() {
    if (!spriteData) return null;
    return spriteData[currentState] || spriteData["IDLE"] || null;
  }

  function tick(timestamp) {
    animationId = requestAnimationFrame(tick);

    const elapsed = timestamp - lastFrameTime;
    if (elapsed < FRAME_INTERVAL) return;

    lastFrameTime = timestamp;

    const frames = getCurrentFrames();
    if (!frames || frames.length === 0) return;

    // Advance frame index (loop)
    currentFrameIndex = currentFrameIndex % frames.length;
    renderFrame(frames[currentFrameIndex]);
    currentFrameIndex = (currentFrameIndex + 1) % frames.length;
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

  /**
   * Switch the mascot's animation state.
   * @param {string} state — One of "IDLE", "MONEY_EYES", "PANIC", "RUNNING", "TINY_VIOLIN"
   */
  function setMascotState(state) {
    if (!spriteData) {
      // Queue the state change for after load
      currentState = state;
      return;
    }
    if (!(state in spriteData)) {
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

    fetch(SPRITE_DATA_URL)
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        spriteData = data;
        // Apply any state set before load completed
        if (!(currentState in spriteData)) {
          currentState = Object.keys(spriteData)[0] || "IDLE";
        }
        startAnimation();
      })
      .catch(function (err) {
        console.error("[mascot] Failed to load sprite data:", err);
      });
  }

  // Boot when DOM is ready
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // Expose public API
  window.setMascotState = setMascotState;
})();
