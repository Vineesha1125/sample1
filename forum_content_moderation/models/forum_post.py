from odoo import models, api
import requests
import logging
import re
import base64
from detoxify import Detoxify
from transformers import pipeline

_logger = logging.getLogger(__name__)

# Load the text moderation model ONCE when Odoo starts (not per-post)
_text_model = Detoxify('original')

# Zero-shot classifier: scores text against arbitrary candidate labels by
# MEANING rather than literal keyword match. Closes the gap keyword
# matching cannot - "weapon transaction", "weapon deal", "arrange a
# firearm exchange" etc. all score similarly to "weapon sale" here,
# without needing every phrasing listed manually. Runs entirely locally -
# no API key, no network call, no billing account required.
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
    "normal conversation",
)

ILLEGAL_SEMANTIC_THRESHOLD = 0.65
NORMAL_CONVERSATION_LOW_CONFIDENCE = 0.15

# --- Default thresholds (used if no System Parameter is set) ---
DEFAULT_TEXT_BLOCK_THRESHOLD = 0.85
DEFAULT_TEXT_REVIEW_THRESHOLD = 0.70
DEFAULT_IMAGE_BLOCK_THRESHOLD = 0.75
DEFAULT_IMAGE_REVIEW_THRESHOLD = 0.60

PARAM_TEXT_BLOCK = 'forum_content_moderation.text_block_threshold'
PARAM_TEXT_REVIEW = 'forum_content_moderation.text_review_threshold'
PARAM_IMAGE_BLOCK = 'forum_content_moderation.image_block_threshold'
PARAM_IMAGE_REVIEW = 'forum_content_moderation.image_review_threshold'

# Service locations are configurable, not hardcoded, so this
# works outside a single dev machine without a code change.
PARAM_NSFW_SERVICE_URL = 'forum_content_moderation.nsfw_service_url'
PARAM_ODOO_BASE_URL = 'forum_content_moderation.odoo_base_url'
DEFAULT_NSFW_SERVICE_URL = 'http://localhost:5001/check-image'
DEFAULT_ODOO_BASE_URL = 'http://localhost:8069'

HARMFUL_IMAGE_CATEGORIES = ('Porn', 'Hentai', 'Sexy')

# Simple leetspeak / obfuscation substitutions used to catch evasive text
LEET_SUBSTITUTIONS = {'0': 'o', '1': 'i', '3': 'e', '4': 'a', '5': 's', '@': 'a', '$': 's'}

# Keyword-based illegal subject-matter detection. Separate from toxicity -
# toxicity measures TONE (hostility, insults, threats); this measures
# SUBJECT MATTER, so a calmly-worded illegal post that scores near-zero on
# toxicity is still caught here. First-pass control only: matches literal
# terms, does not generalise to rephrasing - expand this list over time.
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

# Fields whose change should trigger re-moderation on write()
MODERATION_TRIGGER_FIELDS = ('name', 'content')


class ForumPost(models.Model):
    _inherit = 'forum.post'

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

    def _get_nsfw_service_url(self):
        icp = self.env['ir.config_parameter'].sudo()
        return icp.get_param(PARAM_NSFW_SERVICE_URL, default=None) or DEFAULT_NSFW_SERVICE_URL

    def _get_odoo_base_url(self):
        icp = self.env['ir.config_parameter'].sudo()
        return icp.get_param(PARAM_ODOO_BASE_URL, default=None) or DEFAULT_ODOO_BASE_URL

    # ------------------------------------------------------------------
    # create() - moderate on creation
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        posts = super().create(vals_list)

        for post in posts:
            level, reason = self._check_content_level(post)
            self._apply_moderation_result(post, level, reason)

        return posts

    # ------------------------------------------------------------------
    # write() - re-moderate on edit, and log moderator overrides
    # ------------------------------------------------------------------
    def write(self, vals):
        track_override = 'state' in vals

        previous_states = {}
        if track_override:
            for post in self:
                previous_states[post.id] = post.state

        result = super().write(vals)

        if track_override:
            for post in self:
                old_state = previous_states.get(post.id)
                if old_state in ('offensive', 'flagged') and post.state != old_state:
                    self._log_moderation_feedback(post, old_state)

        if any(f in vals for f in MODERATION_TRIGGER_FIELDS):
            for post in self:
                level, reason = self._check_content_level(post)
                self._apply_moderation_result(post, level, reason, is_edit=True)

        return result

    def _apply_moderation_result(self, post, level, reason, is_edit=False):
        if level == 'block':
            super(ForumPost, post).write({'state': 'offensive'})
            prefix = "Blocked automatically (on edit)" if is_edit else "Blocked automatically"
            post.message_post(body=f"{prefix}: {reason}")
            _logger.warning("Forum post %s blocked: %s", post.id, reason)

        elif level == 'review':
            super(ForumPost, post).write({'state': 'flagged'})
            prefix = "Flagged for moderator review (on edit)" if is_edit else "Flagged for moderator review"
            post.message_post(body=f"{prefix}: {reason}")
            _logger.info("Forum post %s flagged for review: %s", post.id, reason)

    def _log_moderation_feedback(self, post, original_state):
        try:
            self.env['forum.moderation.feedback'].sudo().create({
                'post_id': post.id,
                'original_state': original_state,
                'corrected_state': post.state,
                'corrected_by': self.env.uid,
            })
            _logger.info(
                "Moderation override logged for post %s: %s -> %s by user %s",
                post.id, original_state, post.state, self.env.uid
            )
        except Exception as e:
            _logger.error("Failed to log moderation feedback for post %s: %s", post.id, e)

    # ------------------------------------------------------------------
    # Content-level check (illegal keywords + text + image) - first
    # violation found wins, illegal subject-matter checked first since
    # it's an unconditional block independent of toxicity/NSFW scoring.
    # ------------------------------------------------------------------
    def _check_content_level(self, post):
        """Returns ('block' | 'review' | None, reason). First violation found wins."""
        title = post.name or ''
        raw_text = re.sub(r'<[^>]+>', ' ', post.content or '')
        full_text = f"{title}. {raw_text}".strip()

        illegal_matches = self._score_illegal_text(full_text)
        if illegal_matches:
            return 'block', f"Text flagged as illegal content (matched: {', '.join(illegal_matches)})"

        try:
            semantic_level, semantic_label, semantic_score = self._score_illegal_text_semantic(full_text)
            if semantic_level == 'block':
                return 'block', f"Text flagged as illegal content ({semantic_label}, confidence: {semantic_score:.2f})"
            if semantic_level == 'review':
                return 'review', f"Text borderline for illegal content (model confidence this is normal conversation: {semantic_score:.2f})"
        except Exception as e:
            # Fail OPEN on classifier failure, consistent with the rest
            # of this module's error handling.
            _logger.error("Illegal-content semantic check failed: %s", e)

        text_level, text_reason = self._check_text_toxicity(post)
        if text_level == 'block':
            return 'block', text_reason

        content = post.content or ''
        image_level, image_reason = None, None

        base64_images = re.findall(r'data:image/[^;]+;base64,([^"\']+)', content)
        for b64_data in base64_images:
            try:
                image_bytes = base64.b64decode(b64_data)
                level, reason = self._check_image_nsfw_bytes(image_bytes, 'embedded_image.png', 'image/png')
                if level == 'block':
                    return 'block', reason
                if level == 'review' and image_level is None:
                    image_level, image_reason = level, reason
            except Exception as e:
                _logger.error("Failed to decode base64 image: %s", e)

        image_urls = re.findall(r'<img[^>]+src="(/web/image/[^"]+)"', content)
        for url in image_urls:
            try:
                full_url = f"{self._get_odoo_base_url()}{url}"
                img_response = requests.get(full_url, timeout=10)
                if img_response.status_code == 200:
                    level, reason = self._check_image_nsfw_bytes(
                        img_response.content, 'linked_image.png', 'image/png'
                    )
                    if level == 'block':
                        return 'block', reason
                    if level == 'review' and image_level is None:
                        image_level, image_reason = level, reason
                else:
                    _logger.warning(
                        "Image fetch from %s returned non-200 status %s; skipping check for this image",
                        url, img_response.status_code
                    )
            except Exception as e:
                _logger.error("Failed to fetch image from %s: %s", url, e)

        if text_level == 'review':
            return 'review', text_reason
        if image_level == 'review':
            return 'review', image_reason

        return None, None

    # ------------------------------------------------------------------
    # ILLEGAL SUBJECT-MATTER TEXT CHECK
    # ------------------------------------------------------------------
    def _score_illegal_text(self, text):
        """Raw scorer for illegal subject-matter keywords. Case-insensitive,
        literal-match only. Returns a list of matched terms (empty if none)."""
        if not text:
            return []
        lowered = text.lower()
        return [kw for kw in ILLEGAL_TEXT_KEYWORDS if kw in lowered]

    def _score_illegal_text_semantic(self, text):
        """Zero-shot classification against illegal-content category labels.
        Returns (level, label, score):
          - ('block', label, score) if a specific illegal category crosses
            the block threshold.
          - ('review', None, normal_conv_score) if no single category
            dominates, but the model is confident this ISN'T normal
            conversation either.
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

        if normal_score < NORMAL_CONVERSATION_LOW_CONFIDENCE:
            return 'review', None, normal_score

        return None, None, None

    # ------------------------------------------------------------------
    # TEXT MODERATION (toxicity)
    # ------------------------------------------------------------------
    def _score_text_toxicity(self, text):
        """
        Returns (worst_category, worst_score, all_scores) for `text`,
        scored across chunks of both the original and the
        evasion-normalised variant. Raises on model failure - caller
        decides fail-open vs fail-closed.
        """
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
                results = self._text_model_predict(chunk)  # raises if model fails
                for cat, score in results.items():
                    if score > all_scores.get(cat, 0.0):
                        all_scores[cat] = score
                cat, score = max(results.items(), key=lambda kv: kv[1])
                if score > worst_score:
                    worst_category, worst_score = cat, score

        return worst_category, worst_score, all_scores

    def _check_text_toxicity(self, post):
        raw_text = re.sub(r'<[^>]+>', ' ', post.content or '')
        title = post.name or ''
        full_text = f"{title}. {raw_text}".strip()

        if not full_text:
            return None, None

        block_threshold, review_threshold = self._get_text_thresholds()

        try:
            worst_category, worst_score, _all_scores = self._score_text_toxicity(full_text)

            if worst_category is None:
                return None, None
            if worst_score >= block_threshold:
                return 'block', f"Text flagged for {worst_category} (score: {worst_score:.2f})"
            if worst_score >= review_threshold:
                return 'review', f"Text borderline for {worst_category} (score: {worst_score:.2f})"
            return None, None
        except Exception as e:
            # Fail OPEN: internal create()/write() flow treats a failed
            # check as "no action", same as before this refactor.
            _logger.error("Text moderation check failed: %s", e)
            return None, None

    def _normalize_for_detection(self, text):
        """Collapses repeated characters and common leetspeak substitutions
        used to evade keyword/toxicity detection (e.g. 'k1ll', 'h3ll0')."""
        text = re.sub(r'(.)\1{2,}', r'\1\1', text)
        lowered = text.lower()
        for k, v in LEET_SUBSTITUTIONS.items():
            lowered = lowered.replace(k, v)
        lowered = re.sub(r'\s{2,}', ' ', lowered)
        return lowered

    def _chunk_text(self, text, max_chars=800):
        """
        Splits text into chunks up to max_chars, breaking on whitespace
        rather than mid-word, so a word straddling a chunk boundary isn't
        cut in half and scored as two malformed fragments.
        """
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

        final_chunks = []
        for chunk in chunks:
            if len(chunk) <= max_chars:
                final_chunks.append(chunk)
            else:
                final_chunks.extend(
                    chunk[i:i + max_chars] for i in range(0, len(chunk), max_chars)
                )

        return final_chunks

    def _text_model_predict(self, text):
        return _text_model.predict(text)

    # ------------------------------------------------------------------
    # IMAGE MODERATION (NSFW - illegal-image category check not yet
    # active; see project documentation Section 11.3)
    # ------------------------------------------------------------------
    def _score_image_nsfw(self, image_bytes, filename, mimetype):
        """
        Returns (worst_harmful_category, worst_harmful_score, all_probs)
        for the given image bytes. Raises on failure - caller decides
        fail-open vs fail-closed.
        """
        files = {'image': (filename, image_bytes, mimetype)}
        response = requests.post(
            self._get_nsfw_service_url(),
            files=files,
            timeout=10
        )
        response.raise_for_status()
        result = response.json()

        probs = self._parse_nsfw_response(result)

        harmful_probs = {k: v for k, v in probs.items() if k in HARMFUL_IMAGE_CATEGORIES}
        if not harmful_probs:
            return None, 0.0, probs

        worst_category = max(harmful_probs, key=harmful_probs.get)
        worst_score = harmful_probs[worst_category]
        return worst_category, worst_score, probs

    def _parse_nsfw_response(self, result):
        """
        Normalizes the NSFW service's response into a flat
        {className: probability} dict, regardless of which shape it
        arrives in:
          - {'probabilities': {'Porn': 0.9, ...}}
          - {'predictions': [{'className': 'Porn', 'probability': 0.9}, ...]}
          - {'flagged': True, 'reason': '...'} (illegal-category shape,
            reserved for future use once the OpenAI-based image check in
            nsfw-service is re-enabled - see project documentation)
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
            # No numeric probabilities to score here - handled explicitly
            # by callers that check for a 'reason' field directly.
            return {}

        _logger.error(
            "NSFW service response matched neither 'probabilities' (dict) "
            "nor 'predictions' (list) shape: %r", result
        )
        raise ValueError("Unrecognized NSFW service response shape")

    def _check_image_nsfw_bytes(self, image_bytes, filename, mimetype):
        block_threshold, review_threshold = self._get_image_thresholds()

        try:
            result = requests.post(
                self._get_nsfw_service_url(),
                files={'image': (filename, image_bytes, mimetype)},
                timeout=10
            )
            result.raise_for_status()
            response_json = result.json()

            # Illegal-category shape, if/when the microservice's OpenAI
            # check is active (currently inactive by default).
            if response_json.get('flagged') is True and response_json.get('reason'):
                return 'block', response_json['reason']

            probs = self._parse_nsfw_response(response_json)
            harmful_probs = {k: v for k, v in probs.items() if k in HARMFUL_IMAGE_CATEGORIES}

            if not harmful_probs:
                return None, None

            worst_category = max(harmful_probs, key=harmful_probs.get)
            worst_score = harmful_probs[worst_category]

            if worst_score >= block_threshold:
                return 'block', f"Image flagged for {worst_category} (score: {worst_score:.2f})"
            if worst_score >= review_threshold:
                return 'review', f"Image borderline for {worst_category} (score: {worst_score:.2f})"
            return None, None
        except Exception as e:
            # Fail OPEN: internal create()/write()/attachment flow treats
            # a failed check as "no action", same as before this refactor.
            _logger.error("NSFW check failed: %s", e)
            return None, None