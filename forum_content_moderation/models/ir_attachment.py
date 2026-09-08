from odoo import models, api
import logging

_logger = logging.getLogger(__name__)


class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    @api.model_create_multi
    def create(self, vals_list):
        attachments = super().create(vals_list)
        self._check_attachments_for_moderation(attachments)
        return attachments

    def write(self, vals):
        result = super().write(vals)
        # Only re-run moderation if the actual binary content changed.
        # Without this guard, unrelated writes (renaming, access-token
        # regeneration, public flag toggling, etc.) would trigger a
        # redundant NSFW re-check - including a network call to
        # nsfw-service - on an image that hasn't actually changed.
        if 'datas' in vals or 'raw' in vals:
            self._check_attachments_for_moderation(self)
        return result

    def _check_attachments_for_moderation(self, attachments):
        moderation = self.env['content.moderation.mixin']
        for attachment in attachments:
            if attachment.res_model != 'forum.post' or not attachment.res_id:
                continue
            if not attachment.mimetype or not attachment.mimetype.startswith('image/'):

                continue

            post = self.env['forum.post'].browse(attachment.res_id)
            if not post.exists():
                continue

            try:
                image_bytes = attachment.raw
                if not image_bytes:
                    continue
                level, reason = moderation.check_image_bytes(
                    image_bytes, attachment.name or 'attachment.png', attachment.mimetype
                )
                if level in ('block', 'review'):
                    post._apply_moderation_result(post, level, reason)
            except Exception as e:
                _logger.error(
                    "Attachment moderation check failed for attachment %s: %s",
                    attachment.id, e
                )