// SmartVision Attendance Portal - Minimal PWA Service Worker
const CACHE_NAME = 'smartvision-v1';

// Install Event: Activate worker immediately
self.addEventListener('install', (event) => {
  self.skipWaiting();
});

// Activate Event: Claim clients immediately
self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

// Fetch Event: Network-first strategy to guarantee live database data, authentication, and live video
self.addEventListener('fetch', (event) => {
  // Only handle GET requests and skip non-HTTP schemes or API/video/camera endpoints
  if (event.request.method !== 'GET') return;

  event.respondWith(
    fetch(event.request).catch(() => {
      // In case device is completely offline and requesting page
      return caches.match(event.request);
    })
  );
});
