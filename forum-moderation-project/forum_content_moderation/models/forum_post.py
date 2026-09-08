from odoo import models, api, fields, _
import requests
import logging
import re
import base64
from .content_moderation_mixin import get_text_model, get_illegal_classifier

_logger = logging.getLogger(__name__)

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

# Default thresholds
DEFAULT_TEXT_BLOCK_THRESHOLD = 0.85
DEFAULT_TEXT_REVIEW_THRESHOLD = 0.70
DEFAULT_IMAGE_BLOCK_THRESHOLD = 0.60
DEFAULT_IMAGE_REVIEW_THRESHOLD = 0.40

PARAM_TEXT_BLOCK = 'forum_content_moderation.text_block_threshold'
PARAM_TEXT_REVIEW = 'forum_content_moderation.text_review_threshold'
PARAM_IMAGE_BLOCK = 'forum_content_moderation.image_block_threshold'
PARAM_IMAGE_REVIEW = 'forum_content_moderation.image_review_threshold'

PARAM_NSFW_SERVICE_URL = 'forum_content_moderation.nsfw_service_url'
PARAM_ODOO_BASE_URL = 'forum_content_moderation.odoo_base_url'
DEFAULT_NSFW_SERVICE_URL = 'http://localhost:5001/check-image'
DEFAULT_ODOO_BASE_URL = 'http://localhost:8069'

HARMFUL_IMAGE_CATEGORIES = ('Porn', 'Hentai', 'Sexy')

LEET_SUBSTITUTIONS = {
    '0': 'o', '1': 'i', '!': 'i', '|': 'i', '3': 'e', '4': 'a',
    '@': 'a', '5': 's', '$': 's', '7': 't', '+': 't', '8': 'b',
    'v': 'u',
}

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
    'fake passport', 'fake driving license', 'fake driver license',
    'cloned card', 'stolen card', 'credit card dump', 'cvv dump',
    'selling drugs', 'drugs for sale', 'buy drugs', 'buy weapon',
    'buy gun', 'selling weapon', 'weapons sale', 'pistol for sale',
    'rifle for sale', 'ammo for sale', 'buy cocaine', 'sell cocaine',
    'buy heroin', 'sell heroin', 'buy weed', 'sell weed',
    'hire hacker', 'hacker for hire', 'ddos service', 'buy malware',
    'buy ransomware', 'rat malware', 'stolen credentials', 'account hack',
)

ILLEGAL_COMBINATIONS = [
    # Weapons / firearms combinations
    (
        r'\b(?:weapon|weapons|firearm|firearms|gun|guns|pistol|pistols|rifle|rifles|glock|ammo|ammunition|explosive|bomb|grenade)s?\b',
        r'\b(?:sale|s4le|sell|selling|buy|buying|discount|price|cash|order|deal|cheap|unregistered|unlicensed|blackmarket)\b',
    ),
    # Drugs / illicit substance combinations
    (
        r'\b(?:drug|drugs|weed|cannabis|marijuana|cocaine|coke|heroin|meth|methamphetamine|fentanyl|ketamine|mdma|ecstasy|lsd|pills|oxy|xanax|adderall)s?\b',
        r'\b(?:sale|s4le|sell|selling|buy|buying|dealer|delivery|stash|gram|order|price|plug|vendor)\b',
    ),
    # Hacking / cybercrime combinations
    (
        r'\b(?:hack|hacker|hacking|ddos|botnet|ransomware|exploit|keylogger|trojan|carding|cvv|fullz)\b',
        r'\b(?:service|hire|buy|sell|tool|leak|attack|unauthorized|stolen)\b',
    ),
]

MODERATION_TRIGGER_FIELDS = ('name', 'content')


class ForumPost(models.Model):
    _inherit = 'forum.post'

    moderation_reason = fields.Char(string='Moderation Reason', readonly=True, copy=False)

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
            _logger.warning("Invalid value for %s (%r), using default %.2f", param_key, raw, default_value)
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
    # Admin notification and actions
    # ------------------------------------------------------------------
    def _get_admin_moderator_users(self):
        """Safely finds active administrators/moderators across Odoo versions."""
        admin_user = self.env.ref('base.user_admin', raise_if_not_found=False) or self.env['res.users'].sudo().browse(2)
        users = admin_user.exists() or self.env['res.users'].sudo()

        admin_group = self.env.ref('base.group_system', raise_if_not_found=False)
        mod_group = self.env.ref('website_forum.group_forum_moderator', raise_if_not_found=False)

        for group in (admin_group, mod_group):
            if group and hasattr(group, 'user_ids'):
                try:
                    group_users = group.user_ids.filtered(lambda u: u.active and not u.share)
                    users |= group_users
                except Exception:
                    pass

        return users or self.env.user

    def _notify_admin_moderation(self, post, level, reason, is_edit=False):
        """Sends a notification message to administrators/moderators and creates a To-Do activity."""
        try:
            admin_users = self._get_admin_moderator_users()
            admin_partner_ids = admin_users.mapped('partner_id').ids

            action_tag = "on edit" if is_edit else "new submission"
            header_title = "🚨 Illegal / Prohibited Content Detected" if level == 'block' else "⚠️ Borderline Content Flagged"

            body = (
                f"<div style='padding: 12px; border: 1px solid #e0a800; border-left: 5px solid #dc3545; background-color: #fffdf5; border-radius: 4px;'>"
                f"<h4 style='color: #dc3545; margin-top: 0;'>{header_title} - Pending Admin Approval</h4>"
                f"<p><b>Post Title:</b> {post.name or 'No Title'}</p>"
                f"<p><b>Author:</b> {post.create_uid.name or 'Unknown'} (ID: {post.create_uid.id})</p>"
                f"<p><b>Flag Reason:</b> {reason}</p>"
                f"<p><b>Event:</b> {action_tag.capitalize()}</p>"
                f"<hr style='border-top: 1px solid #f0ad4e;'/>"
                f"<p style='color: #856404; margin-bottom: 0;'>"
                f"<i>This post is held in <b>Waiting Validation (pending)</b> status and is <b>NOT</b> visible on the website forum. "
                f"Please review the post and click <b>Approve &amp; Post</b> to publish it or <b>Reject (Offensive)</b> to block it.</i>"
                f"</p>"
                f"</div>"
            )

            try:
                post.message_post(
                    body=body,
                    partner_ids=admin_partner_ids,
                    message_type='comment',
                    subtype_xmlid='mail.mt_comment',
                )
            except Exception as e:
                _logger.error("Failed to post moderation notification for post %s: %s", post.id, e)

            # Create To-Do activity for admin users
            activity_type = self.env.ref('mail.mail_activity_data_todo', raise_if_not_found=False)
            model_id = self.env['ir.model']._get_id('forum.post')
            for admin_user in admin_users:
                try:
                    existing_activity = self.env['mail.activity'].sudo().search([
                        ('res_model', '=', 'forum.post'),
                        ('res_id', '=', post.id),
                        ('user_id', '=', admin_user.id),
                    ], limit=1)
                    if not existing_activity:
                        self.env['mail.activity'].sudo().create({
                            'activity_type_id': activity_type.id if activity_type else False,
                            'res_model_id': model_id,
                            'res_id': post.id,
                            'user_id': admin_user.id,
                            'summary': f'Moderation Approval Needed: {post.name[:40] if post.name else "Post"}',
                            'note': f'<p>Forum post <b>"{post.name}"</b> was held for approval. Reason: <b>{reason}</b>.</p>',
                        })
                except Exception as e:
                    _logger.error("Failed to create moderation activity for admin %s on post %s: %s", admin_user.id, post.id, e)
        except Exception as e:
            _logger.error("Error during admin moderation notification: %s", e)

    def action_approve_moderation(self):
        """Admin approves the post. It is now published and active on the forum."""
        for post in self:
            super(ForumPost, post).write({
                'state': 'active',
                'active': True,
                'moderator_id': self.env.user.id,
            })
            post.message_post(
                body=f"✅ <b>Approved by Administrator ({self.env.user.name})</b>. Post is now approved, active, and published on the forum."
            )
            try:
                activities = self.env['mail.activity'].sudo().search([
                    ('res_model', '=', 'forum.post'),
                    ('res_id', '=', post.id),
                ])
                activities.action_done()
            except Exception as e:
                _logger.error("Failed to mark activities done on approve for post %s: %s", post.id, e)
        return True

    def action_reject_moderation(self):
        """Admin rejects the post. It transitions to offensive only upon explicit admin action."""
        for post in self:
            super(ForumPost, post).write({
                'state': 'offensive',
                'active': False,
                'moderator_id': self.env.user.id,
            })
            post.message_post(
                body=f"❌ <b>Rejected by Administrator ({self.env.user.name})</b>. Post is marked offensive and will NOT be posted."
            )
            try:
                activities = self.env['mail.activity'].sudo().search([
                    ('res_model', '=', 'forum.post'),
                    ('res_id', '=', post.id),
                ])
                activities.action_done()
            except Exception as e:
                _logger.error("Failed to mark activities done on reject for post %s: %s", post.id, e)
        return True

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
                if old_state in ('offensive', 'flagged', 'pending') and post.state != old_state:
                    self._log_moderation_feedback(post, old_state)

        if any(f in vals for f in MODERATION_TRIGGER_FIELDS):
            for post in self:
                level, reason = self._check_content_level(post)
                self._apply_moderation_result(post, level, reason, is_edit=True)

        return result

    def _apply_moderation_result(self, post, level, reason, is_edit=False):
        """Applies moderation result. NEVER sets offensive automatically. Holds in pending/flagged."""
        if level in ('block', 'review'):
            target_state = 'pending' if level == 'block' else 'flagged'
            super(ForumPost, post).write({
                'state': target_state,
                'moderation_reason': reason,
            })
            prefix = "Flagged automatically (on edit)" if is_edit else "Flagged automatically"
            _logger.warning("Forum post %s held in '%s' state (%s): %s", post.id, target_state, prefix, reason)

            # Notify admin
            self._notify_admin_moderation(post, level, reason, is_edit=is_edit)

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
    # Content-level check (illegal keywords + combinations + text + images)
    # ------------------------------------------------------------------
    def _check_content_level(self, post):
        """Returns ('block' | 'review' | None, reason). First violation found wins."""
        title = post.name or ''
        raw_text = re.sub(r'<[^>]+>', ' ', post.content or '')
        full_text = f"{title}. {raw_text}".strip()

        # 1. Fast Keyword and Combination Matching for Illegal Content
        illegal_matches = self._score_illegal_text(full_text)
        if illegal_matches:
            return 'block', f"Text flagged as illegal content (matched: {', '.join(illegal_matches)})"

        # 2. Semantic Zero-Shot Classification
        try:
            semantic_level, semantic_label, semantic_score = self._score_illegal_text_semantic(full_text)
            if semantic_level == 'block':
                return 'block', f"Text flagged as illegal content ({semantic_label}, confidence: {semantic_score:.2f})"
            if semantic_level == 'review':
                return 'review', f"Text borderline for illegal content (model confidence normal conversation: {semantic_score:.2f})"
        except Exception as e:
            _logger.error("Illegal-content semantic check failed: %s", e)

        # 3. Text Toxicity Scoring
        text_level, text_reason = self._check_text_toxicity(post)
        if text_level == 'block':
            return 'block', text_reason

        # 4. Image Moderation (Base64, Odoo Attachment IDs, Linked Attachments, External URLs)
        content = post.content or ''
        image_level, image_reason = None, None

        # 4a. Base64 embedded images
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

        # 4b. Odoo internal image/content attachments from HTML
        attachment_ids = set()
        for match in re.finditer(r'/(?:web|website)/(?:image|content)(?:/|\?id=)(\d+)', content):
            attachment_ids.add(int(match.group(1)))

        for att_id in attachment_ids:
            try:
                attachment = self.env['ir.attachment'].sudo().browse(att_id)
                if attachment.exists() and attachment.raw:
                    level, reason = self._check_image_nsfw_bytes(
                        attachment.raw, attachment.name or 'linked_image.png', attachment.mimetype or 'image/png'
                    )
                    if level == 'block':
                        return 'block', reason
                    if level == 'review' and image_level is None:
                        image_level, image_reason = level, reason
            except Exception as e:
                _logger.error("Failed to inspect attachment image ID %s: %s", att_id, e)

        # 4c. Attachments linked to this post in ir_attachment table
        linked_attachments = self.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'forum.post'),
            ('res_id', '=', post.id),
            ('mimetype', '=like', 'image/%'),
        ])
        for att in linked_attachments:
            if att.id not in attachment_ids and att.raw:
                try:
                    level, reason = self._check_image_nsfw_bytes(
                        att.raw, att.name or 'attachment.png', att.mimetype or 'image/png'
                    )
                    if level == 'block':
                        return 'block', reason
                    if level == 'review' and image_level is None:
                        image_level, image_reason = level, reason
                except Exception as e:
                    _logger.error("Failed to inspect linked attachment %s: %s", att.id, e)

        # 4d. External image URLs
        external_images = re.findall(r'<img[^>]+src=["\'](https?://[^"\']+)["\']', content)
        odoo_base = self._get_odoo_base_url().rstrip('/')
        for ext_url in external_images:
            if ext_url.startswith(odoo_base) or 'localhost:8069' in ext_url or '127.0.0.1:8069' in ext_url:
                continue
            try:
                img_resp = requests.get(ext_url, timeout=5)
                if img_resp.status_code == 200:
                    level, reason = self._check_image_nsfw_bytes(
                        img_resp.content, 'external_image.png', img_resp.headers.get('Content-Type', 'image/png')
                    )
                    if level == 'block':
                        return 'block', reason
                    if level == 'review' and image_level is None:
                        image_level, image_reason = level, reason
            except Exception as e:
                _logger.error("Failed to fetch external image %s: %s", ext_url, e)

        if text_level == 'review':
            return 'review', text_reason
        if image_level == 'review':
            return 'review', image_reason

        return None, None

    # ------------------------------------------------------------------
    # ILLEGAL SUBJECT-MATTER TEXT CHECK
    # ------------------------------------------------------------------
    def _score_illegal_text(self, text):
        """Scorer for illegal keywords & regex combinations across original and leetspeak-normalized text."""
        if not text:
            return []
        lowered = text.lower()
        normalized = self._normalize_for_detection(text)
        matches = set()

        for kw in ILLEGAL_TEXT_KEYWORDS:
            if kw in lowered or kw in normalized:
                matches.add(kw)

        # Check regex combinations on both lowered and normalized text
        for text_variant in (lowered, normalized):
            for pattern_subject, pattern_action in ILLEGAL_COMBINATIONS:
                if re.search(pattern_subject, text_variant, re.IGNORECASE) and re.search(pattern_action, text_variant, re.IGNORECASE):
                    subj_m = re.search(pattern_subject, text_variant, re.IGNORECASE)
                    act_m = re.search(pattern_action, text_variant, re.IGNORECASE)
                    matched_desc = f"{subj_m.group(0)} + {act_m.group(0)}" if subj_m and act_m else "prohibited transaction"
                    matches.add(matched_desc)

        return list(matches)

    def _score_illegal_text_semantic(self, text):
        """Zero-shot classification against illegal-content category labels."""
        if not text or not text.strip():
            return None, None, None
        classifier = get_illegal_classifier()
        result = classifier(text, candidate_labels=list(ILLEGAL_CATEGORY_LABELS))
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
        if not text or not text.strip():
            return None, 0.0, {}

        variants = [text]
        normalized = self._normalize_for_detection(text)
        if normalized != text:
            variants.append(normalized)

        worst_category, worst_score = None, 0.0
        all_scores = {}
        model = get_text_model()

        for variant in variants:
            for chunk in self._chunk_text(variant, max_chars=800):
                if not chunk.strip():
                    continue
                results = model.predict(chunk)
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
            _logger.error("Text moderation check failed: %s", e)
            return None, None

    def _normalize_for_detection(self, text):
        """Collapses repeated characters and common leetspeak substitutions."""
        text = re.sub(r'(.)\1{2,}', r'\1\1', text)
        lowered = text.lower()
        for k, v in LEET_SUBSTITUTIONS.items():
            lowered = lowered.replace(k, v)
        lowered = re.sub(r'\s{2,}', ' ', lowered)
        return lowered

    def _chunk_text(self, text, max_chars=800):
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

    # ------------------------------------------------------------------
    # IMAGE MODERATION (NSFW microservice)
    # ------------------------------------------------------------------
    def _parse_nsfw_response(self, result):
        if isinstance(result.get('probabilities'), dict):
            return result['probabilities']

        if isinstance(result.get('predictions'), list):
            try:
                return {
                    p['className']: p['probability']
                    for p in result['predictions']
                }
            except (KeyError, TypeError):
                _logger.error("NSFW service returned malformed predictions: %r", result['predictions'])
                raise ValueError("Malformed predictions in NSFW response")

        if 'flagged' in result:
            return {}

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

            flagged = response_json.get('flagged', False)

            if response_json.get('flagged') is True and response_json.get('reason'):
                return 'block', response_json['reason']

            probs = self._parse_nsfw_response(response_json)
            harmful_probs = {k: v for k, v in probs.items() if k in HARMFUL_IMAGE_CATEGORIES}

            worst_category = None
            worst_score = 0.0
            if harmful_probs:
                worst_category = max(harmful_probs, key=harmful_probs.get)
                worst_score = harmful_probs[worst_category]

            # Trigger block if flagged by microservice OR if harmful score exceeds block threshold
            if flagged or worst_score >= block_threshold:
                cat_name = worst_category or "NSFW/Harmful"
                score_str = f" (score: {worst_score:.2f})" if worst_score > 0 else ""
                return 'block', f"Image flagged for {cat_name}{score_str}"

            if worst_score >= review_threshold:
                return 'review', f"Image borderline for {worst_category} (score: {worst_score:.2f})"

            return None, None
        except Exception as e:
            _logger.error("NSFW check failed: %s", e)
            return None, None