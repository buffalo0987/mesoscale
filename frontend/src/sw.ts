/// <reference lib="webworker" />
import { clientsClaim } from 'workbox-core';
import { ExpirationPlugin } from 'workbox-expiration';
import { cleanupOutdatedCaches, precacheAndRoute } from 'workbox-precaching';
import { registerRoute } from 'workbox-routing';
import { StaleWhileRevalidate } from 'workbox-strategies';

declare let self: ServiceWorkerGlobalScope & {
  __WB_MANIFEST: Array<{ url: string; revision: string | null }>;
};

self.skipWaiting();
clientsClaim();
cleanupOutdatedCaches();
precacheAndRoute(self.__WB_MANIFEST);

// Cache forecast & bootstrap responses for instant cold-launch and storm cell-drop resilience
registerRoute(
  ({ url, request }) =>
    request.method === 'GET' &&
    (url.pathname.startsWith('/api/bootstrap') ||
      url.pathname.endsWith('/forecast') ||
      url.pathname.endsWith('/verification')),
  new StaleWhileRevalidate({
    cacheName: 'mesoscale-api-cache-v1',
    plugins: [
      new ExpirationPlugin({
        maxEntries: 35,
        maxAgeSeconds: 24 * 60 * 60,
      }),
    ],
  })
);

// Native VAPID Web Push handler
self.addEventListener('push', (event) => {
  let payload: {
    title?: string;
    body?: string;
    tag?: string;
    severity?: string;
    url?: string;
  } = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch {
    payload = { title: 'Mesoscale Weather Alert', body: event.data?.text() ?? '' };
  }

  const isCritical = payload.severity === 'critical';
  const options: NotificationOptions & { vibrate?: number[]; renotify?: boolean } = {
    body: payload.body || 'Tap to view updated hyper-local forecast.',
    icon: '/icon.svg',
    badge: '/icon.svg',
    tag: payload.tag || 'mesoscale-alert',
    renotify: true,
    requireInteraction: isCritical,
    vibrate: isCritical ? [250, 100, 250, 100, 500] : [150, 80, 150],
    data: { url: payload.url || '/' },
  };

  event.waitUntil(
    self.registration.showNotification(payload.title || 'Mesoscale Alert', options)
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const targetUrl = (event.notification.data?.url as string) || '/';

  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windowClients) => {
      for (const client of windowClients) {
        if ('focus' in client) {
          client.navigate(targetUrl);
          return client.focus();
        }
      }
      return self.clients.openWindow(targetUrl);
    })
  );
});
