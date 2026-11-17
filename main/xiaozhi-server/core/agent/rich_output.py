"""Separate streamed screen text from speech, including split Markdown tokens."""
import asyncio
import re
import time

from core.agent import media_output


def supported(conn):
    return (getattr(conn, 'features', {}) or {}).get('rich_text', {}).get('version') == 1


class SpeechFilter:
    def __init__(self):
        self.buffer = ''
        self.fence = None
        self.line_start = True
        self.indented = False
        self.code_announced = False
        self.image_announced = False

    def feed(self, text, final=False):
        self.buffer += text
        output = []
        while self.buffer:
            if self.indented:
                end = self.buffer.find('\n')
                if end < 0:
                    self.buffer = ''
                    break
                self.buffer = self.buffer[end + 1:]
                self.indented = False
                self.line_start = True
                continue
            if self.fence:
                end = self.buffer.find(self.fence)
                if end < 0:
                    self.buffer = '' if final else self.buffer[-(len(self.fence) - 1):]
                    break
                self.buffer = self.buffer[end + len(self.fence):]
                self.fence = None
                continue
            prefixes = ('```', '~~~', '![', 'http://', 'https://', 'www.')
            if not final and any(token.startswith(self.buffer) and token != self.buffer for token in prefixes):
                break
            if self.line_start and (self.buffer.startswith('    ') or self.buffer.startswith('\t')):
                self.indented = True
                if not self.code_announced:
                    output.append('代码已显示在屏幕上。')
                    self.code_announced = True
                continue
            if self.line_start and not final and self.buffer.isspace() and '\n' not in self.buffer and len(self.buffer) < 4:
                break
            if self.buffer.startswith(('```', '~~~')):
                marker = self.buffer[0]
                length = len(self.buffer) - len(self.buffer.lstrip(marker))
                if length == len(self.buffer) and not final:
                    break
                self.fence = marker * length
                self.buffer = self.buffer[length:]
                if not self.code_announced:
                    output.append('代码已显示在屏幕上。')
                    self.code_announced = True
                continue
            if self.buffer.startswith(('![', '[')):
                image = self.buffer.startswith('![')
                start = 2 if image else 1
                label_end = self.buffer.find(']', start)
                if label_end < 0 or label_end + 1 >= len(self.buffer):
                    if not final:
                        break
                    self.buffer = ''
                    break
                if self.buffer[label_end + 1] == '(':
                    depth, end = 1, label_end + 2
                    while end < len(self.buffer) and depth:
                        if self.buffer[end] == '(':
                            depth += 1
                        elif self.buffer[end] == ')':
                            depth -= 1
                        end += 1
                    if depth and not final:
                        break
                    if image:
                        if not self.image_announced:
                            output.append('图片请看屏幕。')
                            self.image_announced = True
                    else:
                        output.append(self.buffer[start:label_end])
                    self.buffer = self.buffer[end:]
                    continue
                if image:
                    if not self.image_announced:
                        output.append('图片请看屏幕。')
                        self.image_announced = True
                    self.buffer = self.buffer[label_end + 1:]
                    continue
            if self.buffer.startswith(('http://', 'https://', 'www.')):
                match = re.search(r'[\s<>，。！？]', self.buffer)
                if not match and not final:
                    break
                self.buffer = self.buffer[match.start():] if match else ''
                continue
            char, self.buffer = self.buffer[0], self.buffer[1:]
            self.line_start = char == '\n'
            if char not in '`*#_>':
                output.append(char)
        return ''.join(output)

    def flush(self):
        text = self.feed('', final=True)
        self.fence = None
        self.indented = False
        self.line_start = True
        return text


class ScreenStream:
    def __init__(self, conn, sentence_id, stale):
        self.conn, self.sentence_id, self.stale = conn, sentence_id, stale
        self.text = ''
        self.index = 0
        self.last_sent = 0

    def feed(self, text, final=False):
        self.text += text
        if not supported(self.conn) or not self.text or self.stale():
            return
        if not final and time.monotonic() - self.last_sent < .15:
            return
        payload = {'type': 'assistant_text', 'version': 1, 'session_id': self.conn.session_id,
                   'sentence_id': self.sentence_id, 'id': f'{self.sentence_id}:{self.index}',
                   'text': self.text[:60000], 'final': final}
        future = asyncio.run_coroutine_threadsafe(media_output._send(self.conn, payload, self.stale), self.conn.loop)
        try:
            future.result(timeout=5)
        except Exception:
            future.cancel()
            raise
        self.last_sent = time.monotonic()

    def finish_message(self):
        self.feed('', final=True)
        self.text = ''
        self.index += 1
        self.last_sent = 0
