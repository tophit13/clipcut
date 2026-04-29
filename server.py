import os, uuid, threading, subprocess, zipfile, re, sqlite3, json, tempfile, hmac, hashlib, time
import requests as req_lib
from datetime import date
from io import BytesIO
from flask import Flask, request, jsonify, send_file, session
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-clipcut-2024')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CORS(app, supports_credentials=True)

ASSEMBLYAI_API_KEY           = os.environ.get('ASSEMBLYAI_API_KEY', '')
YOUTUBE_OAUTH_TOKEN          = os.environ.get('YOUTUBE_OAUTH_TOKEN', '').strip()

# Write OAuth token to cache dir so yt-dlp can find it on startup
YT_CACHE_DIR = '/tmp/yt-dlp-cache'
if YOUTUBE_OAUTH_TOKEN:
    _oauth_dir = os.path.join(YT_CACHE_DIR, 'youtube-oauth2')
    os.makedirs(_oauth_dir, exist_ok=True)
    with open(os.path.join(_oauth_dir, 'token.json'), 'w') as _f:
        _f.write(YOUTUBE_OAUTH_TOKEN)
PADDLE_CLIENT_TOKEN          = os.environ.get('PADDLE_CLIENT_TOKEN', '')
PADDLE_WEBHOOK_SECRET        = os.environ.get('PADDLE_WEBHOOK_SECRET', '')
PADDLE_PRO_PRICE_ID          = os.environ.get('PADDLE_PRO_PRICE_ID', '')
PADDLE_PRO_YEARLY_PRICE_ID   = os.environ.get('PADDLE_PRO_YEARLY_PRICE_ID', '')
PADDLE_CREATOR_PRICE_ID      = os.environ.get('PADDLE_CREATOR_PRICE_ID', '')
PADDLE_CREATOR_YEARLY_PRICE_ID = os.environ.get('PADDLE_CREATOR_YEARLY_PRICE_ID', '')
PADDLE_BIZ_PRICE_ID          = os.environ.get('PADDLE_BIZ_PRICE_ID', '')
PADDLE_BIZ_YEARLY_PRICE_ID   = os.environ.get('PADDLE_BIZ_YEARLY_PRICE_ID', '')

CLIPS_DIR = 'clips'
os.makedirs(CLIPS_DIR, exist_ok=True)

DB_PATH = 'clipcut.db'

PLAN_LIMITS = {
    'free':     {'clips_per_day': 10,   'max_quality': 720,  'subtitles': False},
    'pro':      {'clips_per_day': None, 'max_quality': 1080, 'subtitles': True},
    'creator':  {'clips_per_day': None, 'max_quality': 2160, 'subtitles': True},
    'business': {'clips_per_day': None, 'max_quality': 2160, 'subtitles': True},
}

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                session_id        TEXT PRIMARY KEY,
                email             TEXT,
                plan              TEXT DEFAULT 'free',
                clips_used_today  INTEGER DEFAULT 0,
                reset_date        TEXT DEFAULT '',
                api_key           TEXT,
                stripe_customer_id TEXT
            )
        ''')
        conn.commit()

init_db()

def get_or_create_user(sid):
    with get_db() as conn:
        row = conn.execute('SELECT * FROM users WHERE session_id=?', (sid,)).fetchone()
        if not row:
            conn.execute('INSERT INTO users (session_id) VALUES (?)', (sid,))
            conn.commit()
            row = conn.execute('SELECT * FROM users WHERE session_id=?', (sid,)).fetchone()
        return dict(row)

def reset_daily_if_needed(user, sid):
    today = str(date.today())
    if user['reset_date'] != today:
        with get_db() as conn:
            conn.execute(
                'UPDATE users SET clips_used_today=0, reset_date=? WHERE session_id=?',
                (today, sid)
            )
            conn.commit()
        user['clips_used_today'] = 0
        user['reset_date'] = today
    return user

def get_session_id():
    if 'sid' not in session:
        session['sid'] = str(uuid.uuid4())
    return session['sid']

# ---------------------------------------------------------------------------
# Jobs store (in-memory)
# ---------------------------------------------------------------------------

jobs = {}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

INVIDIOUS = [
    'https://yewtu.be',
    'https://inv.riverside.rocks',
    'https://invidious.nerdvpn.de',
    'https://iv.ggtyler.dev',
]

def is_youtube_url(url):
    return bool(re.match(r'https?://(www\.)?(youtube\.com/watch|youtu\.be/)', url))

def extract_video_id(url):
    m = re.search(r'(?:v=|youtu\.be/)([^&\n?#]+)', url)
    return m.group(1) if m else None

def get_fetch_urls(url):
    """Direct YouTube first, then Invidious fallbacks."""
    vid = extract_video_id(url)
    urls = [url]
    if vid:
        for inst in INVIDIOUS:
            urls.append(f'{inst}/watch?v={vid}')
    return urls

def ydl_extract(urls, opts):
    """Try each URL in sequence; return info dict from first that works."""
    last_err = None
    for u in urls:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(u, download=False)
        except Exception as e:
            last_err = e
    raise last_err

def check_ffmpeg():
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False

def get_ydl_opts(extra=None):
    """Base yt-dlp options — impersonate iOS YouTube app to bypass bot detection."""
    opts = {
        'quiet': True,
        'nocheckcertificate': True,
        'no_warnings': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['ios', 'android', 'tv_embedded'],
            }
        },
        'proxy': '',  # disable any system/env proxy by default
        'cachedir': YT_CACHE_DIR,
    }
    # Use OAuth2 if token is available (no proxy needed)
    if YOUTUBE_OAUTH_TOKEN:
        opts['username'] = 'oauth2'
        opts['password'] = ''
    else:
        # Fall back to proxy if set
        proxy = os.environ.get('PROXY_URL', '').strip()
        if proxy:
            opts['proxy'] = proxy

    # Optionally layer cookies on top if provided
    cookies = os.environ.get('YOUTUBE_COOKIES', '').strip()
    if cookies:
        cookies = cookies.replace('\\t', '\t').replace('\\n', '\n')
        if not cookies.startswith('# Netscape'):
            cookies = '# Netscape HTTP Cookie File\n' + cookies
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8')
        tmp.write(cookies)
        tmp.close()
        opts['cookiefile'] = tmp.name
    if extra:
        opts.update(extra)
    return opts

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return send_file(os.path.join(BASE_DIR, 'youtube_clip_extractor.html'))

@app.route('/pricing')
def pricing():
    return send_file(os.path.join(BASE_DIR, 'pricing.html'))

@app.route('/terms')
def terms():
    return send_file(os.path.join(BASE_DIR, 'terms.html'))

@app.route('/privacy')
def privacy():
    return send_file(os.path.join(BASE_DIR, 'privacy.html'))

@app.route('/refund')
def refund():
    return send_file(os.path.join(BASE_DIR, 'refund.html'))

@app.route('/api/me')
def api_me():
    sid = get_session_id()
    user = get_or_create_user(sid)
    user = reset_daily_if_needed(user, sid)
    plan = user['plan']
    limits = PLAN_LIMITS[plan]
    return jsonify({
        'plan': plan,
        'session_id': sid,
        'clips_used_today': user['clips_used_today'],
        'clips_per_day': limits['clips_per_day'],
        'max_quality': limits['max_quality'],
        'subtitles': limits['subtitles'],
        'api_key': user['api_key'] if plan == 'business' else None,
    })

@app.route('/api/info', methods=['POST'])
def api_info():
    url = request.json.get('url', '').strip()
    if not is_youtube_url(url):
        return jsonify({'ok': False, 'error': 'Not a valid YouTube URL'}), 400
    try:
        info = ydl_extract(get_fetch_urls(url), get_ydl_opts())
        mins = int(info.get('duration', 0) // 60)
        secs = int(info.get('duration', 0) % 60)
        return jsonify({
            'ok': True,
            'title': info.get('title', 'Unknown'),
            'duration': info.get('duration', 0),
            'duration_str': f'{mins}:{secs:02d}',
            'thumbnail': info.get('thumbnail', ''),
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400

@app.route('/api/generate', methods=['POST'])
def api_generate():
    sid = get_session_id()
    user = get_or_create_user(sid)
    user = reset_daily_if_needed(user, sid)

    data    = request.json
    url     = data.get('url', '').strip()
    if not is_youtube_url(url):
        return jsonify({'error': 'Invalid YouTube URL'}), 400

    plan    = user['plan']
    limits  = PLAN_LIMITS[plan]

    num_clips  = min(int(data.get('num_clips', 5)), 20)
    clip_len   = min(int(data.get('clip_len', 30)), 300)
    quality    = int(data.get('quality', 720))
    ai_detect  = bool(data.get('ai_detect', True))
    ratio      = data.get('ratio', '16:9')  # '16:9' or '9:16'

    # Enforce quality ceiling for plan
    if quality > limits['max_quality']:
        quality = limits['max_quality']

    # Enforce daily clip limit
    if limits['clips_per_day'] is not None:
        remaining = limits['clips_per_day'] - user['clips_used_today']
        if remaining <= 0:
            return jsonify({
                'error': 'limit_reached',
                'message': f"You've used all {limits['clips_per_day']} clips today. Upgrade for unlimited access.",
            }), 403
        num_clips = min(num_clips, remaining)

    if not check_ffmpeg():
        return jsonify({'error': 'ffmpeg is not installed on this server. Contact support.'}), 500

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {'status': 'running', 'progress': 0, 'logs': [], 'clips': []}

    t = threading.Thread(
        target=_process,
        args=(job_id, url, num_clips, clip_len, quality, sid, ai_detect, ratio),
        daemon=True,
    )
    t.start()

    return jsonify({'job_id': job_id})

def _moments_pcm(video_path, duration, num_clips, clip_len):
    """Sampling-based audio energy — probes N short clips, never reads full file."""
    import struct
    try:
        usable    = max(duration - clip_len, 1)
        n_samples = min(num_clips * 8, 50)
        sample_times = [int(usable * i / max(n_samples - 1, 1)) for i in range(n_samples)]

        energies = []
        for t in sample_times:
            r = subprocess.run(
                ['ffmpeg', '-ss', str(t), '-i', video_path, '-t', '8',
                 '-vn', '-acodec', 'pcm_s16le', '-ar', '4000', '-ac', '1', '-f', 's16le', '-'],
                capture_output=True, timeout=20,
            )
            raw = r.stdout
            if not raw:
                continue
            n = len(raw) // 2
            samples = struct.unpack(f'{n}h', raw[:n * 2])
            rms = (sum(s * s for s in samples) / max(n, 1)) ** 0.5
            energies.append((t, rms))

        if len(energies) < num_clips:
            return None

        min_gap = max(clip_len + 5, duration // max(num_clips * 2, 1))
        energies.sort(key=lambda x: x[1], reverse=True)
        selected = []
        for t, _ in energies:
            if all(abs(t - s) >= min_gap for s in selected):
                selected.append(t)
            if len(selected) >= num_clips:
                break
        return sorted(selected) if len(selected) >= num_clips else None
    except Exception:
        return None


def _moments_assemblyai(video_path, duration, num_clips, clip_len):
    """Use AssemblyAI word density + highlights to find viral moments (any language)."""
    try:
        base     = 'https://api.assemblyai.com/v2'
        hdrs     = {'authorization': ASSEMBLYAI_API_KEY, 'content-type': 'application/json'}
        hdrs_bin = {'authorization': ASSEMBLYAI_API_KEY}

        # Extract small mono audio
        audio_path = video_path + '_ai.mp3'
        subprocess.run(
            ['ffmpeg', '-i', video_path, '-vn', '-acodec', 'libmp3lame',
             '-ar', '16000', '-ac', '1', '-q:a', '9', '-y', audio_path],
            capture_output=True, timeout=120
        )
        if not os.path.exists(audio_path):
            return None

        # Upload
        with open(audio_path, 'rb') as f:
            up = req_lib.post(base + '/upload', headers=hdrs_bin, data=f, timeout=180)
        try:
            os.unlink(audio_path)
        except Exception:
            pass
        if up.status_code != 200:
            return None
        upload_url = up.json().get('upload_url')
        if not upload_url:
            return None

        # Submit — request word timestamps, highlights, sentiment
        tr = req_lib.post(base + '/transcript', headers=hdrs, json={
            'audio_url':          upload_url,
            'auto_highlights':    True,
            'sentiment_analysis': True,
            'word_boost':         [],
        }, timeout=30)
        if tr.status_code != 200:
            return None
        tid = tr.json().get('id')
        if not tid:
            return None

        # Poll (max 12 min)
        poll = {}
        for _ in range(144):
            time.sleep(5)
            poll = req_lib.get(f'{base}/transcript/{tid}', headers=hdrs, timeout=30).json()
            if poll.get('status') == 'completed':
                break
            if poll.get('status') == 'error':
                return None
        else:
            return None

        usable  = max(duration - clip_len, 1)
        min_gap = max(clip_len + 5, duration // max(num_clips * 2, 1))
        scores  = {}

        # Signal 1: word density per second (works for any language)
        words = poll.get('words', [])
        density = {}
        for w in words:
            t = min(w.get('start', 0) // 1000, usable)
            density[t] = density.get(t, 0) + 1
        if density:
            max_d = max(density.values()) or 1
            for t, cnt in density.items():
                scores[t] = scores.get(t, 0) + (cnt / max_d) * 60

        # Signal 2: auto-highlights (English works best, bonus signal)
        hl = poll.get('auto_highlights_result', {})
        if hl.get('status') == 'success':
            for h in hl.get('results', []):
                for ts in h.get('timestamps', []):
                    t = min(ts['start'] // 1000, usable)
                    scores[t] = scores.get(t, 0) + h.get('rank', 0) * 80

        # Signal 3: emotional sentiment peaks
        for s in poll.get('sentiment_analysis_results', []):
            if s.get('sentiment') != 'NEUTRAL':
                t = min(s.get('start', 0) // 1000, usable)
                scores[t] = scores.get(t, 0) + 25

        if not scores:
            return None

        candidates = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        selected = []
        for t, _ in candidates:
            if t < 0:
                continue
            if all(abs(t - s) >= min_gap for s in selected):
                selected.append(t)
            if len(selected) >= num_clips:
                break

        return sorted(selected) if len(selected) >= num_clips else None

    except Exception:
        return None


VIRAL_WORDS = {
    'secret','shocking','amazing','incredible','insane','unbelievable','never','always',
    'million','billion','died','killed','banned','illegal','exposed','truth','lie',
    'warning','dangerous','most','best','worst','first','last','only','ever',
    'why','how','actually','literally','love','hate','fear','money','success',
    'fail','win','lose','rich','poor','viral','crazy','huge','massive','broke',
}

def _moments_from_captions(info, duration, num_clips, clip_len):
    """Use youtube-transcript-api to get timestamped transcript and find viral moments."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled

        vid = extract_video_id(info.get('webpage_url', '') or info.get('original_url', ''))
        if not vid:
            return None

        # Try to get transcript in any available language
        try:
            transcript_list = YouTubeTranscriptApi.list_transcripts(vid)
            transcript = None
            # Prefer manually created, then auto-generated
            try:
                transcript = transcript_list.find_manually_created_transcript(
                    transcript_list._manually_created_transcripts.keys()
                ).fetch()
            except Exception:
                pass
            if not transcript:
                try:
                    transcript = transcript_list.find_generated_transcript(
                        transcript_list._generated_transcripts.keys()
                    ).fetch()
                except Exception:
                    pass
            if not transcript:
                return None
        except (NoTranscriptFound, TranscriptsDisabled):
            return None

        # Score each second by viral potential
        sec_scores = {}
        for seg in transcript:
            start = seg.get('start', 0)
            text  = seg.get('text', '')
            score = (text.count('!') * 5
                     + text.count('?') * 3
                     + len(re.findall(r'\b\d+\b', text)) * 2
                     + sum(8 for w in VIRAL_WORDS if w in text.lower()))
            sec = int(start)
            sec_scores[sec] = sec_scores.get(sec, 0) + score

        if not sec_scores:
            return None

        usable = max(duration - clip_len, 1)
        window_scores = {t: sum(sec_scores.get(s, 0) for s in range(t, t + clip_len))
                         for t in range(0, int(usable))}

        min_gap  = max(clip_len + 5, duration // max(num_clips * 2, 1))
        candidates = sorted(window_scores.items(), key=lambda x: x[1], reverse=True)
        selected = []
        for t, score in candidates:
            if score == 0:
                break
            if all(abs(t - s) >= min_gap for s in selected):
                selected.append(t)
            if len(selected) >= num_clips:
                break
        return sorted(selected) if len(selected) >= num_clips else None
    except Exception:
        return None


def find_best_moments(video_path, duration, num_clips, clip_len):
    """AssemblyAI for short videos (≤15 min); fast local PCM for everything else."""
    if ASSEMBLYAI_API_KEY and duration <= 900:
        result = _moments_assemblyai(video_path, duration, num_clips, clip_len)
        if result:
            return result
    return _moments_pcm(video_path, duration, num_clips, clip_len)


def _process(job_id, url, num_clips, clip_len, quality, sid, ai_detect=True, ratio='16:9'):
    job     = jobs[job_id]
    job_dir = os.path.join(CLIPS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    def log(msg, pct):
        job['logs'].append(msg)
        job['progress'] = pct

    try:
        log('Fetching video info...', 5)

        fmt = f'best[height<={quality}]/best[height<=720]/best'

        # Step 1: get metadata via proxy
        info     = ydl_extract(get_fetch_urls(url), get_ydl_opts())
        duration = int(info.get('duration', 0))
        title    = info.get('title', 'clip')
        log(f'Found "{title}" ({duration//60}:{duration%60:02d}).', 15)

        # Step 2: AI moment detection
        starts = None
        if ai_detect and num_clips > 1:
            # Try captions first — free, fast, works for ANY video length
            log('Analyzing captions for viral moments...', 18)
            starts = _moments_from_captions(info, duration, num_clips, clip_len)
            if starts:
                log(f'AI found {len(starts)} viral moments: {", ".join(f"{s//60}:{s%60:02d}" for s in starts)}', 30)
            else:
                # Fallback: AssemblyAI audio analysis (short videos only)
                if ASSEMBLYAI_API_KEY and duration <= 900:
                    log('Downloading audio for deep AI analysis...', 20)
                    audio_tmpl = os.path.join(job_dir, 'audio.%(ext)s')
                    try:
                        with yt_dlp.YoutubeDL(get_ydl_opts({
                            'format': 'best[height<=144]/worst/best',
                            'outtmpl': audio_tmpl,
                            'socket_timeout': 60,
                            'retries': 2,
                        })) as ydl:
                            ydl.download([url])
                        audio_path = next(
                            (os.path.join(job_dir, f) for f in os.listdir(job_dir) if f.startswith('audio.')),
                            None
                        )
                        if audio_path:
                            log('AI analyzing speech and emotion...', 25)
                            starts = _moments_assemblyai(audio_path, duration, num_clips, clip_len)
                            if starts:
                                log(f'AI found {len(starts)} moments: {", ".join(f"{s//60}:{s%60:02d}" for s in starts)}', 32)
                            try:
                                os.unlink(audio_path)
                            except Exception:
                                pass
                    except Exception as e:
                        log(f'Audio analysis skipped — {e}', 25)
                if not starts:
                    log('Using optimized distribution...', 25)

        # Fallback: evenly spaced timestamps
        if not starts:
            margin     = max(int(duration * 0.05), 10)
            safe_start = margin
            safe_end   = max(duration - clip_len - margin, safe_start + clip_len)
            safe_range = safe_end - safe_start
            starts = [safe_start] if num_clips == 1 else [
                safe_start + int(safe_range * i / (num_clips - 1)) for i in range(num_clips)
            ]

        # Step 3: download only the specific sections — not the full video
        clips = []
        vf_filter = (
            'crop=ih*9/16:ih' if ratio == '9:16' else
            'crop=ih:ih'      if ratio == '1:1'  else
            None
        )
        for i, start in enumerate(starts):
            pct       = 20 + int((i + 1) / num_clips * 75)
            clip_name = f'clip_{i+1:02d}.mp4'
            clip_path = os.path.join(job_dir, clip_name)
            raw_tmpl  = os.path.join(job_dir, f'raw_{i+1:02d}.%(ext)s')
            log(f'Downloading clip {i+1}/{num_clips} ({start//60}:{start%60:02d} – {(start+clip_len)//60}:{(start+clip_len)%60:02d})...', pct)

            dl_opts = get_ydl_opts({
                'format': fmt,
                'outtmpl': raw_tmpl,
                'download_ranges': yt_dlp.utils.download_range_func(None, [(start, start + clip_len)]),
                'force_keyframes_at_cuts': True,
                'socket_timeout': 60,
                'retries': 3,
            })
            try:
                with yt_dlp.YoutubeDL(dl_opts) as ydl:
                    ydl.download([url])
            except Exception as e:
                log(f'Warning: clip {i+1} download failed — {e}', pct)
                continue

            # Find the downloaded raw file
            raw_path = None
            for f in os.listdir(job_dir):
                if f.startswith(f'raw_{i+1:02d}.') and not f.endswith('.part'):
                    raw_path = os.path.join(job_dir, f)
                    break

            if not raw_path or not os.path.exists(raw_path):
                log(f'Warning: clip {i+1} file not found after download', pct)
                continue

            # Re-encode with ffmpeg (apply crop if needed, ensure mp4)
            cmd = ['ffmpeg', '-i', raw_path, '-t', str(clip_len)]
            if vf_filter:
                cmd += ['-vf', vf_filter]
            cmd += ['-c:v', 'libx264', '-c:a', 'aac', '-movflags', '+faststart', '-y', clip_path]
            subprocess.run(cmd, capture_output=True)
            try:
                os.unlink(raw_path)
            except Exception:
                pass

            if os.path.exists(clip_path) and os.path.getsize(clip_path) > 0:
                size_mb = round(os.path.getsize(clip_path) / 1024 / 1024, 1)
                clips.append({
                    'name':     clip_name,
                    'url':      f'/clips/{job_id}/{clip_name}',
                    'start':    start,
                    'duration': clip_len,
                    'size_mb':  size_mb,
                })
            else:
                log(f'Warning: clip {i+1} encode failed', pct)

        if not clips:
            raise RuntimeError('No clips produced. Check server logs.')

        # Record usage
        with get_db() as conn:
            conn.execute(
                'UPDATE users SET clips_used_today=clips_used_today+? WHERE session_id=?',
                (len(clips), sid),
            )
            conn.commit()

        job['clips']    = clips
        job['status']   = 'done'
        job['progress'] = 100
        log(f'Done! {len(clips)} clips ready to download.', 100)

    except Exception as e:
        job['status'] = 'error'
        job['error']  = str(e)
        job['logs'].append(f'Error: {e}')

@app.route('/api/status/<job_id>')
def api_status(job_id):
    if job_id not in jobs:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(jobs[job_id])

@app.route('/clips/<job_id>/<filename>')
def serve_clip(job_id, filename):
    if '..' in job_id or '..' in filename or '/' in filename or '\\' in filename:
        return 'Forbidden', 403
    path = os.path.join(CLIPS_DIR, job_id, filename)
    if not os.path.exists(path):
        return 'Not found', 404
    return send_file(path, as_attachment=True, download_name=filename)

@app.route('/api/zip/<job_id>')
def api_zip(job_id):
    if job_id not in jobs:
        return 'Not found', 404
    clips = jobs[job_id].get('clips', [])
    if not clips:
        return 'No clips yet', 404
    buf = BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for clip in clips:
            p = os.path.join(CLIPS_DIR, job_id, clip['name'])
            if os.path.exists(p):
                zf.write(p, clip['name'])
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='clips.zip', mimetype='application/zip')

# ---------------------------------------------------------------------------
# Paddle
# ---------------------------------------------------------------------------

PADDLE_PRICE_TO_PLAN = {}

def _build_price_map():
    for pid, plan in [
        (PADDLE_PRO_PRICE_ID,            'pro'),
        (PADDLE_PRO_YEARLY_PRICE_ID,     'pro'),
        (PADDLE_CREATOR_PRICE_ID,        'creator'),
        (PADDLE_CREATOR_YEARLY_PRICE_ID, 'creator'),
        (PADDLE_BIZ_PRICE_ID,            'business'),
        (PADDLE_BIZ_YEARLY_PRICE_ID,     'business'),
    ]:
        if pid:
            PADDLE_PRICE_TO_PLAN[pid] = plan

_build_price_map()

@app.route('/api/paddle-config')
def paddle_config():
    sid = get_session_id()
    return jsonify({
        'client_token': PADDLE_CLIENT_TOKEN,
        'session_id': sid,
        'prices': {
            'pro':      {'monthly': PADDLE_PRO_PRICE_ID,      'yearly': PADDLE_PRO_YEARLY_PRICE_ID},
            'creator':  {'monthly': PADDLE_CREATOR_PRICE_ID,  'yearly': PADDLE_CREATOR_YEARLY_PRICE_ID},
            'business': {'monthly': PADDLE_BIZ_PRICE_ID,      'yearly': PADDLE_BIZ_YEARLY_PRICE_ID},
        },
    })

def _verify_paddle_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    try:
        parts = dict(p.split('=', 1) for p in signature.split(';'))
        ts = parts.get('ts', '')
        h1 = parts.get('h1', '')
        signed = f"{ts}:{raw_body.decode('utf-8')}"
        expected = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(h1, expected)
    except Exception:
        return False

@app.route('/api/paddle-webhook', methods=['POST'])
def paddle_webhook():
    raw_body  = request.get_data()
    signature = request.headers.get('Paddle-Signature', '')

    if PADDLE_WEBHOOK_SECRET and not _verify_paddle_signature(raw_body, signature, PADDLE_WEBHOOK_SECRET):
        return jsonify({'error': 'Invalid signature'}), 401

    try:
        event      = json.loads(raw_body)
        event_type = event.get('event_type', '')
        data       = event.get('data', {})
    except Exception:
        return jsonify({'error': 'Bad JSON'}), 400

    if event_type in ('subscription.activated', 'subscription.created'):
        custom_data = data.get('custom_data') or {}
        sid         = custom_data.get('session_id')
        email       = (data.get('customer') or {}).get('email', '')
        customer_id = data.get('customer_id', '')

        price_id = ''
        try:
            price_id = data['items'][0]['price']['id']
        except (KeyError, IndexError):
            pass

        plan    = PADDLE_PRICE_TO_PLAN.get(price_id, 'pro')
        api_key = str(uuid.uuid4()).replace('-', '') if plan == 'business' else None

        if sid:
            with get_db() as conn:
                conn.execute(
                    'UPDATE users SET plan=?, email=?, stripe_customer_id=?, api_key=? WHERE session_id=?',
                    (plan, email, customer_id, api_key, sid),
                )
                conn.commit()

    elif event_type in ('subscription.canceled', 'subscription.cancelled'):
        customer_id = data.get('customer_id', '')
        if customer_id:
            with get_db() as conn:
                conn.execute(
                    "UPDATE users SET plan='free', api_key=NULL WHERE stripe_customer_id=?",
                    (customer_id,),
                )
                conn.commit()

    return jsonify({'ok': True})

# ---------------------------------------------------------------------------

if __name__ == '__main__':
    print('\n  ClipCut running -> http://localhost:5000\n')
    app.run(debug=False, port=5000)
