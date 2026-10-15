"""Browser transport owned by the Chrome extension; no browser process is launched."""
import asyncio
import base64
import secrets
import struct
from contextlib import suppress
from urllib.parse import urlparse


class Browser:
    def __init__(self, directory=None, headless=False, channel=None):
        self.lock = asyncio.Lock()
        self.connection = None
        self.pending = {}
        self.on_frame = None
        self.streaming = False
        self.width, self.height = 1280, 800
        self.image_width, self.image_height = 1280, 800
        self.url = ''
        self.last_observation = None

    @property
    def connected(self):
        return self.connection is not None

    async def start(self):
        if not self.connected:
            raise ConnectionError('Chrome 连接器尚未连接，请确认扩展已更新并启用自动连接')

    async def active_page(self):
        await self.start()
        return None

    async def request(self, action, **params):
        await self.start()
        connection = self.connection
        request_id = secrets.token_hex(12)
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await connection.send_json({'type': 'command', 'id': request_id,
                                        'action': action, **params})
            return await asyncio.wait_for(future, 35)
        finally:
            self.pending.pop(request_id, None)

    def disconnect(self, connection):
        if self.connection is not connection:
            return
        self.connection = None
        self.streaming = False
        for future in self.pending.values():
            if not future.done():
                future.set_exception(ConnectionError('浏览器扩展已断开'))

    def receive(self, message):
        if message.get('type') == 'result':
            future = self.pending.get(message.get('id'))
            if future and not future.done():
                if message.get('error'):
                    future.set_exception(RuntimeError(message['error']))
                else:
                    result = message.get('result')
                    if isinstance(result, dict):
                        if 'elements' in result:self.last_observation=result
                        self.width = result.get('width', self.width)
                        self.height = result.get('height', self.height)
                    future.set_result(result)
        elif message.get('type') == 'frame' and self.on_frame:
            self.width = message.get('width', self.width)
            self.height = message.get('height', self.height)
            self.url = message.get('url', '')
            self.on_frame(message['data'], self.url)

    async def observe(self):
        return await self.request('read')

    async def perform(self, action, url='', ref=-1, text='', amount=500):
        if action == 'open' and urlparse(url).scheme not in ('http', 'https'):
            raise ValueError('只支持 http/https 网页')
        async with self.lock:
            try:
                return await self.request(action, url=url, ref=ref, text=text, amount=amount)
            except RuntimeError as error:
                # These errors are emitted by point() BEFORE any input is sent.
                # Return a fresh observation rather than retrying a guessed target.
                message=str(error).splitlines()[0].removeprefix('Error: ')
                if action in ('hover','click','click_text','fill','select','press') and message in (
                        '需要唯一可见文字','元素已失效，请重新读取页面','元素被遮挡'):
                    observed=await self.request('read')
                    return {**observed,'status':'needs_observation','action_performed':False,
                            'requested_action':action,'message':message,
                            'recovery':'本次操作未执行。已重新读取页面；只使用下方最新元素ref，结合文字与context重新选择目标。文字不唯一时不要重复click_text；无可用元素时查看最新截图。'}
                if action!='hover' or str(error)!='未知操作':raise
                # Old extension already supports manual mouseMoved. Re-read and
                # compare semantic identity before using current coordinates.
                previous=next((e for e in (self.last_observation or {}).get('elements',[]) if e.get('ref')==ref),None)
                observed=await self.request('read')
                target=next((e for e in observed.get('elements',[]) if e.get('ref')==ref),None)
                if not previous or not target or any(previous.get(k)!=target.get(k) for k in ('tag','text','context','href')):
                    raise RuntimeError('页面元素已变化，请重新读取后再悬停')
                x,y=target.get('x'),target.get('y')
                if not isinstance(x,(int,float)) or not isinstance(y,(int,float)) or not 0<=x<self.width or not 0<=y<self.height:
                    raise RuntimeError('悬停目标不在可见范围，请滚动并重新读取')
                await self.request('manual',input={'action':'move','x':x,'y':y})
                await asyncio.sleep(.2)
                return await self.request('read')

    async def screenshot(self, page=None):
        result = await self.request('screenshot')
        picture = base64.b64decode(result['data'])
        # CDP screenshots can contain device pixels on Retina screens, while
        # Input.dispatchMouseEvent uses CSS pixels. Keep both coordinate spaces.
        self.image_width, self.image_height = self.width, self.height
        offset = 2
        while offset + 4 < len(picture):
            if picture[offset] != 0xff:
                break
            marker = picture[offset + 1]
            length = struct.unpack('>H', picture[offset + 2:offset + 4])[0]
            if marker in (0xc0, 0xc1, 0xc2) and offset + 9 <= len(picture):
                self.image_height, self.image_width = struct.unpack('>HH', picture[offset + 5:offset + 9])
                break
            offset += 2 + length
        return picture

    async def frame(self):
        if self.lock.locked() or not self.connected:
            return None
        async with self.lock:
            return await self.screenshot(), self.url

    async def ensure_stream(self, on_frame):
        self.on_frame = on_frame
        if self.connected and not self.streaming:
            await self.request('stream_start')
            self.streaming = True

    async def stop_stream(self):
        if self.streaming and self.connected:
            with suppress(Exception):
                await self.request('stream_stop')
        self.streaming = False

    async def manual(self, action, x=0, y=0, text='', x2=0, y2=0, button='left', dx=0):
        if action == 'open' and urlparse(text).scheme not in ('http', 'https'):
            raise ValueError('只支持 http/https 网页')
        async with self.lock:
            await self.request('manual', input={'action': action, 'x': x, 'y': y,
                'text': text, 'x2': x2, 'y2': y2, 'button': button, 'dx': dx})

    async def release_input(self):
        if self.connected:
            with suppress(Exception):
                await self.request('release')

    async def close(self):
        await self.stop_stream()
        if self.connection:
            connection = self.connection
            self.disconnect(connection)
            with suppress(Exception):
                await connection.close()
