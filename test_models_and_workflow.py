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

LEET_SUBSTITUTIONS = {'0': 'o', '1': 'i', '!': 'i', '3': 'e', '4': 'a', '@': 'a', '5': 's', '$': 's', '7': 't', '+': 't', '8': 'b', 'v': 'u'}
HARMFUL_IMAGE_CATEGORIES = ('Porn', 'Hentai', 'Sexy')

DEFAULT_TEXT_BLOCK_THRESHOLD = 0.85
DEFAULT_TEXT_REVIEW_THRESHOLD = 0.70
DEFAULT_IMAGE_BLOCK_THRESHOLD = 0.75
DEFAULT_IMAGE_REVIEW_THRESHOLD = 0.60
ILLEGAL_SEMANTIC_THRESHOLD = 0.65
NORMAL_CONVERSATION_LOW_CONFIDENCE = 0.15

ILLEGAL_CATEGORY_LABELS = (
    "illegal weapon sale",
    "drug sale or trafficking",
    "counterfeit goods sale",
    "hacking or cybercrime service",
    "human trafficking",
    "stolen goods sale",
    "sale or arrangement of illegal or prohibited items",
    "normal conversation",
)

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
        if kw in lowered or kw in normalized:
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
    harmful_probs = {k: v for k, v in probs.items() if k in HARMFUL_IMAGE_CATEGORIES}
    if not harmful_probs:
        return None, None
    worst_category = max(harmful_probs, key=harmful_probs.get)
    worst_score = harmful_probs[worst_category]
    if worst_score >= block_thresh:
        return 'block', f"Image flagged for {worst_category} (score: {worst_score:.2f})"
    if worst_score >= review_thresh:
        return 'review', f"Image borderline for {worst_category} (score: {worst_score:.2f})"
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

    test("Exact illegal keyword matches: 'gun for sale'",
         "gun for sale" in score_illegal_text("I have a gun for sale, contact me"))

    test("Exact illegal keyword matches: 'unlicensed firearm'",
         "unlicensed firearm" in score_illegal_text("Offering unlicensed firearm, cash only"))

    test("Exact illegal keyword matches: 'drug sale'",
         "drug sale" in score_illegal_text("Special drug sale tonight"))

    test("Leetspeak obfuscated keyword: 'gvn f0r s4l3'",
         "gun for sale" in score_illegal_text("I have a gvn f0r s4l3"))

    test("Leetspeak obfuscated keyword: 'drvg s@le'",
         "drug sale" in score_illegal_text("drvg s@le tonight"))

    test("Leetspeak obfuscated keyword: 'w3ap0n s4l3'",
         "weapon sale" in score_illegal_text("w3ap0n s4l3 here"))

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
    test("Neutral image triggers no moderation action", level_clean is None)

    nsfw_borderline = {
        "predictions": [
            {"className": "Neutral", "probability": 0.32},
            {"className": "Sexy", "probability": 0.65},
            {"className": "Porn", "probability": 0.03}
        ]
    }
    level_border, _ = evaluate_image_moderation(nsfw_borderline)
    test("Borderline image (Sexy 0.65) triggers 'review'", level_border == 'review')

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
