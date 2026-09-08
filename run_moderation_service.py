"""
Standalone Forum Content Moderation Service & Interactive Web UI (No Database Required!)
Runs a self-contained web server on http://localhost:8000.

Features:
- Live Text Moderation: Keywords, Leetspeak, Detoxify, Zero-shot BART-large-MNLI
- Live Image Moderation: Connects to nsfw-service (http://localhost:5001)
- Complete Admin Workflow:
    * Clean post -> Automatically 'active' (Published)
    * Illegal / Borderline post -> Held in 'pending' (Waiting Admin Approval)
    * Admin Panel -> Displays pending posts with reason and allows 'Approve' or 'Reject'
"""

import http.server
import socketserver
import json
import re
import urllib.parse
import urllib.request
import base64
import time

PORT = 8000

# -------------------------------------------------------------
# 1. MODERATION CONFIG & RULES
# -------------------------------------------------------------
ILLEGAL_TEXT_KEYWORDS = (
    'unlicensed firearm', 'weapon sale', 'drug sale', 'counterfeit',
    'trafficking', 'stolen goods', 'hacking service', 'fake id',
    'selling a gun', 'selling gun', 'gun for sale', 'guns for sale',
    'no license needed', 'no license required', 'no background check',
    'selling firearm', 'firearm for sale', 'illegal weapon',
    'selling a pistol', 'selling a rifle', 'unregistered gun',
    'cash only no questions', 'no paperwork needed',
    'prohibited weapon', 'weapon transaction', 'weapon deal',
    'weapon exchange', 'arrange a weapon', 'illegal firearm',
    'black market weapon', 'restricted weapon',
    'prohibited goods', 'prohibited item', 'illegal goods',
    'banned item', 'restricted goods', 'contraband',
)

LEET_SUBSTITUTIONS = {
    '0': 'o', '1': 'i', '!': 'i', '3': 'e', '4': 'a', '@': 'a',
    '5': 's', '$': 's', '7': 't', '+': 't', '8': 'b', 'v': 'u'
}

HARMFUL_IMAGE_CATEGORIES = ('Porn', 'Hentai', 'Sexy')

# Try loading heavy ML models if available, fallback gracefully
_text_model = None
_illegal_classifier = None

try:
    from detoxify import Detoxify
    print("[INIT] Loading Detoxify model...")
    _text_model = Detoxify('original')
    print("[INIT] Detoxify loaded successfully.")
except Exception as e:
    print(f"[WARN] Detoxify model not loaded ({e}). Rule-based checks active.")

try:
    from transformers import pipeline
    print("[INIT] Loading Zero-Shot Classifier (facebook/bart-large-mnli)...")
    _illegal_classifier = pipeline("zero-shot-classification", model="facebook/bart-large-mnli")
    print("[INIT] Zero-Shot Classifier loaded successfully.")
except Exception as e:
    print(f"[WARN] Transformers model not loaded ({e}). Rule-based checks active.")

# In-memory store for posts & admin notifications
posts_db = []
notifications_db = []
post_counter = 1

def normalize_text(text):
    text = re.sub(r'(.)\1{2,}', r'\1\1', text)
    lowered = text.lower()
    for k, v in LEET_SUBSTITUTIONS.items():
        lowered = lowered.replace(k, v)
    return re.sub(r'\s{2,}', ' ', lowered)

def check_text_moderation(text):
    text = (text or '').strip()
    if not text:
        return None, None

    # 1. Keyword + Leetspeak Check
    lowered = text.lower()
    normalized = normalize_text(text)
    matched = [kw for kw in ILLEGAL_TEXT_KEYWORDS if kw in lowered or kw in normalized]
    if matched:
        return 'block', f"Text flagged as illegal content (matched: {', '.join(matched)})"

    # 2. Semantic Zero-Shot Classifier Check
    if _illegal_classifier:
        try:
            candidate_labels = [
                "illegal weapon sale", "drug sale or trafficking", "counterfeit goods sale",
                "hacking or cybercrime service", "human trafficking", "stolen goods sale",
                "sale or arrangement of illegal items", "normal conversation"
            ]
            res = _illegal_classifier(text, candidate_labels=candidate_labels)
            top_label, top_score = res['labels'][0], res['scores'][0]
            if top_label != "normal conversation" and top_score >= 0.65:
                return 'block', f"Text flagged as illegal content ({top_label}, confidence: {top_score:.2f})"
        except Exception as e:
            print(f"[ERROR] Zero-shot classification failed: {e}")

    # 3. Detoxify Toxicity Check
    if _text_model:
        try:
            scores = _text_model.predict(text[:800])
            worst_cat, worst_score = max(scores.items(), key=lambda kv: kv[1])
            if worst_score >= 0.85:
                return 'block', f"Text flagged for {worst_cat} (score: {worst_score:.2f})"
            elif worst_score >= 0.70:
                return 'review', f"Text borderline for {worst_cat} (score: {worst_score:.2f})"
        except Exception as e:
            print(f"[ERROR] Toxicity check failed: {e}")

    return None, None

def check_image_moderation(image_b64):
    if not image_b64:
        return None, None
    try:
        image_bytes = base64.b64decode(image_b64.split(',')[-1])
        # Call nsfw-service on port 5001
        boundary = '----WebKitFormBoundary7MA4YWxkTrZu0gW'
        body = (
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="image"; filename="upload.png"\r\n'
            f'Content-Type: image/png\r\n\r\n'
        ).encode('latin-1') + image_bytes + f'\r\n--{boundary}--\r\n'.encode('latin-1')

        req = urllib.request.Request(
            'http://localhost:5001/check-image',
            data=body,
            headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            predictions = data.get('predictions', [])
            probs = {p['className']: p['probability'] for p in predictions}
            harmful = {k: v for k, v in probs.items() if k in HARMFUL_IMAGE_CATEGORIES}
            if harmful:
                worst_cat = max(harmful, key=harmful.get)
                worst_score = harmful[worst_cat]
                if worst_score >= 0.75:
                    return 'block', f"Image flagged for {worst_cat} (score: {worst_score:.2f})"
                elif worst_score >= 0.60:
                    return 'review', f"Image borderline for {worst_cat} (score: {worst_score:.2f})"
    except Exception as e:
        print(f"[WARN] NSFW microservice check skipped (ensure nsfw-service is running on 5001): {e}")

    return None, None

# -------------------------------------------------------------
# 2. HTTP REQUEST HANDLER
# -------------------------------------------------------------
class ModerationRequestHandler(http.server.BaseHTTPRequestHandler):

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        global posts_db, notifications_db

        if self.path == '/api/posts':
            self.send_json({'posts': posts_db, 'notifications': notifications_db})
            return

        if self.path == '/' or self.path.startswith('/index'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode('utf-8'))
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        global posts_db, notifications_db, post_counter

        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        payload = {}
        if body:
            try:
                payload = json.loads(body.decode('utf-8'))
            except Exception:
                pass

        if self.path == '/api/check-text':
            text = payload.get('text', '')
            level, reason = check_text_moderation(text)
            self.send_json({'status': 'ok', 'level': level or 'clean', 'reason': reason or 'Clean content'})
            return

        if self.path == '/api/submit-post':
            title = payload.get('title', '').strip()
            content = payload.get('content', '').strip()
            image_b64 = payload.get('image_base64', '').strip()
            author = payload.get('author', 'User1')

            full_text = f"{title}. {content}".strip()
            level, reason = check_text_moderation(full_text)

            if not level and image_b64:
                img_level, img_reason = check_image_moderation(image_b64)
                if img_level:
                    level, reason = img_level, img_reason

            # State decision
            if level in ('block', 'review'):
                state = 'pending'
                is_published = False
                notif_text = f"🚨 Moderation Alert: Post '{title}' held for admin approval. Reason: {reason}"
                notifications_db.append({'time': time.strftime('%X'), 'message': notif_text, 'post_id': post_counter})
            else:
                state = 'active'
                is_published = True

            new_post = {
                'id': post_counter,
                'title': title,
                'content': content,
                'has_image': bool(image_b64),
                'author': author,
                'state': state,
                'moderation_reason': reason,
                'created_at': time.strftime('%Y-%m-%d %H:%M:%S')
            }
            posts_db.insert(0, new_post)
            post_counter += 1

            self.send_json({
                'status': 'ok',
                'post': new_post,
                'is_published': is_published,
                'notice': 'Your post was submitted and is pending administrator approval before it becomes visible.' if not is_published else 'Post published successfully!'
            })
            return

        if self.path == '/api/admin/approve':
            post_id = payload.get('post_id')
            for p in posts_db:
                if p['id'] == post_id:
                    p['state'] = 'active'
                    notifications_db.append({'time': time.strftime('%X'), 'message': f"✅ Admin approved post #{post_id} ('{p['title']}'). Post is now active & published!"})
                    break
            self.send_json({'status': 'ok'})
            return

        if self.path == '/api/admin/reject':
            post_id = payload.get('post_id')
            for p in posts_db:
                if p['id'] == post_id:
                    p['state'] = 'offensive'
                    notifications_db.append({'time': time.strftime('%X'), 'message': f"❌ Admin rejected post #{post_id} ('{p['title']}'). Post is permanently blocked."})
                    break
            self.send_json({'status': 'ok'})
            return

        self.send_response(404)
        self.end_headers()

# -------------------------------------------------------------
# 3. INTERACTIVE WEB UI (HTML/JS)
# -------------------------------------------------------------
HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Forum Content Moderation & Admin Approval</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { background-color: #f4f6f9; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        .card { border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); margin-bottom: 20px; }
        .badge-pending { background-color: #ffc107; color: #212529; font-weight: 600; }
        .badge-active { background-color: #28a745; color: #fff; font-weight: 600; }
        .badge-offensive { background-color: #dc3545; color: #fff; font-weight: 600; }
        .notif-box { max-height: 220px; overflow-y: auto; background: #fffdf5; border-left: 4px solid #ffc107; padding: 10px; border-radius: 4px; }
    </style>
</head>
<body class="p-4">
<div class="container-fluid">
    <div class="row mb-4">
        <div class="col-12">
            <h2 class="fw-bold">🛡️ Forum Content Moderation System</h2>
            <p class="text-muted">Test real-time content classification (Detoxify, BART Zero-Shot, NSFW) and the Admin Approval Workflow.</p>
        </div>
    </div>

    <div class="row">
        <!-- USER FORUM SUBMISSION COLUMN -->
        <div class="col-md-5">
            <div class="card p-4">
                <h4 class="card-title fw-bold mb-3">📝 Create Forum Post</h4>
                
                <div class="mb-3">
                    <label class="form-label fw-semibold">Author</label>
                    <input type="text" id="postAuthor" class="form-control" value="CommunityUser">
                </div>

                <div class="mb-3">
                    <label class="form-label fw-semibold">Post Title</label>
                    <input type="text" id="postTitle" class="form-control" placeholder="e.g. Unlicensed firearm for sale or clean question">
                </div>

                <div class="mb-3">
                    <label class="form-label fw-semibold">Post Content</label>
                    <textarea id="postContent" class="form-control" rows="3" placeholder="Write question details..."></textarea>
                </div>

                <div class="mb-3">
                    <label class="form-label fw-semibold">Attach Image (Optional)</label>
                    <input type="file" id="postImage" class="form-control" accept="image/*">
                </div>

                <div class="d-flex gap-2">
                    <button class="btn btn-primary px-4" onclick="submitPost()">Submit Post</button>
                    <button class="btn btn-outline-secondary" onclick="fillIllegalSample()">Sample: Illegal Post</button>
                    <button class="btn btn-outline-secondary" onclick="fillCleanSample()">Sample: Clean Post</button>
                </div>

                <div id="userNotice" class="mt-3"></div>
            </div>

            <!-- ADMIN LIVE ALERTS -->
            <div class="card p-3">
                <h5 class="fw-bold text-danger">🔔 Admin Notification Log</h5>
                <div id="notifList" class="notif-box small">
                    <span class="text-muted">No notifications yet. Flagged posts will trigger admin alerts here.</span>
                </div>
            </div>
        </div>

        <!-- MODERATION & ADMIN REVIEW COLUMN -->
        <div class="col-md-7">
            <!-- PENDING APPROVAL QUEUE -->
            <div class="card p-4 border-warning">
                <div class="d-flex justify-content-between align-items-center mb-3">
                    <h4 class="card-title fw-bold text-warning mb-0">⏳ Admin Moderation Queue (Pending Approval)</h4>
                    <span id="pendingCount" class="badge bg-warning text-dark fs-6">0 Pending</span>
                </div>
                <p class="small text-muted">Posts flagged as illegal/prohibited are held here. They are <b>NOT visible</b> on the public forum until you approve them.</p>
                <div id="pendingPostsList"></div>
            </div>

            <!-- PUBLISHED FORUM FEED -->
            <div class="card p-4">
                <h4 class="card-title fw-bold text-success mb-3">🌐 Published Forum Posts (Active Feed)</h4>
                <div id="activePostsList"></div>
            </div>
        </div>
    </div>
</div>

<script>
let imageBase64 = "";

document.getElementById('postImage').addEventListener('change', function(e) {
    const file = e.target.files[0];
    if (file) {
        const reader = new FileReader();
        reader.onload = function(evt) { imageBase64 = evt.target.result; };
        reader.readAsDataURL(file);
    } else { imageBase64 = ""; }
});

function fillIllegalSample() {
    document.getElementById('postTitle').value = "Unlicensed firearm available";
    document.getElementById('postContent').value = "Selling rifle no license needed cash only message me";
}

function fillCleanSample() {
    document.getElementById('postTitle').value = "How to build a machine learning model?";
    document.getElementById('postContent').value = "I am learning Python and Transformers. What are best practices?";
}

async function submitPost() {
    const title = document.getElementById('postTitle').value;
    const content = document.getElementById('postContent').value;
    const author = document.getElementById('postAuthor').value;
    const noticeDiv = document.getElementById('userNotice');

    if (!title) { alert("Please provide a title"); return; }

    const res = await fetch('/api/submit-post', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ title, content, author, image_base64: imageBase64 })
    });
    const data = await res.json();

    if (data.is_published) {
        noticeDiv.innerHTML = `<div class="alert alert-success">${data.notice}</div>`;
    } else {
        noticeDiv.innerHTML = `<div class="alert alert-warning">${data.notice}<br/><small class="text-danger">Reason: ${data.post.moderation_reason}</small></div>`;
    }

    // Reset fields
    document.getElementById('postTitle').value = "";
    document.getElementById('postContent').value = "";
    document.getElementById('postImage').value = "";
    imageBase64 = "";

    loadPosts();
}

async function approvePost(id) {
    await fetch('/api/admin/approve', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ post_id: id })
    });
    loadPosts();
}

async function rejectPost(id) {
    await fetch('/api/admin/reject', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ post_id: id })
    });
    loadPosts();
}

async function loadPosts() {
    const res = await fetch('/api/posts');
    const data = await res.json();

    // Render Notifications
    const notifDiv = document.getElementById('notifList');
    if (data.notifications && data.notifications.length > 0) {
        notifDiv.innerHTML = data.notifications.slice().reverse().map(n => 
            `<div class="mb-1"><strong>[${n.time}]</strong> ${n.message}</div>`
        ).join('');
    }

    // Filter Posts
    const pending = data.posts.filter(p => p.state === 'pending');
    const active = data.posts.filter(p => p.state === 'active');

    document.getElementById('pendingCount').innerText = `${pending.length} Pending`;

    // Render Pending
    const pendingList = document.getElementById('pendingPostsList');
    if (pending.length === 0) {
        pendingList.innerHTML = '<div class="text-muted p-2">No posts waiting for approval. Everything is clear!</div>';
    } else {
        pendingList.innerHTML = pending.map(p => `
            <div class="border rounded p-3 mb-2 bg-light">
                <div class="d-flex justify-content-between align-items-start">
                    <div>
                        <span class="badge badge-pending">Waiting Approval</span>
                        <h5 class="fw-bold mt-1 mb-1">${p.title}</h5>
                        <p class="mb-1 text-secondary">${p.content}</p>
                        <p class="mb-1 text-danger small"><strong>Flagged Reason:</strong> ${p.moderation_reason}</p>
                        <small class="text-muted">Author: ${p.author} | Created: ${p.created_at}</small>
                    </div>
                    <div class="d-flex gap-2">
                        <button class="btn btn-success btn-sm px-3" onclick="approvePost(${p.id})">✓ Approve & Post</button>
                        <button class="btn btn-danger btn-sm px-3" onclick="rejectPost(${p.id})">✗ Reject</button>
                    </div>
                </div>
            </div>
        `).join('');
    }

    // Render Active
    const activeList = document.getElementById('activePostsList');
    if (active.length === 0) {
        activeList.innerHTML = '<div class="text-muted p-2">No published posts yet.</div>';
    } else {
        activeList.innerHTML = active.map(p => `
            <div class="border rounded p-3 mb-2 bg-white">
                <span class="badge badge-active mb-1">Published</span>
                <h5 class="fw-bold mb-1">${p.title}</h5>
                <p class="mb-1 text-dark">${p.content}</p>
                <small class="text-muted">By ${p.author} at ${p.created_at}</small>
            </div>
        `).join('');
    }
}

// Initial load & poll every 3s
loadPosts();
setInterval(loadPosts, 3000);
</script>
</body>
</html>
"""

if __name__ == '__main__':
    with socketserver.TCPServer(("", PORT), ModerationRequestHandler) as httpd:
        print(f"\n=======================================================")
        print(f"🚀 Forum Content Moderation Service Started!")
        print(f"👉 Open in your browser: http://localhost:{PORT}")
        print(f"=======================================================\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server.")
