from odoo import models
import requests
import logging
import re
from detoxify import Detoxify
from transformers import pipeline

_logger = logging.getLogger(__name__)

# Loaded ONCE at Odoo start-up, shared by every caller of this service.
_text_model = Detoxify('original')

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

DEFAULT_TEXT_BLOCK_THRESHOLD = 0.85
DEFAULT_TEXT_REVIEW_THRESHOLD = 0.70
DEFAULT_IMAGE_BLOCK_THRESHOLD = 0.75
DEFAULT_IMAGE_REVIEW_THRESHOLD = 0.60

PARAM_TEXT_BLOCK = 'forum_content_moderation.text_block_threshold'
PARAM_TEXT_REVIEW = 'forum_content_moderation.text_review_threshold'
PARAM_IMAGE_BLOCK = 'forum_content_moderation.image_block_threshold'
PARAM_IMAGE_REVIEW = 'forum_content_moderation.image_review_threshold'

HARMFUL_IMAGE_CATEGORIES = ('Porn', 'Hentai', 'Sexy')
LEET_SUBSTITUTIONS = {'0': 'o', '1': 'i', '3': 'e', '4': 'a', '5': 's', '@': 'a', '$': 's'}

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


class ContentModerationMixin(models.AbstractModel):
    _name = 'content.moderation.mixin'
    _description = 'Shared Content Moderation Service'

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

    def check_text(self, text, raise_on_error=False):
        text = (text or '').strip()
        if not text:
            return None, None

        illegal_matches = self._score_illegal_text_keywords(text)
        if illegal_matches:
            return 'block', f"Text flagged as illegal content (matched: {', '.join(illegal_matches)})"

        try:
            semantic_level, semantic_label, semantic_score = self._score_illegal_text_semantic(text)
            if semantic_level == 'block':
                return 'block', f"Text flagged as illegal content ({semantic_label}, confidence: {semantic_score:.2f})"
            if semantic_level == 'review':
                return 'review', f"Text borderline for illegal content (model confidence this is normal conversation: {semantic_score:.2f})"
        except Exception as e:
            _logger.error("Illegal-content semantic check failed: %s", e)

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
        lowered = text.lower()
        return [kw for kw in ILLEGAL_TEXT_KEYWORDS if kw in lowered]

    def _score_illegal_text_semantic(self, text):
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

    def check_image_bytes(self, image_bytes, filename='upload.png', mimetype='image/png', raise_on_error=False):
        block_threshold, review_threshold = self._get_image_thresholds()

        try:
            files = {'image': (filename, image_bytes, mimetype)}
            response = requests.post(
                "http://localhost:5001/check-image",
                files=files,
                timeout=10
            )
            result = response.json()

            if result.get('flagged') is True and result.get('reason'):
                return 'block', result['reason']

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
            if raise_on_error:
                raise
            return None, None