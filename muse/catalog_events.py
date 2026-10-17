"""Nonblocking catalog events emitted by Muse-owned MCP connections."""


class CatalogEvents:
    def init_catalog_events(self):
        self._catalog_listeners = set()

    def add_catalog_listener(self, callback):
        self._catalog_listeners.add(callback)
        return lambda: self._catalog_listeners.discard(callback)

    def emit_catalog_event(self, reason):
        for callback in tuple(self._catalog_listeners):
            callback(reason)

    async def handle_catalog_message(self, message):
        payload = getattr(message, 'root', message)
        method = payload.get('method') if isinstance(payload, dict) else getattr(payload, 'method', None)
        if method == 'notifications/tools/list_changed':
            self.emit_catalog_event('tools_changed')
