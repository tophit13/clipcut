import os, uuid, threading, subprocess, zipfile, re, sqlite3, json, tempfile
from datetime import date
from io import BytesIO
from flask import Flask, request, jsonify, send_file, session
from flask_cors import CORS
import yt_dlp
import stripe

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-clipcut-2024')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CORS(app, supports_credentials=True)

stripe.api_key              = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_WEBHOOK_SECRET       = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
STRIPE_PRO_PRICE_ID         = os.environ.get('STRIPE_PRO_PRICE_ID', '')
STRIPE_BIZ_PRICE_ID         = os.environ.get('STRIPE_BIZ_PRICE_ID', '')

CLIPS_DIR = 'clips'
os.makedirs(CLIPS_DIR, exist_ok=True)

DB_PATH = 'clipcut.db'

PLAN_LIMITS = {
    'free':     {'clips_per_day': 3,    'max_quality': 720,  'subtitles': False},
    'pro':      {'clips_per_day': None, 'max_quality': 1080, 'subtitles': True},
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

def is_youtube_url(url):
    return bool(re.match(r'https?://(www\.)?(youtube\.com/watch|youtu\.be/)', url))

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
    }
    # Optional proxy (set PROXY_URL=http://user:pass@host:port in env)
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

@app.route('/api/me')
def api_me():
    sid = get_session_id()
    user = get_or_create_user(sid)
    user = reset_daily_if_needed(user, sid)
    plan = user['plan']
    limits = PLAN_LIMITS[plan]
    return jsonify({
        'plan': plan,
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
        with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
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

    num_clips = min(int(data.get('num_clips', 5)), 20)
    clip_len  = min(int(data.get('clip_len', 30)), 300)
    quality   = int(data.get('quality', 720))

    # Enforce quality ceiling for plan
    if quality > limits['max_quality']:
        quality = limits['max_quality']

    # Enforce daily clip limit
    if limits['clips_per_day'] is not None:
        remaining = limits['clips_per_day'] - user['clips_used_today']
        if remaining <= 0:
            return jsonify({
                'error': 'limit_reached',
                'message': f"You've used all {limits['clips_per_day']} free clips today. Upgrade for unlimited access.",
            }), 403
        num_clips = min(num_clips, remaining)

    if not check_ffmpeg():
        return jsonify({'error': 'ffmpeg is not installed on this server. Contact support.'}), 500

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {'status': 'running', 'progress': 0, 'logs': [], 'clips': []}

    t = threading.Thread(
        target=_process,
        args=(job_id, url, num_clips, clip_len, quality, sid),
        daemon=True,
    )
    t.start()

    return jsonify({'job_id': job_id})

def _process(job_id, url, num_clips, clip_len, quality, sid):
    job     = jobs[job_id]
    job_dir = os.path.join(CLIPS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    def log(msg, pct):
        job['logs'].append(msg)
        job['progress'] = pct

    try:
        log('Downloading video from YouTube...', 5)

        fmt = (
            f'bestvideo[height<={quality}]+bestaudio'
            f'/best[height<={quality}]'
            f'/bestvideo+bestaudio'
            f'/best'
        )
        video_tmpl = os.path.join(job_dir, 'video.%(ext)s')
        ydl_opts = get_ydl_opts({
            'format': fmt,
            'outtmpl': video_tmpl,
            'merge_output_format': 'mp4',
        })

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        video_path = None
        for f in os.listdir(job_dir):
            if f.startswith('video.'):
                video_path = os.path.join(job_dir, f)
                break

        if not video_path or not os.path.exists(video_path):
            raise FileNotFoundError('Download failed — video file not found.')

        duration = int(info.get('duration', 0))
        title    = info.get('title', 'clip')
        log(f'Downloaded "{title}" ({duration//60}:{duration%60:02d}). Cutting clips...', 40)

        usable = max(duration - clip_len, 1)
        starts = [0] if num_clips == 1 else [
            int(usable * i / (num_clips - 1)) for i in range(num_clips)
        ]

        clips = []
        for i, start in enumerate(starts):
            pct       = 40 + int((i + 1) / num_clips * 55)
            clip_name = f'clip_{i+1:02d}.mp4'
            clip_path = os.path.join(job_dir, clip_name)
            log(f'Cutting clip {i+1}/{num_clips} (starts {start//60}:{start%60:02d})...', pct)

            cmd = [
                'ffmpeg', '-ss', str(start), '-i', video_path,
                '-t', str(clip_len),
                '-c:v', 'libx264', '-c:a', 'aac',
                '-movflags', '+faststart',
                '-y', clip_path,
            ]
            result = subprocess.run(cmd, capture_output=True)

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
                stderr = result.stderr.decode(errors='ignore')[:300]
                log(f'Warning: clip {i+1} failed — {stderr}', pct)

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
# Stripe
# ---------------------------------------------------------------------------

@app.route('/api/create-checkout-session', methods=['POST'])
def create_checkout_session():
    if not stripe.api_key:
        return jsonify({'error': 'Payments not configured yet'}), 500

    sid     = get_session_id()
    plan    = request.json.get('plan', 'pro')
    price_id = STRIPE_PRO_PRICE_ID if plan == 'pro' else STRIPE_BIZ_PRICE_ID

    if not price_id:
        return jsonify({'error': 'Price not configured'}), 500

    try:
        checkout = stripe.checkout.Session.create(
            payment_method_types=['card'],
            mode='subscription',
            line_items=[{'price': price_id, 'quantity': 1}],
            success_url=request.host_url + '?upgraded=1',
            cancel_url=request.host_url + 'pricing',
            metadata={'session_id': sid},
        )
        return jsonify({'url': checkout.url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data()
    sig     = request.headers.get('Stripe-Signature', '')

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    if event['type'] == 'checkout.session.completed':
        cs          = event['data']['object']
        sid         = cs.get('metadata', {}).get('session_id')
        email       = cs.get('customer_details', {}).get('email')
        customer_id = cs.get('customer')

        plan = 'pro'
        try:
            sub      = stripe.Subscription.retrieve(cs['subscription'])
            price_id = sub['items']['data'][0]['price']['id']
            if price_id == STRIPE_BIZ_PRICE_ID:
                plan = 'business'
        except Exception:
            pass

        api_key = str(uuid.uuid4()).replace('-', '') if plan == 'business' else None

        if sid:
            with get_db() as conn:
                conn.execute(
                    'UPDATE users SET plan=?, email=?, stripe_customer_id=?, api_key=? WHERE session_id=?',
                    (plan, email, customer_id, api_key, sid),
                )
                conn.commit()

    elif event['type'] == 'customer.subscription.deleted':
        customer_id = event['data']['object']['customer']
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
