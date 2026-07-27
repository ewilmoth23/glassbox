/* Glassbox first-party analytics — tiny pageview + event tracker.
 *
 * No cookies. No GDPR banner needed. ~700 bytes minified.
 * Fires a pageview on load and exposes window.gbtrack(event_type, meta?)
 * for custom events (waitlist_signup, layer_toggle, 3d_tiles_on, …).
 *
 * The backend hashes IP with a daily-rotating salt for unique-visitor
 * counting without storing PII. Country comes from CF-IPCountry header.
 */
(function () {
  function send(event_type, meta) {
    try {
      var body = JSON.stringify({
        event_type: event_type || 'pageview',
        path: location.pathname + location.search,
        source: new URLSearchParams(location.search).get('utm_source') || '',
        referrer: document.referrer || '',
        meta: meta || {},
      });
      // Prefer sendBeacon (fire-and-forget, survives page unload).
      if (navigator.sendBeacon) {
        navigator.sendBeacon('/api/v1/analytics/event',
          new Blob([body], { type: 'application/json' }));
      } else {
        fetch('/api/v1/analytics/event', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: body,
          keepalive: true,
        }).catch(function () {});
      }
    } catch (e) { /* never break the page on analytics */ }
  }
  // Pageview on load
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { send('pageview'); });
  } else {
    send('pageview');
  }
  // Public API
  window.gbtrack = send;
})();
