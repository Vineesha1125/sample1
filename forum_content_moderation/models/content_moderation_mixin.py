from odoo import models
import requests
import logging
import re
from detoxify import Detoxify

_logger = logging.getLogger(__name__)

# Loaded ONCE at Odoo start-up, shared by every caller of this service.
_text_model = Detoxify('original')

DEFAULT_TEXT_BLOCK_THRESHOLD = 0.85
DEFAULT_TEXT_REVIEW_THRESHOLD = 0.70
DEFAULT_IMAGE_BLOCK_THRESHOLD = 0.75
DEFAULT_IMAGE_REVIEW_THRESHOLD = 0.60

PARAM_TEXT_BLOCK = 'forum_content_moderation.text_block_threshold'
PARAM_TEXT_REVIEW = 'forum_content_moderation.text_review_threshold'
PARAM_IMAGE_BLOCK = 'forum_content_moderation.image_block_threshold'
PARAM_IMAGE_REVIEW = 'forum_content_moderation.image_review_threshold'

# Service locations are configurable, not hardcoded, so this works
# outside a single dev machine without a code change.
PARAM_NSFW_SERVICE_URL = 'forum_content_moderation.nsfw_service_url'
DEFAULT_NSFW_SERVICE_URL = 'http://localhost:5001/check-image'

HARMFUL_IMAGE_CATEGORIES = ('Porn', 'Hentai', 'Sexy')
LEET_SUBSTITUTIONS = {'0': 'o', '1': 'i', '3': 'e', '4': 'a', '5': 's', '@': 'a', '$': 's'}

# Simple keyword-based illegal content detection.
# This is separate from toxicity scoring - toxicity measures TONE
# (hostility, insults, threats), this measures SUBJECT MATTER. A calmly
# worded illegal post scores near-zero on toxicity but should still be
# caught here. This is a basic first pass, not a substitute for a proper
# content-category classifier - expand this list or replace with a real
# classifier (e.g. an external moderation API) as coverage needs grow.
ILLEGAL_TEXT_KEYWORDS = (
    'unlicensed firearm', 'weapon sale', 'drug sale', 'counterfeit',
    'trafficking', 'stolen goods', 'hacking service', 'fake id',
)


class ContentModerationMixin(models.AbstractModel):
    """Shared, model-agnostic content moderation service.

    Public entry points â€” check_text() and check_image_bytes() â€” always
    fail open by default (raise_on_error=False): on an internal error
    they log it and return (None, None), matching the system's
    documented fail-open design for real content creation. Pass
    raise_on_error=True (used by the standalone moderation-check API)
    when the caller needs to know a check genuinely failed rather than
    treating a failed check as "clean".

    Internally, each public method delegates to a raw "_score_..."
    method that always raises on failure and never applies thresholds â€”
    this keeps the fail-open/fail-closed decision in exactly one place
    per check, rather than duplicated across every caller.
    """
    _name = 'content.moderation.mixin'
    _description = 'Shared Content Moderation Service'

    # ------------------------------------------------------------------
    # Threshold / config lookups
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

    def _get_nsfw_service_url(self):
        icp = self.env['ir.config_parameter'].sudo()
        return icp.get_param(PARAM_NSFW_SERVICE_URL, default=None) or DEFAULT_NSFW_SERVICE_URL

    # ------------------------------------------------------------------
    # TEXT MODERATION
    # ------------------------------------------------------------------
    def check_text(self, text, raise_on_error=False):
        """Public entry point. Returns ('block' | 'review' | None, reason)."""
        text = (text or '').strip()
        if not text:
            return None, None

        block_threshold, review_threshold = self._get_text_thresholds()

        try:
            # Illegal subject-matter check runs first and independently of
            # the toxicity score - a calmly worded illegal post would
            # otherwise score near-zero on toxicity and pass through.
            matched_terms = self._score_illegal_text(text)
            if matched_terms:
                return 'block', f"Text flagged as illegal content (matched: {', '.join(matched_terms)})"

            worst_category, worst_score, _all_scores = self._score_text_toxicity(text)

            if worst_category is None:
                return None, None
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

    def _score_illegal_text(self, text):
        """Raw scorer for illegal subject-matter keywords. Separate from
        toxicity - this checks WHAT is being said, not HOW aggressively
        it's phrased. Returns a list of matched terms (empty if none)."""
        lowered = text.lower()
        return [kw for kw in ILLEGAL_TEXT_KEYWORDS if kw in lowered]

    def _score_text_toxicity(self, text):
        """Raw scorer. Raises on model failure â€” never catches, never
        applies thresholds. Returns (worst_category, worst_score, all_scores)."""
        if not text or not text.strip():
            return None, 0.0, {}

        variants = [text]
        normalized = self._normalize_for_detection(text)
        if normalized != text:
            variants.append(normalized)

        worst_category, worst_score = None, 0.0
        all_scores = {}

        for variant in variants:
            for chunk in self._chunk_text(variant, max_chars=800):
                if not chunk.strip():
                    continue
                results = _text_model.predict(chunk)  # raises if model fails
                for cat, score in results.items():
                    if score > all_scores.get(cat, 0.0):
                        all_scores[cat] = score
                cat, score = max(results.items(), key=lambda kv: kv[1])
                if score > worst_score:
                    worst_category, worst_score = cat, score

        return worst_category, worst_score, all_scores

    def _normalize_for_detection(self, text):
        text = re.sub(r'(.)\1{2,}', r'\1\1', text)
        lowered = text.lower()
        for k, v in LEET_SUBSTITUTIONS.items():
            lowered = lowered.replace(k, v)
        return re.sub(r'\s{2,}', ' ', lowered)

    def _chunk_text(self, text, max_chars=800):
        """Splits text into chunks up to max_chars, breaking on whitespace
        rather than mid-word, so a word straddling a chunk boundary isn't
        cut in half and scored as two malformed fragments."""
        if not text:
            return []

        words = text.split(' ')
        chunks = []
        current = []
        current_len = 0

        for word in words:
            added_len = len(word) + (1 if current else 0)
            if current_len + added_len > max_chars and current:
                chunks.append(' '.join(current))
                current = [word]
                current_len = len(word)
            else:
                current.append(word)
                current_len += added_len

        if current:
            chunks.append(' '.join(current))

        # Fallback: a single "word" longer than max_chars (e.g. a URL or
        # spam string with no spaces) â€” hard-slice it so it still gets
        # scored instead of being skipped entirely.
        final_chunks = []
        for chunk in chunks:
            if len(chunk) <= max_chars:
                final_chunks.append(chunk)
            else:
                final_chunks.extend(
                    chunk[i:i + max_chars] for i in range(0, len(chunk), max_chars)
                )

        return final_chunks

    # ------------------------------------------------------------------
    # IMAGE MODERATION
    # ------------------------------------------------------------------
    def check_image_bytes(self, image_bytes, filename='upload.png', mimetype='image/png', raise_on_error=False):
        """Public entry point. Returns ('block' | 'review' | None, reason)."""
        block_threshold, review_threshold = self._get_image_thresholds()

        try:
            result = self._call_nsfw_service(image_bytes, filename, mimetype)

            # If the service explicitly flagged the image (e.g. via an
            # illegal-content category check on the service side) and gave
            # us a ready-made reason, use it directly rather than trying to
            # force it through the probability/threshold path below.
            if result.get('flagged') is True and result.get('reason'):
                return 'block', result['reason']

            worst_category, worst_score, _all_probs = self._score_image_nsfw(result)

            if worst_category is None:
                return None, None
            if worst_score >= block_threshold:
                return 'block', f"Image flagged for {worst_category} (score: {worst_score:.2f})"
            if worst_score >= review_threshold:
                return 'review', f"Image borderline for {worst_category} (score: {worst_score:.2f})"
            return None, None
        except Exception as e:
            _logger.error("NSFW check failed: %s", e)
            if raise_on_error:
                raise
            return None, None

    def _call_nsfw_service(self, image_bytes, filename, mimetype):
        """Raw HTTP call to the external image moderation service. Raises
        on failure (bad response, timeout, connection error)."""
        files = {'image': (filename, image_bytes, mimetype)}
        response = requests.post(
            self._get_nsfw_service_url(),
            files=files,
            timeout=10
        )
        response.raise_for_status()
        return response.json()

    def _score_image_nsfw(self, result):
        """Raw scorer. Raises on failure (unrecognized shape) â€” never
        applies thresholds. Takes the already-parsed JSON response."""
        probs = self._parse_nsfw_response(result)

        harmful_probs = {k: v for k, v in probs.items() if k in HARMFUL_IMAGE_CATEGORIES}
        if not harmful_probs:
            return None, 0.0, probs

        worst_category = max(harmful_probs, key=harmful_probs.get)
        worst_score = harmful_probs[worst_category]
        return worst_category, worst_score, probs

    def _parse_nsfw_response(self, result):
        """Normalizes the NSFW service's response into a flat
        {className: probability} dict, accepting either shape:
          - {'probabilities': {'Porn': 0.9, ...}}
          - {'predictions': [{'className': 'Porn', 'probability': 0.9}, ...]}
        If neither shape is present (e.g. the response only carried a
        top-level 'flagged'/'reason' pair, already handled upstream in
        check_image_bytes), returns an empty dict rather than raising -
        that's a valid "nothing to score here" case, not malformed data.
        """
        if isinstance(result.get('probabilities'), dict):
            return result['probabilities']

        if isinstance(result.get('predictions'), list):
            try:
                return {
                    p['className']: p['probability']
                    for p in result['predictions']
                }
            except (KeyError, TypeError):
                _logger.error(
                    "NSFW service returned a 'predictions' list with an "
                    "unexpected item shape: %r", result['predictions']
                )
                raise ValueError("Malformed 'predictions' entries in NSFW response")

        if 'flagged' in result:
            # Already handled explicitly in check_image_bytes via the
            # 'reason' short-circuit; nothing left to score numerically.
            return {}

        _logger.error(
            "NSFW service response matched no recognized shape: %r", result
        )
        raise ValueError("Unrecognized NSFW service response shape")