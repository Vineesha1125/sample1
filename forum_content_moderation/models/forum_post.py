from odoo import models, api
import requests
import logging
import re
import base64
from detoxify import Detoxify

_logger = logging.getLogger(__name__)

# Load the text moderation model ONCE when Odoo starts (not per-post)
_text_model = Detoxify('original')

# --- Default thresholds (used if no System Parameter is set) ---
DEFAULT_TEXT_BLOCK_THRESHOLD = 0.85
DEFAULT_TEXT_REVIEW_THRESHOLD = 0.70
DEFAULT_IMAGE_BLOCK_THRESHOLD = 0.75
DEFAULT_IMAGE_REVIEW_THRESHOLD = 0.60

PARAM_TEXT_BLOCK = 'forum_content_moderation.text_block_threshold'
PARAM_TEXT_REVIEW = 'forum_content_moderation.text_review_threshold'
PARAM_IMAGE_BLOCK = 'forum_content_moderation.image_block_threshold'
PARAM_IMAGE_REVIEW = 'forum_content_moderation.image_review_threshold'

HARMFUL_IMAGE_CATEGORIES = ('Porn', 'Hentai', 'Sexy')

# Simple leetspeak / obfuscation substitutions used to catch evasive text
LEET_SUBSTITUTIONS = {'0': 'o', '1': 'i', '3': 'e', '4': 'a', '5': 's', '@': 'a', '$': 's'}

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

    # ------------------------------------------------------------------
    # create() — moderate on creation
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        posts = super().create(vals_list)

        for post in posts:
            level, reason = self._check_content_level(post)
            self._apply_moderation_result(post, level, reason)

        return posts

    # ------------------------------------------------------------------
    # write() — re-moderate on edit, and log moderator overrides
    # ------------------------------------------------------------------
    def write(self, vals):
        # Track manual state changes made by a human/UI action. Our own
        # automated changes bypass this method entirely (see
        # _apply_moderation_result below), so any 'state' change that
        # DOES arrive here is a genuine external/manual override.
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

        # Re-run moderation if a moderation-relevant field changed.
        if any(f in vals for f in MODERATION_TRIGGER_FIELDS):
            for post in self:
                level, reason = self._check_content_level(post)
                self._apply_moderation_result(post, level, reason, is_edit=True)

        return result

    def _apply_moderation_result(self, post, level, reason, is_edit=False):
        # Bypass our own write() override entirely for automated state
        # changes: this avoids re-entrant moderation checks, keeps the
        # write in the SAME environment/cache as the caller (avoiding a
        # stale-cache issue that with_context() would introduce), and
        # means only genuine external/manual writes get logged as
        # overrides in write() above.
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
    # Content-level check (text + image)
    # ------------------------------------------------------------------
    def _check_content_level(self, post):
        """Returns ('block' | 'review' | None, reason). First violation found wins."""
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
                full_url = f"http://localhost:8069{url}"
                img_response = requests.get(full_url, timeout=10)
                if img_response.status_code == 200:
                    level, reason = self._check_image_nsfw_bytes(
                        img_response.content, 'linked_image.png', 'image/png'
                    )
                    if level == 'block':
                        return 'block', reason
                    if level == 'review' and image_level is None:
                        image_level, image_reason = level, reason
            except Exception as e:
                _logger.error("Failed to fetch image from %s: %s", url, e)

        if text_level == 'review':
            return 'review', text_reason
        if image_level == 'review':
            return 'review', image_reason

        return None, None

    # ------------------------------------------------------------------
    # Text moderation — chunked scoring + obfuscation-aware normalization
    # ------------------------------------------------------------------
    def _check_text_toxicity(self, post):
        raw_text = re.sub(r'<[^>]+>', ' ', post.content or '')
        title = post.name or ''
        full_text = f"{title}. {raw_text}".strip()

        if not full_text:
            return None, None

        block_threshold, review_threshold = self._get_text_thresholds()

        try:
            worst_category, worst_score = self._score_worst_across_variants(full_text)

            if worst_score >= block_threshold:
                return 'block', f"Text flagged for {worst_category} (score: {worst_score:.2f})"
            if worst_score >= review_threshold:
                return 'review', f"Text borderline for {worst_category} (score: {worst_score:.2f})"
            return None, None
        except Exception as e:
            _logger.error("Text moderation check failed: %s", e)
            return None, None

    def _score_worst_across_variants(self, text):
        """
        Scores both the original text and a de-obfuscated (leetspeak-normalized)
        variant, in chunks to avoid silent truncation on long posts, and
        returns the single worst (category, score) found across all of it.
        """
        variants = [text]
        normalized = self._normalize_for_detection(text)
        if normalized != text:
            variants.append(normalized)

        worst_category, worst_score = None, 0.0

        for variant in variants:
            for chunk in self._chunk_text(variant, max_chars=800):
                if not chunk.strip():
                    continue
                results = self._text_model_predict(chunk)
                cat, score = max(results.items(), key=lambda kv: kv[1])
                if score > worst_score:
                    worst_category, worst_score = cat, score

        return worst_category, worst_score

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
        if not text:
            return []
        return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]

    def _text_model_predict(self, text):
        return _text_model.predict(text)

    # ------------------------------------------------------------------
    # Image moderation
    # ------------------------------------------------------------------
    def _check_image_nsfw_bytes(self, image_bytes, filename, mimetype):
        block_threshold, review_threshold = self._get_image_thresholds()

        try:
            files = {'image': (filename, image_bytes, mimetype)}
            response = requests.post(
                "http://localhost:5001/check-image",
                files=files,
                timeout=10
            )
            result = response.json()

            predictions = result.get('predictions', [])
            probs = {p['className']: p['probability'] for p in predictions}

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
            _logger.error("NSFW check failed: %s", e)
            return None, None