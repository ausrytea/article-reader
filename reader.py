#!/usr/bin/env python3
"""
Article Reader Agent
Reads web articles aloud in English or Lithuanian.
Runs a background reader thread so you can queue articles and keep working.

Usage:
    python reader.py                    # interactive mode
    python reader.py <url>              # read single article and exit
    python reader.py --text "..." --lang lt  # read text directly
"""

import sys
import os
import subprocess
import tempfile
import threading
import queue
import argparse
import json
import re
import signal
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SUPPORTED_LANGS = {'en', 'lt'}
current_process = None
process_lock = threading.Lock()
read_queue = queue.Queue()
stop_event = threading.Event()
cancel_event = threading.Event()  # cancels current item without stopping worker
pause_event = threading.Event()   # pauses playback between chunks
server_status = {'state': 'idle', 'message': 'Ready', 'current': ''}
status_lock = threading.Lock()


def set_status(state: str, message: str):
    with status_lock:
        server_status['state'] = state
        server_status['message'] = message


def reset_transcript():
    with status_lock:
        server_status['current'] = ''


def append_transcript(chunk: str):
    with status_lock:
        server_status['current'] = chunk


# ─── Article Extraction ───────────────────────────────────────────────────────

def fetch_article(url: str) -> tuple:
    """Fetch article from URL. Returns (title, text)."""
    try:
        import trafilatura
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            raise ValueError("Could not download URL")

        # Try with metadata first
        raw = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=False,
            output_format='json',
            with_metadata=True
        )
        if raw:
            data = json.loads(raw)
            return data.get('title', '') or '', data.get('text', '') or ''

        # Fallback: plain text
        text = trafilatura.extract(downloaded, include_comments=False)
        return '', text or ''

    except Exception as e:
        print(f"[Error] Fetch failed: {e}")
        return '', ''


# ─── Language Detection ───────────────────────────────────────────────────────

def detect_language(text: str) -> str:
    """Auto-detect 'en' or 'lt' from text sample."""
    try:
        from langdetect import detect
        lang = detect(text[:1000])
        return lang if lang in SUPPORTED_LANGS else 'en'
    except Exception:
        return 'en'


# ─── Text Chunking ────────────────────────────────────────────────────────────

def chunk_text(text: str, max_chars: int = 450) -> list:
    """Split into chunks at sentence boundaries (gTTS has a ~5000 char limit)."""
    # Normalize whitespace
    text = re.sub(r'\s+', ' ', text).strip()

    # Split on sentence-ending punctuation
    sentences = re.split(r'(?<=[.!?])\s+', text)

    chunks = []
    current = ''
    for sentence in sentences:
        if not sentence.strip():
            continue
        if len(current) + len(sentence) + 1 <= max_chars:
            current = (current + ' ' + sentence).strip()
        else:
            if current:
                chunks.append(current)
            # If a single sentence is too long, split on commas
            if len(sentence) > max_chars:
                parts = sentence.split(', ')
                sub = ''
                for part in parts:
                    if len(sub) + len(part) + 2 <= max_chars:
                        sub = (sub + ', ' + part).lstrip(', ')
                    else:
                        if sub:
                            chunks.append(sub)
                        sub = part
                if sub:
                    chunks.append(sub)
                current = ''
            else:
                current = sentence

    if current:
        chunks.append(current)

    return chunks


# ─── TTS + Playback ───────────────────────────────────────────────────────────

EDGE_VOICES = {
    'lt': {'f': 'lt-LT-OnaNeural',    'm': 'lt-LT-LeonasNeural'},
    'en': {'f': 'en-US-JennyNeural',  'm': 'en-GB-RyanNeural'},
}


def _tts_to_file(chunk: str, lang: str, rate: float = 1.0, gender: str = 'f') -> str:
    """Generate TTS audio to a temp mp3 file. Returns the file path."""
    import asyncio

    # Convert float rate (0.5–2.0) to edge-tts prosody format e.g. "+50%", "-25%"
    rate_str = f"{int((rate - 1.0) * 100):+d}%"

    async def _edge(chunk, voice, rate_str):
        import edge_tts
        with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as f:
            tmp = f.name
        communicate = edge_tts.Communicate(chunk, voice, rate=rate_str)
        await communicate.save(tmp)
        return tmp

    voice = (EDGE_VOICES.get(lang) or {}).get(gender) or (EDGE_VOICES.get(lang) or {}).get('f')
    if voice:
        try:
            return asyncio.run(_edge(chunk, voice, rate_str))
        except Exception as e:
            print(f"[edge-tts error] {e}, falling back to gTTS")

    # gTTS fallback (no rate support, play at normal speed)
    from gtts import gTTS
    tts = gTTS(text=chunk, lang=lang, slow=False)
    with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as f:
        tmp = f.name
    tts.save(tmp)
    return tmp


def speak_text(text: str, lang: str = 'en', rate: float = 1.0, gender: str = 'f') -> None:
    """Convert text to speech and play, chunk by chunk."""
    global current_process

    chunks = chunk_text(text)
    if not chunks:
        return

    for chunk in chunks:
        # Wait while paused
        while pause_event.is_set():
            if stop_event.is_set() or cancel_event.is_set():
                return
            threading.Event().wait(0.2)

        if stop_event.is_set() or cancel_event.is_set():
            return

        tmp_path = None
        try:
            append_transcript(chunk)
            tmp_path = _tts_to_file(chunk, lang, rate, gender)

            with process_lock:
                if stop_event.is_set() or cancel_event.is_set():
                    break
                current_process = subprocess.Popen(
                    ['afplay', tmp_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )

            current_process.wait()

        except Exception as e:
            print(f"\n[TTS Error] {e}")
            # Fallback: macOS say command (English only)
            if lang == 'en':
                try:
                    subprocess.run(['say', chunk], check=False)
                except Exception:
                    pass
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass


def stop_current():
    """Interrupt currently playing audio."""
    global current_process
    with process_lock:
        if current_process and current_process.poll() is None:
            current_process.terminate()
            current_process = None


# ─── Reader Worker ────────────────────────────────────────────────────────────

def reader_worker():
    """Background thread: dequeues and reads articles."""
    while not stop_event.is_set():
        try:
            item = read_queue.get(timeout=1)
        except queue.Empty:
            continue

        if item is None:
            break

        cancel_event.clear()
        payload, lang, is_url, rate, gender = item

        try:
            if cancel_event.is_set():
                read_queue.task_done()
                continue
            if is_url:
                print(f"\n[Fetching] {payload}")
                set_status('reading', 'Fetching article...')
                title, text = fetch_article(payload)
                if not text:
                    msg = "Could not extract article text. Check the URL."
                    print(f"[Error] {msg}")
                    set_status('error', msg)
                    read_queue.task_done()
                    continue
                if title:
                    print(f"[Title] {title}")
                    print(f"[Length] ~{len(text.split())} words")
            else:
                title = ''
                text = payload

            if lang == 'auto':
                detected = detect_language(text)
                print(f"[Language] Detected: {detected.upper()}")
                lang = detected

            set_status('reading', f'Reading ({lang.upper()})...')
            reset_transcript()
            print(f"[Reading] Starting... (type 'stop' to interrupt)\n")

            if title:
                speak_text(title + '.', lang, rate, gender)

            speak_text(text, lang, rate, gender)

            if not stop_event.is_set():
                print("\n[Done] Finished reading.")
                set_status('idle', 'Done')

        except Exception as e:
            print(f"\n[Error] {e}")
            set_status('error', str(e))
        finally:
            if not stop_event.is_set() and read_queue.empty():
                set_status('idle', 'Ready')
            read_queue.task_done()


# ─── Queue Helpers ────────────────────────────────────────────────────────────

def clear_queue():
    """Drain the queue."""
    while not read_queue.empty():
        try:
            read_queue.get_nowait()
            read_queue.task_done()
        except Exception:
            pass


def enqueue(payload: str, lang: str, is_url: bool, rate: float = 1.0, gender: str = 'f'):
    read_queue.put((payload, lang, is_url, rate, gender))


# ─── Interactive Mode ─────────────────────────────────────────────────────────

HELP_TEXT = """
Commands:
  <url>              Queue a web article for reading
  text:<...>         Read typed/pasted text directly
  stop               Stop current playback and clear queue
  skip               Stop current article, play next
  lang:<en|lt|auto>  Set language (default: auto-detect)
  status             Show queue length
  help               Show this help
  quit / exit        Exit the reader
"""

def interactive_mode(default_lang: str = 'auto'):
    print("=" * 50)
    print("  Article Reader Agent  |  EN + LT")
    print("=" * 50)
    print(HELP_TEXT)

    current_lang = default_lang
    worker = threading.Thread(target=reader_worker, daemon=True)
    worker.start()

    def handle_sigint(sig, frame):
        print("\n[Interrupted] Type 'quit' to exit or keep using the reader.")

    signal.signal(signal.SIGINT, handle_sigint)

    while True:
        try:
            prompt = f"\n[lang:{current_lang}] > "
            user_input = input(prompt).strip()
        except EOFError:
            break

        if not user_input:
            continue

        if user_input in ('quit', 'exit', 'q'):
            break

        elif user_input == 'help':
            print(HELP_TEXT)

        elif user_input == 'stop':
            stop_current()
            clear_queue()
            print("[Stopped and queue cleared]")

        elif user_input == 'skip':
            stop_current()
            print("[Skipped]")

        elif user_input == 'status':
            n = read_queue.qsize()
            print(f"[Queue] {n} article(s) waiting")

        elif user_input.startswith('lang:'):
            lang = user_input[5:].strip().lower()
            if lang in {'en', 'lt', 'auto'}:
                current_lang = lang
                print(f"[Language set to: {current_lang}]")
            else:
                print("[Supported languages: en, lt, auto]")

        elif user_input.startswith('text:'):
            text = user_input[5:].strip()
            if text:
                enqueue(text, current_lang, is_url=False)
                print("[Queued for reading]")
            else:
                print("[Empty text]")

        elif re.match(r'https?://', user_input):
            enqueue(user_input, current_lang, is_url=True)
            print(f"[Queued] {read_queue.qsize()} article(s) in queue")

        else:
            # Treat bare input as text to read
            enqueue(user_input, current_lang, is_url=False)
            print("[Queued as text]")

    stop_event.set()
    stop_current()
    print("\n[Goodbye]")


# ─── HTTP Server ──────────────────────────────────────────────────────────────

class ReaderHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ('/', '/index.html'):
            html = (Path(__file__).parent / 'app.html').read_bytes()
            self._respond(200, 'text/html', html)
        elif self.path == '/status':
            with status_lock:
                data = json.dumps(server_status).encode()
            self._respond(200, 'application/json', data)  # includes transcript
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length) or b'{}')

        if self.path == '/speak':
            payload = body.get('text', '').strip()
            lang = body.get('lang', 'auto')
            is_url = bool(body.get('is_url', False))
            rate = float(body.get('rate', 1.0))
            gender = body.get('gender', 'f')
            print(f"[speak] rate={rate} gender={gender} lang={lang}", flush=True)
            title, transcript = '', ''
            if payload:
                if is_url:
                    set_status('reading', 'Fetching article...')
                    title, transcript = fetch_article(payload)
                    if transcript:
                        enqueue(transcript, lang, False, rate, gender)
                        set_status('reading', 'Starting...')
                    else:
                        set_status('error', 'Could not extract article text.')
                else:
                    transcript = payload
                    enqueue(payload, lang, False, rate, gender)
                    set_status('reading', 'Starting...')
            resp = json.dumps({'status': 'queued', 'title': title}).encode()
            self._respond(200, 'application/json', resp)
        elif self.path == '/pause':
            pause_event.set()
            with process_lock:
                if current_process and current_process.poll() is None:
                    import signal as _sig
                    try: os.kill(current_process.pid, _sig.SIGSTOP)
                    except Exception: pass
            set_status('reading', 'Paused')
            self._respond(200, 'application/json', b'{"status":"paused"}')
        elif self.path == '/resume':
            with process_lock:
                if current_process and current_process.poll() is None:
                    import signal as _sig
                    try: os.kill(current_process.pid, _sig.SIGCONT)
                    except Exception: pass
            pause_event.clear()
            set_status('reading', 'Reading...')
            self._respond(200, 'application/json', b'{"status":"resumed"}')
        elif self.path == '/stop':
            pause_event.clear()
            cancel_event.set()
            stop_current()
            clear_queue()
            set_status('idle', 'Ready')
            self._respond(200, 'application/json', b'{"status":"stopped"}')
        else:
            self.send_response(404)
            self.end_headers()

    def _respond(self, code, content_type, body: bytes):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # silence request logs


def serve_mode(port: int = 7654):
    worker = threading.Thread(target=reader_worker, daemon=True)
    worker.start()

    server = ThreadingHTTPServer(('127.0.0.1', port), ReaderHandler)
    url = f'http://localhost:{port}'
    print(f'Article Reader running at {url}')
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        stop_current()


# ─── Entry Point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Article Reader Agent — reads web articles aloud in EN/LT'
    )
    parser.add_argument('url', nargs='?', help='Article URL to read immediately')
    parser.add_argument('--text', help='Read this text directly instead of a URL')
    parser.add_argument(
        '--lang', default='auto',
        choices=['en', 'lt', 'auto'],
        help='Language (default: auto-detect)'
    )
    parser.add_argument('--serve', action='store_true', help='Start web UI server')
    parser.add_argument('--port', type=int, default=7654, help='Port for --serve (default: 7654)')
    args = parser.parse_args()

    if args.serve:
        serve_mode(args.port)

    elif args.text:
        # One-shot text mode
        lang = args.lang
        if lang == 'auto':
            lang = detect_language(args.text)
            print(f"[Language] Detected: {lang.upper()}")
        speak_text(args.text, lang)

    elif args.url:
        # One-shot URL mode
        print(f"[Fetching] {args.url}")
        title, text = fetch_article(args.url)
        if not text:
            print("[Error] Could not extract article text.")
            sys.exit(1)

        lang = args.lang
        if lang == 'auto':
            lang = detect_language(text)
            print(f"[Language] Detected: {lang.upper()}")

        if title:
            print(f"[Title] {title}")
        speak_text((title + '. ' if title else '') + text, lang)

    else:
        # Interactive mode
        interactive_mode(default_lang=args.lang)


if __name__ == '__main__':
    main()
