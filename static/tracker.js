/**
 * GFI Web Analytics Tracker
 * Pasang di berdonasi.goldenfutureindonesia.org
 * Usage: <script src="https://fundraising.goldenfutureindonesia.org/static/tracker.js" defer></script>
 */
(function () {
  'use strict';

  const ENDPOINT = 'https://fundraising.goldenfutureindonesia.org/api/track';

  // ── Session ID: UUID disimpan di sessionStorage ──────────────────────────────
  function getOrCreateSession() {
    let sid = sessionStorage.getItem('_gfi_sid');
    if (!sid) {
      sid = 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
        const r = Math.random() * 16 | 0;
        return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
      });
      sessionStorage.setItem('_gfi_sid', sid);
    }
    return sid;
  }

  // ── Parse UTM params dari URL ─────────────────────────────────────────────────
  function getUtm() {
    const p = new URLSearchParams(window.location.search);
    const keys = ['utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term'];
    const utm = {};
    let hasUtm = false;
    keys.forEach(k => {
      const v = p.get(k);
      if (v) { utm[k] = v; hasUtm = true; }
    });
    // Simpan UTM ke sessionStorage supaya halaman berikutnya tetap ter-track
    if (hasUtm) sessionStorage.setItem('_gfi_utm', JSON.stringify(utm));
    return hasUtm ? utm : JSON.parse(sessionStorage.getItem('_gfi_utm') || '{}');
  }

  // ── Kirim event ──────────────────────────────────────────────────────────────
  function track(extra) {
    const payload = JSON.stringify({
      session_id:   getOrCreateSession(),
      path:         window.location.pathname,
      referrer:     document.referrer || '',
      ...getUtm(),
      ...extra,
    });
    // sendBeacon untuk page unload; fetch untuk pageview biasa
    if (navigator.sendBeacon) {
      navigator.sendBeacon(ENDPOINT, new Blob([payload], { type: 'application/json' }));
    } else {
      fetch(ENDPOINT, { method: 'POST', body: payload, headers: { 'Content-Type': 'application/json' }, keepalive: true })
        .catch(() => {});
    }
  }

  // ── Pageview saat load ───────────────────────────────────────────────────────
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => track({ event: 'pageview' }));
  } else {
    track({ event: 'pageview' });
  }

  // ── Expose untuk custom events (e.g. donation_started) ──────────────────────
  window.gfiTrack = function (event, extra) { track({ event: event || 'custom', ...(extra || {}) }); };
})();
