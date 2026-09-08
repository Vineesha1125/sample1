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
        self._check_attachments_for_moderation(self)
        return result

    def _check_attachments_for_moderation(self, attachments):
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
                level, reason = post._check_image_nsfw_bytes(
                    image_bytes, attachment.name or 'attachment.png', attachment.mimetype
                )
                if level in ('block', 'review'):
                    post._apply_moderation_result(post, level, reason)
            except Exception as e:
                _logger.error(
                    "Attachment moderation check failed for attachment %s: %s",
                    attachment.id, e
                )