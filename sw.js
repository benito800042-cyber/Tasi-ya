self.addEventListener('push', event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (_) { data = {title:'Taxi Ya', body:event.data?.text() || 'Nueva solicitud'}; }
  event.waitUntil(self.registration.showNotification(data.title || 'Taxi Ya', {
    body: data.body || 'Nueva solicitud disponible',
    icon: '/static/icon-192.png', badge: '/static/icon-192.png',
    vibrate: [300,150,300,150,500], tag: data.ride_id || 'taxi-ya-ride', renotify: true,
    data: {url: '/', ride_id: data.ride_id}
  }));
});
self.addEventListener('notificationclick', event => { event.notification.close(); event.waitUntil(clients.matchAll({type:'window', includeUncontrolled:true}).then(list => list.length ? list[0].focus() : clients.openWindow('/'))); });
