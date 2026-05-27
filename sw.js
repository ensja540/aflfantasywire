/* AFLFantasyWire service worker — Web Push receiver + minimal PWA shell.
 * Kept deliberately tiny: it does NOT cache/intercept normal requests (the
 * site is always served fresh by the Cloudflare Worker); it exists so the app
 * is installable and can receive push notifications for watchlisted players. */

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

// An empty fetch handler keeps the SW "installable" without altering responses.
self.addEventListener("fetch", () => {});

self.addEventListener("push", (event) => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; }
  catch (_) { d = { body: event.data ? event.data.text() : "" }; }

  const title = d.title || "AFLFantasyWire";
  const options = {
    body:  d.body || "",
    icon:  "/icon-192.png",
    badge: "/icon-192.png",
    tag:   d.tag || undefined,          // collapse duplicate alerts for the same item
    data:  { url: d.url || "/" },
    requireInteraction: false,
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      for (const c of list) {
        if ("focus" in c) { try { c.navigate(target); } catch (_) {} return c.focus(); }
      }
      if (self.clients.openWindow) return self.clients.openWindow(target);
    })
  );
});
