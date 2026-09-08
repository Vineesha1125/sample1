from odoo import models, api, fields, _
import requests
import logging
import re
import base64

_logger = logging.getLogger(__name__)

PARAM_ODOO_BASE_URL = 'forum_content_moderation.odoo_base_url'
DEFAULT_ODOO_BASE_URL = 'http://localhost:8069'

MODERATION_TRIGGER_FIELDS = ('name', 'content')


class ForumPost(models.Model):
    _inherit = ['forum.post', 'content.moderation.mixin']

    moderation_reason = fields.Char(
        string='Moderation Reason',
        readonly=True,
        copy=False
    )

    # ------------------------------------------------------------------
    # Config lookups
    # ------------------------------------------------------------------

    def _get_odoo_base_url(self):
        icp = self.env['ir.config_parameter'].sudo()
        return icp.get_param(
            PARAM_ODOO_BASE_URL,
            default=None
        ) or DEFAULT_ODOO_BASE_URL

    def _is_safe_external_url(self, url):
        """Blocks SSRF: rejects private, loopback,
        link-local, reserved and metadata IP addresses."""
        import ipaddress
        import socket
        from urllib.parse import urlparse

        try:
            parsed = urlparse(url)

            if parsed.scheme not in ('http', 'https'):
                return False

            hostname = parsed.hostname

            if not hostname:
                return False

            resolved_ip = socket.gethostbyname(hostname)
            ip_obj = ipaddress.ip_address(resolved_ip)

            if (
                ip_obj.is_private
                or ip_obj.is_loopback
                or ip_obj.is_link_local
                or ip_obj.is_reserved
                or ip_obj.is_multicast
                or str(ip_obj) == '169.254.169.254'
            ):
                return False

            return True

        except Exception as e:
            _logger.warning(
                "Failed to validate external URL safety for %s: %s",
                url,
                e
            )
            return False

    # ------------------------------------------------------------------
    # Admin notification and actions
    # ------------------------------------------------------------------

    def _get_admin_moderator_users(self):
        """Safely finds active administrators/moderators."""
        admin_user = (
            self.env.ref(
                'base.user_admin',
                raise_if_not_found=False
            )
            or self.env['res.users'].sudo().browse(2)
        )

        users = admin_user.exists() or self.env['res.users'].sudo()

        admin_group = self.env.ref(
            'base.group_system',
            raise_if_not_found=False
        )

        mod_group = self.env.ref(
            'website_forum.group_forum_moderator',
            raise_if_not_found=False
        )

        for group in (admin_group, mod_group):
            if group and hasattr(group, 'user_ids'):
                try:
                    group_users = group.user_ids.filtered(
                        lambda u: u.active and not u.share
                    )
                    users |= group_users
                except Exception:
                    pass

        return users or self.env.user

    def _notify_admin_moderation(
        self,
        post,
        level,
        reason,
        is_edit=False
    ):
        """Notify administrators/moderators."""
        try:
            admin_users = self._get_admin_moderator_users()
            admin_partner_ids = admin_users.mapped(
                'partner_id'
            ).ids

            action_tag = (
                "on edit"
                if is_edit
                else "new submission"
            )

            header_title = (
                "🚨 Illegal / Prohibited Content Detected"
                if level == 'block'
                else "⚠️ Borderline Content Flagged"
            )

            body = (
                "<div style='padding: 12px; "
                "border: 1px solid #e0a800; "
                "border-left: 5px solid #dc3545; "
                "background-color: #fffdf5; "
                "border-radius: 4px;'>"

                f"<h4 style='color: #dc3545; "
                f"margin-top: 0;'>{header_title} - "
                "Pending Admin Approval</h4>"

                f"<p><b>Post Title:</b> "
                f"{post.name or 'No Title'}</p>"

                f"<p><b>Author:</b> "
                f"{post.create_uid.name or 'Unknown'} "
                f"(ID: {post.create_uid.id})</p>"

                f"<p><b>Flag Reason:</b> {reason}</p>"

                f"<p><b>Event:</b> "
                f"{action_tag.capitalize()}</p>"

                "<hr style='border-top: 1px solid #f0ad4e;'/>"

                "<p style='color: #856404; "
                "margin-bottom: 0;'>"

                "<i>This post is held in "
                "<b>Waiting Validation (pending)</b> "
                "status and is <b>NOT</b> visible on the "
                "website forum. Please review the post and "
                "click <b>Approve &amp; Post</b> to publish "
                "it or <b>Reject (Offensive)</b> to block it."
                "</i>"

                "</p>"
                "</div>"
            )

            try:
                post.message_post(
                    body=body,
                    partner_ids=admin_partner_ids,
                    message_type='comment',
                    subtype_xmlid='mail.mt_comment',
                )
            except Exception as e:
                _logger.error(
                    "Failed to post moderation notification "
                    "for post %s: %s",
                    post.id,
                    e
                )

            activity_type = self.env.ref(
                'mail.mail_activity_data_todo',
                raise_if_not_found=False
            )

            model_id = self.env['ir.model']._get_id(
                'forum.post'
            )

            for admin_user in admin_users:
                try:
                    existing_activity = (
                        self.env['mail.activity']
                        .sudo()
                        .search(
                            [
                                ('res_model', '=', 'forum.post'),
                                ('res_id', '=', post.id),
                                ('user_id', '=', admin_user.id),
                            ],
                            limit=1
                        )
                    )

                    if not existing_activity:
                        self.env['mail.activity'].sudo().create({
                            'activity_type_id': (
                                activity_type.id
                                if activity_type
                                else False
                            ),
                            'res_model_id': model_id,
                            'res_id': post.id,
                            'user_id': admin_user.id,
                            'summary': (
                                'Moderation Approval Needed: '
                                f'{post.name[:40] if post.name else "Post"}'
                            ),
                            'note': (
                                f'<p>Forum post '
                                f'<b>"{post.name}"</b> '
                                'was held for approval. '
                                f'Reason: <b>{reason}</b>.</p>'
                            ),
                        })

                except Exception as e:
                    _logger.error(
                        "Failed to create moderation activity "
                        "for admin %s on post %s: %s",
                        admin_user.id,
                        post.id,
                        e
                    )

        except Exception as e:
            _logger.error(
                "Error during admin moderation notification: %s",
                e
            )

    def action_approve_moderation(self):
        """Admin approves the post."""
        for post in self:
            super(ForumPost, post).write({
                'state': 'active',
                'active': True,
                'moderator_id': self.env.user.id,
            })

            post.message_post(
                body=(
                    f"✅ <b>Approved by Administrator "
                    f"({self.env.user.name})</b>. "
                    "Post is now approved, active, and "
                    "published on the forum."
                )
            )

            try:
                activities = (
                    self.env['mail.activity']
                    .sudo()
                    .search([
                        ('res_model', '=', 'forum.post'),
                        ('res_id', '=', post.id),
                    ])
                )

                activities.action_done()

            except Exception as e:
                _logger.error(
                    "Failed to mark activities done on approve "
                    "for post %s: %s",
                    post.id,
                    e
                )

        return True

    def action_reject_moderation(self):
        """Admin rejects the post."""
        for post in self:
            super(ForumPost, post).write({
                'state': 'offensive',
                'active': False,
                'moderator_id': self.env.user.id,
            })

            post.message_post(
                body=(
                    f"❌ <b>Rejected by Administrator "
                    f"({self.env.user.name})</b>. "
                    "Post is marked offensive and will NOT "
                    "be posted."
                )
            )

            try:
                activities = (
                    self.env['mail.activity']
                    .sudo()
                    .search([
                        ('res_model', '=', 'forum.post'),
                        ('res_id', '=', post.id),
                    ])
                )

                activities.action_done()

            except Exception as e:
                _logger.error(
                    "Failed to mark activities done on reject "
                    "for post %s: %s",
                    post.id,
                    e
                )

        return True

    # ------------------------------------------------------------------
    # create() - moderate on creation
    # ------------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        posts = super().create(vals_list)

        for post in posts:
            level, reason = self._check_content_level(post)
            self._apply_moderation_result(
                post,
                level,
                reason
            )

        return posts

    # ------------------------------------------------------------------
    # write() - re-moderate on edit
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

                if (
                    old_state in (
                        'offensive',
                        'flagged',
                        'pending'
                    )
                    and post.state != old_state
                ):
                    self._log_moderation_feedback(
                        post,
                        old_state
                    )

        if any(
            field in vals
            for field in MODERATION_TRIGGER_FIELDS
        ):
            for post in self:
                level, reason = self._check_content_level(post)

                self._apply_moderation_result(
                    post,
                    level,
                    reason,
                    is_edit=True
                )

        return result

    def _apply_moderation_result(
        self,
        post,
        level,
        reason,
        is_edit=False
    ):
        """Apply moderation result."""
        if level in ('block', 'review'):
            target_state = (
                'pending'
                if level == 'block'
                else 'flagged'
            )

            super(ForumPost, post).write({
                'state': target_state,
                'moderation_reason': reason,
            })

            prefix = (
                "Flagged automatically (on edit)"
                if is_edit
                else "Flagged automatically"
            )

            _logger.warning(
                "Forum post %s held in '%s' state "
                "(%s): %s",
                post.id,
                target_state,
                prefix,
                reason
            )

            self._notify_admin_moderation(
                post,
                level,
                reason,
                is_edit=is_edit
            )

    def _log_moderation_feedback(
        self,
        post,
        original_state
    ):
        try:
            self.env[
                'forum.moderation.feedback'
            ].sudo().create({
                'post_id': post.id,
                'original_state': original_state,
                'corrected_state': post.state,
                'corrected_by': self.env.uid,
            })

            _logger.info(
                "Moderation override logged for post %s: "
                "%s -> %s by user %s",
                post.id,
                original_state,
                post.state,
                self.env.uid
            )

        except Exception as e:
            _logger.error(
                "Failed to log moderation feedback "
                "for post %s: %s",
                post.id,
                e
            )

    # ------------------------------------------------------------------
    # Content-level moderation
    # ------------------------------------------------------------------

    def _check_content_level(self, post):
        """Returns ('block' | 'review' | None, reason)."""

        title = post.name or ''

        raw_text = re.sub(
            r'<[^>]+>',
            ' ',
            post.content or ''
        )

        full_text = f"{title}. {raw_text}".strip()

        moderation = self.env[
            'content.moderation.mixin'
        ]

        # 1. Text moderation
        text_level, text_reason = moderation.check_text(
            full_text
        )

        if text_level == 'block':
            return 'block', text_reason

        # 2. Image moderation
        content = post.content or ''

        image_level = None
        image_reason = None

        # 2a. Base64 embedded images
        base64_images = re.findall(
            r'data:image/[^;]+;base64,([^"\']+)',
            content
        )

        for b64_data in base64_images:
            try:
                image_bytes = base64.b64decode(
                    b64_data
                )

                level, reason = (
                    moderation.check_image_bytes(
                        image_bytes,
                        'embedded_image.png',
                        'image/png'
                    )
                )

                if level == 'block':
                    return 'block', reason

                if (
                    level == 'review'
                    and image_level is None
                ):
                    image_level = level
                    image_reason = reason

            except Exception as e:
                _logger.error(
                    "Failed to decode base64 image: %s",
                    e
                )

        # 2b. Odoo internal image attachments
        attachment_ids = set()

        for match in re.finditer(
            r'/(?:web|website)/(?:image|content)'
            r'(?:/|\?id=)(\d+)',
            content
        ):
            attachment_ids.add(
                int(match.group(1))
            )

        for att_id in attachment_ids:
            try:
                attachment = (
                    self.env['ir.attachment']
                    .sudo()
                    .browse(att_id)
                )

                if (
                    attachment.exists()
                    and attachment.raw
                ):
                    level, reason = (
                        moderation.check_image_bytes(
                            attachment.raw,
                            attachment.name
                            or 'linked_image.png',
                            attachment.mimetype
                            or 'image/png'
                        )
                    )

                    if level == 'block':
                        return 'block', reason

                    if (
                        level == 'review'
                        and image_level is None
                    ):
                        image_level = level
                        image_reason = reason

            except Exception as e:
                _logger.error(
                    "Failed to inspect attachment "
                    "image ID %s: %s",
                    att_id,
                    e
                )

        # 2c. Attachments linked to post
        linked_attachments = (
            self.env['ir.attachment']
            .sudo()
            .search([
                ('res_model', '=', 'forum.post'),
                ('res_id', '=', post.id),
                ('mimetype', '=like', 'image/%'),
            ])
        )

        for att in linked_attachments:
            if (
                att.id not in attachment_ids
                and att.raw
            ):
                try:
                    level, reason = (
                        moderation.check_image_bytes(
                            att.raw,
                            att.name or 'attachment.png',
                            att.mimetype or 'image/png'
                        )
                    )

                    if level == 'block':
                        return 'block', reason

                    if (
                        level == 'review'
                        and image_level is None
                    ):
                        image_level = level
                        image_reason = reason

                except Exception as e:
                    _logger.error(
                        "Failed to inspect linked "
                        "attachment %s: %s",
                        att.id,
                        e
                    )

        # 2d. External image URLs
        external_images = re.findall(
            r'<img[^>]+src=["\']'
            r'(https?://[^"\']+)',
            content
        )

        odoo_base = (
            self._get_odoo_base_url()
            .rstrip('/')
        )

        for ext_url in external_images:

            if (
                ext_url.startswith(odoo_base)
                or 'localhost:8069' in ext_url
                or '127.0.0.1:8069' in ext_url
            ):
                continue

            if not self._is_safe_external_url(
                ext_url
            ):
                _logger.warning(
                    "Blocked potentially unsafe "
                    "external image URL: %s",
                    ext_url
                )
                continue

            try:
                img_resp = requests.get(
                    ext_url,
                    timeout=5
                )

                if img_resp.status_code == 200:
                    level, reason = (
                        moderation.check_image_bytes(
                            img_resp.content,
                            'external_image.png',
                            img_resp.headers.get(
                                'Content-Type',
                                'image/png'
                            )
                        )
                    )

                    if level == 'block':
                        return 'block', reason

                    if (
                        level == 'review'
                        and image_level is None
                    ):
                        image_level = level
                        image_reason = reason

            except Exception as e:
                _logger.error(
                    "Failed to fetch external image %s: %s",
                    ext_url,
                    e
                )

        if text_level == 'review':
            return 'review', text_reason

        if image_level == 'review':
            return 'review', image_reason

        return None, None