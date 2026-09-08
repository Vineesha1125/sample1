from odoo import models
import requests
import logging
import re
from detoxify import Detoxify
from transformers import pipeline

_logger = logging.getLogger(__name__)

# Loaded ONCE at Odoo start-up, shared by every caller of this service.
_text_model = Detoxify('original')

# Zero-shot classifier: scores text against arbitrary candidate labels by
# MEANING rather than literal keyword match. Closes the gap keyword
# matching cannot - "weapon transaction", "arrange a firearm exchange"
# etc. all score similarly to "weapon sale" here, without needing every
# phrasing listed manually. Runs entirely locally - no API key needed.
_illegal_classifier = pipeline(
    "zero-shot-classification",
    model="facebook/bart-large-mnli"
)

ILLEGAL_CATEGORY_LABELS = (
    "illegal weapon sale",
    "drug sale or trafficking",
    "counterfeit goods sale",
    "hacking or cybercrime service",
    "human trafficking",
    "stolen goods sale",
    "sale or arrangement of illegal or prohibited items",
    "normal conversation",  # included so the model has a genuine "none of the above" option
)

ILLEGAL_SEMANTIC_THRESHOLD = 0.65

# Fallback signal: if the model is confident this ISN'T normal conversation
# (low score here) but no single illegal category crosses the block
# threshold above, the probability mass is likely spread across several
# related categories rather than concentrated in one. This routes that
# ambiguous-but-clearly-not-normal case to review instead of letting it
# pass silently.
NORMAL_CONVERSATION_LOW_CONFIDENCE = 0.15
# Below this length, the classifier has too little context to reliably
# judge "normal conversation" either way (short/sparse text like "img",
# "test", "hi" scores low on every label simply for lacking content, not
# because it's suspicious). The low-confidence fallback is skipped for
# text shorter than this - only the block-level check (a strong, specific
# category match) still applies regardless of length.
SEMANTIC_MIN_TEXT_LENGTH = 20

DEFAULT_TEXT_BLOCK_THRESHOLD = 0.85
DEFAULT_TEXT_REVIEW_THRESHOLD = 0.70
DEFAULT_IMAGE_BLOCK_THRESHOLD = 0.75
DEFAULT_IMAGE_REVIEW_THRESHOLD = 0.50

PARAM_TEXT_BLOCK = 'forum_content_moderation.text_block_threshold'
PARAM_TEXT_REVIEW = 'forum_content_moderation.text_review_threshold'
PARAM_IMAGE_BLOCK = 'forum_content_moderation.image_block_threshold'
PARAM_IMAGE_REVIEW = 'forum_content_moderation.image_review_threshold'

# Explicit adult content categories - block/review based on thresholds.
EXPLICIT_IMAGE_CATEGORIES = ('Porn', 'Hentai')
# Suggestive-but-not-explicit - only held at a much higher bar (see
# _score_image_nsfw), since "Sexy" alone covers a very broad range of
# ordinary, non-explicit imagery (swimwear photos, fashion shots, etc.)
# and blocking it at the same threshold as Porn/Hentai produces heavy
# false positives.
SUGGESTIVE_IMAGE_CATEGORIES = ('Sexy',)
SUGGESTIVE_IMAGE_HIGH_CONFIDENCE = 0.85
# Categories indicating clearly safe/legal content.
SAFE_IMAGE_CATEGORIES = ('Neutral', 'Drawing')
SAFE_IMAGE_DOMINANCE_THRESHOLD = 0.50
SAFE_IMAGE_SINGLE_CATEGORY_THRESHOLD = 0.40

LEET_SUBSTITUTIONS = {'0': 'o', '1': 'i', '3': 'e', '4': 'a', '5': 's', '@': 'a', '$': 's'}

# Keyword-based illegal subject-matter detection. Whole-word matching
# (via regex \b boundaries) so 'illegal' doesn't match inside an unrelated
# longer word. Kept as a fast, cheap first-pass check that runs before the
# (slower) zero-shot classifier below - an exact match short-circuits
# immediately without needing a model inference call. The classifier is
# the catch-all for rephrasing not covered by this list.
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


class ContentModerationMixin(models.AbstractModel):
    """Shared, model-agnostic content moderation service.

    This holds NO knowledge of forum posts, attachments, or any other
    specific model. Any Odoo model can inherit this mixin to gain
    check_text()/check_image_bytes(), or any code can call it directly
    via self.env['content.moderation.mixin'].check_text(...) without
    needing a real record of any kind — including from a standalone API
    endpoint, as in custom_forum_api.

    All checks default to fail-open (raise_on_error=False): on an
    internal error, they log it and return (None, None) rather than
    raising, so a moderation-service problem never blocks whatever the
    caller is trying to do. Pass raise_on_error=True when the caller
    itself needs to know a check genuinely failed (e.g. a standalone
    moderation-check API responding to an external system) rather than
    silently treating a failed check as "clean".
    """
    _name = 'content.moderation.mixin'
    _description = 'Shared Content Moderation Service'

    # ------------------------------------------------------------------
    # Threshold lookups
    # ------------------------------------------------------------------
    def _get_threshold(self, param_key, default_value):
        icp = self.env['ir.config_parameter'].sudo()
        raw = icp.get_param(param_key, default=None)
        if raw is None:
            return default_value
        try:
            return float(raw)
        except (TypeError, ValueError):
            _logger.warning(
                "Invalid value for %s (%r), using default %.2f",
                param_key, raw, default_value
            )
            return default_value

    def _get_text_thresholds(self):
        block = self._get_threshold(PARAM_TEXT_BLOCK, DEFAULT_TEXT_BLOCK_THRESHOLD)
        review = self._get_threshold(PARAM_TEXT_REVIEW, DEFAULT_TEXT_REVIEW_THRESHOLD)
        return block, review

    def _get_image_thresholds(self):
        block = self._get_threshold(PARAM_IMAGE_BLOCK, DEFAULT_IMAGE_BLOCK_THRESHOLD)
        review = self._get_threshold(PARAM_IMAGE_REVIEW, DEFAULT_IMAGE_REVIEW_THRESHOLD)
        return block, review

    # ------------------------------------------------------------------
    # Text moderation (model-agnostic)
    # ------------------------------------------------------------------
    def check_text(self, text, raise_on_error=False):
        """Checks a plain text string. Returns ('block' | 'review' | None, reason).
        Callers are responsible for combining/stripping HTML from any
        model-specific fields (e.g. title + body) before calling this."""
        text = (text or '').strip()
        if not text:
            return None, None

        # 1. Deterministic whole-word keyword check - fast, exact matches only.
        illegal_matches = self._score_illegal_text_keywords(text)
        if illegal_matches:
            return 'block', f"Text flagged as illegal content (matched: {', '.join(illegal_matches)})"

        # 2. Semantic pass - catches rephrasing the keyword list misses.
        try:
            semantic_level, semantic_label, semantic_score = self._score_illegal_text_semantic(text)
            if semantic_level == 'block':
                return 'block', f"Text flagged as illegal content ({semantic_label}, confidence: {semantic_score:.2f})"
            if semantic_level == 'review':
                return 'review', f"Text borderline for illegal content (model confidence this is normal conversation: {semantic_score:.2f})"
        except Exception as e:
            # Fail OPEN on classifier failure - a classifier outage
            # should not block legitimate posts.
            _logger.error("Illegal-content semantic check failed: %s", e)

        # 3. Detoxify toxicity/tone check.
        block_threshold, review_threshold = self._get_text_thresholds()

        try:
            worst_category, worst_score = self._score_worst_across_variants(text)

            if worst_score >= block_threshold:
                return 'block', f"Text flagged for {worst_category} (score: {worst_score:.2f})"
            if worst_score >= review_threshold:
                return 'review', f"Text borderline for {worst_category} (score: {worst_score:.2f})"
            return None, None
        except Exception as e:
            _logger.error("Text moderation check failed: %s", e)
            if raise_on_error:
                raise
            return None, None

    def _score_illegal_text_keywords(self, text):
        """Raw scorer for illegal subject-matter keywords. Whole-word,
        case-insensitive matching via regex boundaries - 'illegal' will
        not match inside an unrelated longer word. Returns a list of
        matched terms (empty if none)."""
        if not text:
            return []
        lowered = text.lower()
        normalized = self._normalize_for_detection(text)
        matches = []
        for kw in ILLEGAL_TEXT_KEYWORDS:
            pattern = rf'\b{re.escape(kw)}\b'
            if re.search(pattern, lowered) or re.search(pattern, normalized):
                matches.append(kw)
        return matches

    def _score_illegal_text_semantic(self, text):
        """Zero-shot classification against illegal-content category labels.
        Returns (level, label, score):
          - ('block', label, score) if a specific illegal category crosses
            the block threshold.
          - ('review', None, normal_conv_score) if no single category
            dominates, but the model is confident this ISN'T normal
            conversation either (probability spread across several
            related categories - common for vague/generic phrasing).
          - (None, None, None) if this looks like normal conversation.
        Raises on model failure - caller decides fail-open handling."""
        if not text or not text.strip():
            return None, None, None
        result = _illegal_classifier(text, candidate_labels=list(ILLEGAL_CATEGORY_LABELS))
        labels = result['labels']
        scores = result['scores']

        top_label, top_score = labels[0], scores[0]

        if top_label != "normal conversation" and top_score >= ILLEGAL_SEMANTIC_THRESHOLD:
            return 'block', top_label, top_score

        normal_idx = labels.index("normal conversation")
        normal_score = scores[normal_idx]

        # Only apply the low-confidence review fallback to text with
        # enough content for the classifier to judge meaningfully - short
        # text (titles like "img", "test", "hi") scores low on every
        # label simply for lacking substance, not because it's suspicious.
        if len(text.strip()) >= SEMANTIC_MIN_TEXT_LENGTH and normal_score < NORMAL_CONVERSATION_LOW_CONFIDENCE:
            return 'review', None, normal_score

        return None, None, None

    def _score_worst_across_variants(self, text):
        variants = [text]
        normalized = self._normalize_for_detection(text)
        if normalized != text:
            variants.append(normalized)

        worst_category, worst_score = None, 0.0

        for variant in variants:
            for chunk in self._chunk_text(variant, max_chars=800):
                if not chunk.strip():
                    continue
                results = _text_model.predict(chunk)
                cat, score = max(results.items(), key=lambda kv: kv[1])
                if score > worst_score:
                    worst_category, worst_score = cat, score

        return worst_category, worst_score

    def _normalize_for_detection(self, text):
        text = re.sub(r'(.)\1{2,}', r'\1\1', text)
        lowered = text.lower()
        for k, v in LEET_SUBSTITUTIONS.items():
            lowered = lowered.replace(k, v)
        return re.sub(r'\s{2,}', ' ', lowered)

    def _chunk_text(self, text, max_chars=800):
        if not text:
            return []
        return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]

    # ------------------------------------------------------------------
    # Image moderation (model-agnostic)
    # ------------------------------------------------------------------
    def check_image_bytes(self, image_bytes, filename='upload.png', mimetype='image/png', raise_on_error=False):
        """Checks raw image bytes. Returns ('block' | 'review' | None, reason).

        Decision order:
          1. Explicit adult content (Porn/Hentai) at or above block
             threshold -> block.
          2. Explicit adult content at or above review threshold -> review.
          3. Dominant safe content (Neutral/Drawing) with no meaningful
             explicit/suggestive signal -> clean, returned immediately.
          4. High-confidence suggestive-only content (Sexy, without safe
             dominance) -> review. This is a separate, explicit branch -
             not nested inside the safe-dominance check - so images that
             are neither clearly safe nor clearly explicit still get a
             defined outcome instead of falling through to an implicit
             None.
          5. Otherwise -> clean.
        """
        block_threshold, review_threshold = self._get_image_thresholds()

        try:
            files = {'image': (filename, image_bytes, mimetype)}
            response = requests.post(
                self._get_nsfw_service_url() if hasattr(self, '_get_nsfw_service_url') else "http://localhost:5001/check-image",
                files=files,
                timeout=10
            )
            result = response.json()

            if result.get('flagged') is True and result.get('reason'):
                return 'block', result['reason']

            predictions = result.get('predictions', [])
            probs = {p['className']: p['probability'] for p in predictions}
            if not probs:
                return None, None

            explicit_probs = {k: v for k, v in probs.items() if k in EXPLICIT_IMAGE_CATEGORIES}
            worst_explicit_cat, worst_explicit_score = (
                max(explicit_probs.items(), key=lambda kv: kv[1])
                if explicit_probs else (None, 0.0)
            )
            sexy_score = probs.get('Sexy', 0.0)
            safe_score = probs.get('Neutral', 0.0) + probs.get('Drawing', 0.0)
            safe_dominant = (
                safe_score >= SAFE_IMAGE_DOMINANCE_THRESHOLD
                or probs.get('Neutral', 0.0) >= SAFE_IMAGE_SINGLE_CATEGORY_THRESHOLD
                or probs.get('Drawing', 0.0) >= SAFE_IMAGE_SINGLE_CATEGORY_THRESHOLD
            )

            # 1. High-confidence explicit content -> block.
            if worst_explicit_score >= block_threshold:
                return 'block', f"Image flagged for {worst_explicit_cat} (score: {worst_explicit_score:.2f})"

            # 2. Borderline explicit content -> review.
            if worst_explicit_score >= review_threshold:
                return 'review', f"Image borderline for {worst_explicit_cat} (score: {worst_explicit_score:.2f})"

            # 3. Clearly safe/legal dominant content, and nothing
            # borderline-explicit or extremely suggestive -> clean.
            if safe_dominant and sexy_score < SUGGESTIVE_IMAGE_HIGH_CONFIDENCE:
                return None, None

            # 4. High-confidence suggestive-only content -> review. This
            # is a separate branch (not nested under #3) so it's reached
            # even when safe_dominant is False.
            if sexy_score >= SUGGESTIVE_IMAGE_HIGH_CONFIDENCE:
                return 'review', f"Image borderline for Sexy (score: {sexy_score:.2f})"

            # 5. Nothing crossed any threshold - clean.
            return None, None
        except Exception as e:
            _logger.error("NSFW check failed: %s", e)
            if raise_on_error:
                raise
            return None, None