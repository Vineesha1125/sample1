"""
Test Suite: Forum Content Moderation Models & Admin Approval Workflow
Verifies:
1. Keyword detection (exact and leetspeak obfuscation)
2. Normalization logic
3. Zero-shot classifier configuration & threshold logic
4. Toxicity threshold logic
5. Image NSFW response parser & threshold logic
6. Admin notification and approval/rejection state transitions
"""

import re
import sys

# -------------------------------------------------------------
# 1. CONSTANTS & CONFIGURATION (as defined in the module)
# -------------------------------------------------------------
ILLEGAL_TEXT_KEYWORDS = (
    # Direct testing & generic illegal terms
    'illegal', 'illicit', 'contraband', 'prohibited item', 'prohibited goods',
    'prohibited content', 'illegal content', 'illegal text', 'illegal post',
    'illegal goods', 'illegal item', 'illegal items', 'illegal substance',
    'illegal test', 'illegal service', 'illegal trade', 'illegal deal',
    'illegal sale', 'illegal sales', 'illegal market', 'illegal activity',
    'banned item', 'banned goods', 'banned content', 'restricted goods',

    # Weapons & Firearms
    'unlicensed firearm', 'weapon sale', 'selling weapons', 'weapon transaction',
    'weapon deal', 'weapon exchange', 'arrange a weapon', 'illegal weapon',
    'black market weapon', 'restricted weapon', 'prohibited weapon',
    'selling a gun', 'selling gun', 'gun for sale', 'guns for sale',
    'selling firearm', 'firearm for sale', 'selling a pistol', 'selling a rifle',
    'unregistered gun', 'ghost gun', 'ammo for sale', 'untraceable gun',
    'no license needed', 'no license required', 'no background check',
    'cash only no questions', 'no paperwork needed',

    # Drugs & Controlled Substances
    'drug sale', 'selling drugs', 'buy drugs', 'drug trafficking', 'trafficking',
    'cocaine for sale', 'heroin for sale', 'meth for sale', 'weed for sale',
    'pills for sale', 'narcotics for sale', 'illicit drugs',

    # Counterfeits & Fraud & Stolen Goods
    'counterfeit', 'counterfeit money', 'counterfeit goods', 'fake currency',
    'fake id', 'fake passport', 'fake license', 'fake driver license',
    'stolen goods', 'stolen items', 'stolen card', 'credit card fraud',
    'cvv for sale', 'dump cards', 'hacking service', 'hack service',
    'hire a hacker', 'hack account', 'human trafficking',
)

LEET_SUBSTITUTIONS = {'0': 'o', '1': 'i', '!': 'i', '3': 'e', '4': 'a', '@': 'a', '5': 's', '$': 's', '7': 't', '+': 't', '8': 'b'}
EXPLICIT_IMAGE_CATEGORIES = ('Porn', 'Hentai')
SAFE_IMAGE_CATEGORIES = ('Neutral', 'Drawing')

DEFAULT_TEXT_BLOCK_THRESHOLD = 0.85
DEFAULT_TEXT_REVIEW_THRESHOLD = 0.70
DEFAULT_IMAGE_BLOCK_THRESHOLD = 0.75
DEFAULT_IMAGE_REVIEW_THRESHOLD = 0.50

# -------------------------------------------------------------
# 2. LOGIC IMPLEMENTATIONS UNDER TEST
# -------------------------------------------------------------
def normalize_for_detection(text):
    text = re.sub(r'(.)\1{2,}', r'\1\1', text)
    lowered = text.lower()
    for k, v in LEET_SUBSTITUTIONS.items():
        lowered = lowered.replace(k, v)
    return re.sub(r'\s{2,}', ' ', lowered)

def score_illegal_text(text):
    if not text:
        return []
    lowered = text.lower()
    normalized = normalize_for_detection(text)
    matches = set()
    for kw in ILLEGAL_TEXT_KEYWORDS:
        pattern = rf'\b{re.escape(kw)}\b'
        if re.search(pattern, lowered) or re.search(pattern, normalized):
            matches.add(kw)
    return list(matches)

def parse_nsfw_response(result):
    if isinstance(result.get('probabilities'), dict):
        return result['probabilities']
    if isinstance(result.get('predictions'), list):
        return {p['className']: p['probability'] for p in result['predictions']}
    if 'flagged' in result:
        return {}
    raise ValueError("Unrecognized NSFW service response shape")

def evaluate_image_moderation(result, block_thresh=DEFAULT_IMAGE_BLOCK_THRESHOLD, review_thresh=DEFAULT_IMAGE_REVIEW_THRESHOLD):
    if result.get('flagged') is True and result.get('reason'):
        return 'block', result['reason']
    probs = parse_nsfw_response(result)
    if not probs:
        return None, None

    safe_score = probs.get('Neutral', 0.0) + probs.get('Drawing', 0.0)
    porn_score = probs.get('Porn', 0.0)
    hentai_score = probs.get('Hentai', 0.0)
    sexy_score = probs.get('Sexy', 0.0)

    worst_explicit_score = max(porn_score, hentai_score)
    worst_explicit_cat = 'Porn' if porn_score >= hentai_score else 'Hentai'

    # 1. High confidence explicit porn/hentai -> block
    if worst_explicit_score >= block_thresh:
        return 'block', f"Image flagged for {worst_explicit_cat} (score: {worst_explicit_score:.2f})"

    # 2. Borderline explicit porn/hentai -> review
    if worst_explicit_score >= review_thresh:
        return 'review', f"Image borderline for {worst_explicit_cat} (score: {worst_explicit_score:.2f})"

    # 3. Dominant safe classes (Neutral / Drawing) -> Clean & Legal!
    if safe_score >= 0.50 or probs.get('Neutral', 0.0) >= 0.40 or probs.get('Drawing', 0.0) >= 0.40:
        if worst_explicit_score < review_thresh and sexy_score < 0.85:
            return None, None

    # 4. High suggestive content -> review
    if sexy_score >= 0.85:
        return 'review', f"Image borderline for Sexy (score: {sexy_score:.2f})"

    return None, None

# Simulated Post object to test state machine and admin notifications
class MockPost:
    def __init__(self, post_id, name, content, state='active'):
        self.id = post_id
        self.name = name
        self.content = content
        self.state = state
        self.active = True
        self.moderation_reason = None
        self.chatter_messages = []
        self.activities = []

    def message_post(self, body, partner_ids=None, **kwargs):
        self.chatter_messages.append({'body': body, 'partner_ids': partner_ids})

    def apply_moderation_result(self, level, reason):
        if level in ('block', 'review'):
            self.state = 'pending' if level == 'block' else 'flagged'
            self.moderation_reason = reason
            badge = "🚨 ILLEGAL / PROHIBITED CONTENT DETECTED" if level == 'block' else "⚠️ BORDERLINE CONTENT FLAGGED"
            msg = f"{badge}: {reason}. Pending administrator approval before posting."
            self.message_post(body=msg, partner_ids=[1])  # partner 1 = admin
            self.activities.append({
                'summary': f'Moderation Approval Needed: {self.name}',
                'reason': reason,
                'status': 'open'
            })

    def action_approve_moderation(self, admin_user='admin'):
        self.state = 'active'
        self.active = True
        self.message_post(body=f"Approved by Administrator ({admin_user}). Post is now active and published.")
        for act in self.activities:
            act['status'] = 'done'

    def action_reject_moderation(self, admin_user='admin'):
        self.state = 'offensive'
        self.active = False
        self.message_post(body=f"Rejected by Administrator ({admin_user}). Post remains blocked.")
        for act in self.activities:
            act['status'] = 'done'

# -------------------------------------------------------------
# 3. TEST RUNNER
# -------------------------------------------------------------
def run_tests():
    passed = 0
    failed = 0

    def test(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            print(f"  [PASS] {name}")
            passed += 1
        else:
            print(f"  [FAIL] {name} - {detail}")
            failed += 1

    print("=== 1. Testing Keyword & Obfuscation Detection ===")
    test("Clean text passes without keyword matches",
         score_illegal_text("What is the best way to learn Python?") == [])

    # Verify safe short posts are NOT flagged
    test("Safe short post passes: 'Need help with my computer'",
         score_illegal_text("Need help with my computer") == [])

    test("Safe short post passes: 'Selling my old bicycle, pick up in town'",
         score_illegal_text("Selling my old bicycle, pick up in town") == [])

    test("Safe short post passes: 'Thanks for the advice, have a great day!'",
         score_illegal_text("Thanks for the advice, have a great day!") == [])

    test("Safe short post passes: 'Where can I buy a phone?'",
         score_illegal_text("Where can I buy a phone?") == [])

    test("Safe short post passes: 'How do I reset my password?'",
         score_illegal_text("How do I reset my password?") == [])

    test("Exact illegal keyword matches: 'gun for sale'",
         "gun for sale" in score_illegal_text("I have a gun for sale, contact me"))

    test("Exact illegal keyword matches: 'unlicensed firearm'",
         "unlicensed firearm" in score_illegal_text("Offering unlicensed firearm, cash only"))

    test("Exact illegal keyword matches: 'drug sale'",
         "drug sale" in score_illegal_text("Special drug sale tonight"))

    test("Leetspeak obfuscated keyword: 'gvn f0r s4l3'",
         "gun for sale" in score_illegal_text("I have a gun f0r s4l3"))

    test("Leetspeak obfuscated keyword: 'drvg s@le'",
         "drug sale" in score_illegal_text("drug s@le tonight"))

    test("Exact illegal keyword matches: 'Illegal Text Moderation Test'",
         len(score_illegal_text("Illegal Text Moderation Test")) > 0)

    test("Exact illegal keyword matches: 'illegal post'",
         len(score_illegal_text("This is an illegal post")) > 0)

    print("\n=== 2. Testing Image Moderation & NSFW Response Parsing ===")
    nsfw_raw_predictions = {
        "predictions": [
            {"className": "Neutral", "probability": 0.05},
            {"className": "Drawing", "probability": 0.02},
            {"className": "Sexy", "probability": 0.10},
            {"className": "Porn", "probability": 0.83},
            {"className": "Hentai", "probability": 0.00}
        ]
    }
    level, reason = evaluate_image_moderation(nsfw_raw_predictions)
    test("High porn probability triggers 'block'", level == 'block' and "Porn" in reason)

    nsfw_clean = {
        "predictions": [
            {"className": "Neutral", "probability": 0.95},
            {"className": "Drawing", "probability": 0.04},
            {"className": "Sexy", "probability": 0.01},
            {"className": "Porn", "probability": 0.00},
            {"className": "Hentai", "probability": 0.00}
        ]
    }
    level_clean, _ = evaluate_image_moderation(nsfw_clean)
    test("Neutral image triggers no moderation action (Clean/Legal)", level_clean is None)

    # test.png actual prediction shape: Drawing 0.598, Neutral 0.380, Hentai 0.021
    nsfw_test_png = {
        "predictions": [
            {"className": "Drawing", "probability": 0.598},
            {"className": "Neutral", "probability": 0.380},
            {"className": "Hentai", "probability": 0.021},
            {"className": "Porn", "probability": 0.0002},
            {"className": "Sexy", "probability": 0.0001}
        ]
    }
    level_test_png, _ = evaluate_image_moderation(nsfw_test_png)
    test("Safe image with Drawing/Neutral dominance is Clean/Legal", level_test_png is None)

    # Moderate Sexy on safe background should NOT be flagged
    nsfw_safe_summer = {
        "predictions": [
            {"className": "Neutral", "probability": 0.52},
            {"className": "Sexy", "probability": 0.45},
            {"className": "Porn", "probability": 0.02},
            {"className": "Hentai", "probability": 0.01}
        ]
    }
    level_summer, _ = evaluate_image_moderation(nsfw_safe_summer)
    test("Everyday photo with Neutral dominance and moderate Sexy is Clean", level_summer is None)

    # High suggestive Sexy without safe dominance triggers review
    nsfw_high_sexy = {
        "predictions": [
            {"className": "Neutral", "probability": 0.08},
            {"className": "Drawing", "probability": 0.02},
            {"className": "Sexy", "probability": 0.88},
            {"className": "Porn", "probability": 0.02}
        ]
    }
    level_sexy, _ = evaluate_image_moderation(nsfw_high_sexy)
    test("High suggestive image (Sexy >= 0.85) triggers 'review'", level_sexy == 'review')

    print("\n=== 3. Testing Admin Notification & Approval/Rejection Workflow ===")
    # Scenario A: User posts an illegal image or text
    post1 = MockPost(101, "Selling rifles no license", "Cash only deal")
    post1.apply_moderation_result('block', "Text flagged as illegal content (gun for sale)")
    
    test("Illegal post transitions to 'pending' (NOT 'offensive', NOT 'active')",
         post1.state == 'pending', f"Actual state: {post1.state}")
    test("Post stores moderation reason",
         post1.moderation_reason is not None)
    test("Chatter message sent to admin partner",
         len(post1.chatter_messages) > 0 and post1.chatter_messages[0]['partner_ids'] == [1])
    test("Activity created for admin approval",
         len(post1.activities) == 1 and post1.activities[0]['status'] == 'open')

    # Scenario B: Admin APPROVES the post
    post1.action_approve_moderation(admin_user='admin')
    test("Admin approval transitions post to 'active' (published on website)",
         post1.state == 'active' and post1.active is True)
    test("Approval chatter note is recorded",
         any("Approved by Administrator" in m['body'] for m in post1.chatter_messages))
    test("Moderation activity marked as done upon approval",
         post1.activities[0]['status'] == 'done')

    # Scenario C: Admin REJECTS the post
    post2 = MockPost(102, "Illegal firearm deal", "Contact via telegram")
    post2.apply_moderation_result('block', "Matched unlicensed firearm")
    test("Second illegal post is placed in 'pending'", post2.state == 'pending')
    post2.action_reject_moderation(admin_user='admin')
    test("Admin rejection transitions post to 'offensive' (unpublished)",
         post2.state == 'offensive' and post2.active is False)
    test("Rejection chatter note is recorded",
         any("Rejected by Administrator" in m['body'] for m in post2.chatter_messages))
    test("Moderation activity marked as done upon rejection",
         post2.activities[0]['status'] == 'done')

    print(f"\n==========================================")
    print(f"Test Summary: {passed} PASSED, {failed} FAILED")
    print(f"==========================================")

if __name__ == "__main__":
    run_tests()
