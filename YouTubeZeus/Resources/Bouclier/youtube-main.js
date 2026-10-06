/*
 * Bouclier – YouTube ad removal. Runs in the page's own JavaScript world
 * (world: MAIN) at document_start, before YouTube's scripts.
 *
 * What it does, in order of preference:
 *   1. strips ad descriptions (adPlacements, adSlots, playerAds) from the
 *      player data YouTube receives: the initial page data, JSON.parse
 *      results, and fetch / XHR responses for the player endpoints;
 *   2. removes ads from the Shorts feed;
 *   3. neutralises the "abnormality detected" callback YouTube uses to punish
 *      ad blockers;
 *   4. if an ad still starts (server-inserted ads), mutes it, presses Skip and
 *      jumps to its end, then restores the sound.
 *
 * Techniques follow uBlock Origin's public YouTube filters (GPL-3.0). YouTube
 * changes its ad delivery often; this file is the part that needs updating.
 */
(function bouclierYouTube() {
  'use strict';
  if (window.__bouclierYT || location.hostname === 'studio.youtube.com') return;
  try {
    Object.defineProperty(window, '__bouclierYT', { value: true });
  } catch { return; }

  const AD_KEYS = ['adPlacements', 'adSlots', 'playerAds', 'adBreakHeartbeatParams'];
  const AD_KEY_RE = /"(?:adPlacements|adSlots|playerAds)"/;
  const AD_KEY_RE_G = /"(?:adPlacements|adSlots|playerAds)"/g;
  const ENDPOINT_RE = /\/youtubei\/v1\/(?:player|get_watch|reel\/reel_watch_sequence)(?:[?/]|$)|\/playlist\?list=/;
  const fnToString = Function.prototype.toString;

  /* ---------------------------------------------- 1. prune player data */

  function isAdEntry(entry) {
    try {
      return !!entry.command.reelWatchEndpoint.adClientParams.isAd;
    } catch {
      return false;
    }
  }

  function prune(value, depth = 0) {
    if (!value || typeof value !== 'object' || depth > 5) return;
    if (Array.isArray(value)) {
      for (const item of value) prune(item, depth + 1);
      return;
    }
    for (const key of AD_KEYS) {
      if (key in value) delete value[key];
    }
    if (Array.isArray(value.entries) && value.entries.some(isAdEntry)) {
      value.entries = value.entries.filter(e => !isAdEntry(e));
    }
    if (value.playerResponse) prune(value.playerResponse, depth + 1);
    if (value.reelWatchSequenceResponse) prune(value.reelWatchSequenceResponse, depth + 1);
  }

  function looksRelevant(obj) {
    if (!obj || typeof obj !== 'object') return false;
    if (Array.isArray(obj)) return obj.length > 0 && obj.length < 10 && obj.some(looksRelevant);
    return 'adPlacements' in obj || 'playerAds' in obj || 'adSlots' in obj ||
      'playerResponse' in obj || Array.isArray(obj.entries) || 'reelWatchSequenceResponse' in obj;
  }

  // JSON.parse
  const nativeParse = JSON.parse;
  JSON.parse = new Proxy(nativeParse, {
    apply(target, thisArg, args) {
      const result = Reflect.apply(target, thisArg, args);
      try {
        if (looksRelevant(result)) prune(result);
      } catch { /* never break the page */ }
      return result;
    },
  });

  // Initial page data (ytInitialPlayerResponse is set by an inline script)
  for (const name of ['ytInitialPlayerResponse', 'playerResponse']) {
    let current = window[name];
    try {
      if (current) prune(current);
      Object.defineProperty(window, name, {
        configurable: true,
        enumerable: true,
        get() { return current; },
        set(v) {
          try { prune(v); } catch { /* ignore */ }
          current = v;
        },
      });
    } catch { /* property locked */ }
  }

  // fetch()
  const nativeFetch = window.fetch;
  async function rewrite(response) {
    const text = await response.clone().text();
    if (!AD_KEY_RE.test(text)) return response;
    const out = new Response(text.replace(AD_KEY_RE_G, '"no_ads"'), {
      status: response.status,
      statusText: response.statusText,
      headers: response.headers,
    });
    for (const prop of ['ok', 'redirected', 'type', 'url']) {
      try { Object.defineProperty(out, prop, { value: response[prop] }); } catch { /* ignore */ }
    }
    return out;
  }
  window.fetch = new Proxy(nativeFetch, {
    apply(target, thisArg, args) {
      const pending = Reflect.apply(target, thisArg, args);
      let url = '';
      try {
        const input = args[0];
        url = input && typeof input === 'object' && 'url' in input ? String(input.url) : String(input);
      } catch { /* ignore */ }
      if (!ENDPOINT_RE.test(url)) return pending;
      return pending.then(response => rewrite(response).catch(() => response));
    },
  });

  // XMLHttpRequest
  const NativeXHR = window.XMLHttpRequest;
  const xhrUrls = new WeakMap();
  class BouclierXHR extends NativeXHR {
    open(method, url, ...rest) {
      try { xhrUrls.set(this, String(url)); } catch { /* ignore */ }
      return super.open(method, url, ...rest);
    }
    get responseText() {
      const text = super.responseText;
      if (this.readyState !== 4 || !ENDPOINT_RE.test(xhrUrls.get(this) || '') || !AD_KEY_RE.test(text)) return text;
      return text.replace(AD_KEY_RE_G, '"no_ads"');
    }
    get response() {
      const value = super.response;
      if (this.readyState !== 4 || !ENDPOINT_RE.test(xhrUrls.get(this) || '')) return value;
      if (typeof value === 'string') return AD_KEY_RE.test(value) ? value.replace(AD_KEY_RE_G, '"no_ads"') : value;
      if (value && typeof value === 'object' && !(value instanceof ArrayBuffer) && !(value instanceof Blob)) {
        try { prune(value); } catch { /* ignore */ }
      }
      return value;
    }
  }
  try {
    Object.defineProperty(BouclierXHR, 'name', { value: 'XMLHttpRequest' });
    window.XMLHttpRequest = BouclierXHR;
  } catch { /* ignore */ }

  // YouTube can fetch pristine fetch/JSON.parse/XHR from a fresh about:blank iframe
  // to get around hooks like the ones above; hand such iframes our versions.
  const patchFrame = node => {
    try {
      if (!node || node.nodeType !== 1 || node.localName !== 'iframe') return;
      const w = node.contentWindow;
      if (!w || String(w) !== '[object Window]') return;
      const href = w.location.href;
      if (href !== 'about:blank' && href !== location.href) return;
      w.fetch = window.fetch;
      w.XMLHttpRequest = window.XMLHttpRequest;
      w.JSON.parse = JSON.parse;
    } catch { /* cross-origin or detached */ }
  };
  for (const method of ['appendChild', 'insertBefore', 'replaceChild']) {
    const native = Node.prototype[method];
    Node.prototype[method] = new Proxy(native, {
      apply(target, thisArg, args) {
        const result = Reflect.apply(target, thisArg, args);
        patchFrame(args[0]);
        return result;
      },
    });
  }

  // Experiment flags that route player requests around page-level hooks.
  // Traps each object along `path` so the flags stay forced however YouTube
  // builds its config (object literal, later assignment, or ytcfg.set()).
  function forceProps(root, path, props) {
    if (!root || typeof root !== 'object' && typeof root !== 'function') return;
    if (!path) {
      for (const [key, value] of Object.entries(props)) {
        try {
          Object.defineProperty(root, key, { configurable: true, enumerable: true, get: () => value, set: () => {} });
        } catch { /* ignore */ }
      }
      return;
    }
    const dot = path.indexOf('.');
    const head = dot === -1 ? path : path.slice(0, dot);
    const rest = dot === -1 ? '' : path.slice(dot + 1);
    let current = root[head];
    if (current && typeof current === 'object') forceProps(current, rest, props);
    try {
      Object.defineProperty(root, head, {
        configurable: true,
        enumerable: true,
        get: () => current,
        set: v => {
          current = v;
          if (v && typeof v === 'object') forceProps(v, rest, props);
        },
      });
    } catch { /* ignore */ }
  }
  if (location.hostname === 'www.youtube.com') {
    forceProps(window, 'ytcfg.data_.EXPERIMENT_FLAGS', {
      all_web_enable_network_machine: false,
      all_web_network_machine_raw_request: false,
    });
  }

  // The 17-second wait YouTube adds for ad-block users: make it 17 ms.
  const nativeSetTimeout = window.setTimeout;
  window.setTimeout = new Proxy(nativeSetTimeout, {
    apply(target, thisArg, args) {
      try {
        if (args[1] === 17000 && typeof args[0] === 'function' && fnToString.call(args[0]).includes('[native code]')) {
          args[1] = 17;
        }
      } catch { /* ignore */ }
      return Reflect.apply(target, thisArg, args);
    },
  });

  /* ------------------------------------- 3. anti-adblock "abnormality" */

  const nativeThen = Promise.prototype.then;
  Promise.prototype.then = new Proxy(nativeThen, {
    apply(target, thisArg, args) {
      const cb = args[0];
      if (typeof cb === 'function') {
        try {
          if (fnToString.call(cb).includes('onAbnormalityDetected')) args[0] = function () {};
        } catch { /* ignore */ }
      }
      return Reflect.apply(target, thisArg, args);
    },
  });

  /* --------------------------------------- 4. skip ads that still play */

  const SKIP_BUTTONS = [
    '.ytp-skip-ad-button', '.ytp-ad-skip-button', '.ytp-ad-skip-button-modern',
    '.ytp-ad-skip-button-slot button', '.ytp-ad-skip-button-container button',
  ].join(',');
  let mutedByUs = false;
  let lastStatsCheck = 0;
  let serverAd = false;

  function tick() {
    const player = document.getElementById('movie_player');
    if (!player) return;
    const video = player.querySelector('video');
    // Server-inserted ads don't flag the player; the stats overlay does. Checking it
    // costs a little, so do it about once a second, and only while something plays.
    const now = Date.now();
    if (video && !video.paused && now - lastStatsCheck > 900) {
      lastStatsCheck = now;
      try {
        const info = player.getStatsForNerds && player.getStatsForNerds();
        serverAd = !!(info && String(info.debug_info || '').startsWith('SSAP, AD'));
      } catch {
        serverAd = false;
      }
    } else if (!video || video.paused) {
      serverAd = false;
    }
    const adShowing = player.classList.contains('ad-showing') || player.classList.contains('ad-interrupting') || serverAd;
    if (!adShowing) {
      if (mutedByUs && video) {
        video.muted = false;
        mutedByUs = false;
      }
      return;
    }
    if (video && !video.muted) {
      video.muted = true;
      mutedByUs = true;
    }
    document.querySelectorAll(SKIP_BUTTONS).forEach(btn => {
      try { btn.click(); } catch { /* ignore */ }
    });
    if (serverAd) {
      try {
        const p = player.getProgressState && player.getProgressState();
        if (p && p.duration > 0 && p.current < p.duration - 0.3) player.seekTo(p.duration);
      } catch { /* ignore */ }
    } else if (video && Number.isFinite(video.duration) && video.duration > 0 && video.currentTime < video.duration - 0.3) {
      try { video.currentTime = video.duration - 0.1; } catch { /* ignore */ }
    }
  }

  setInterval(() => {
    try { tick(); } catch { /* never break the page */ }
  }, 300);
})();
