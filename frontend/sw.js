// Stockwin Service Worker — stale-while-revalidate for static assets, network-first for HTML
var CACHE_STATIC = 'stockwin-static-v1';
var CACHE_HTML = 'stockwin-html-v1';

// Static assets: cache-first with background refresh (stale-while-revalidate)
self.addEventListener('fetch', function(event) {
  var url = new URL(event.request.url);

  // Only handle same-origin requests
  if (url.origin !== self.location.origin) return;

  // HTML pages: network-first, fallback to cache
  if (event.request.mode === 'navigate' || url.pathname.endsWith('.html') || url.pathname === '/') {
    event.respondWith(networkFirst(event.request, CACHE_HTML));
    return;
  }

  // Static assets (css/js/lib): cache-first with background update
  if (/\.(css|js)$/.test(url.pathname) || url.pathname.startsWith('/css/') || url.pathname.startsWith('/js/') || url.pathname.startsWith('/lib/')) {
    event.respondWith(staleWhileRevalidate(event.request, CACHE_STATIC));
    return;
  }
});

function networkFirst(request, cacheName) {
  return fetch(request).then(function(response) {
    if (response && response.status === 200) {
      var clone = response.clone();
      caches.open(cacheName).then(function(cache) { cache.put(request, clone); });
    }
    return response;
  }).catch(function() {
    return caches.match(request);
  });
}

function staleWhileRevalidate(request, cacheName) {
  return caches.open(cacheName).then(function(cache) {
    return cache.match(request).then(function(cached) {
      var fetchPromise = fetch(request).then(function(response) {
        if (response && response.status === 200) {
          cache.put(request, response.clone());
        }
        return response;
      });
      return cached || fetchPromise;
    });
  });
}
